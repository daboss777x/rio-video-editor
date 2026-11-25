"""Classify silent gaps in a screen recording as static / typing / active.

Usage:
    .venv/bin/python rio2/helpers/media/detect_screen_activity.py \
        --video V --transcript E/transcripts/<stem>.json --out E/activity.json \
        [--profile rio2/profiles/tutorial.json] [--<param> value ...]

Gaps: every gap between consecutive Scribe `word`s that is >= gap_min_s, plus the
lead-in before the first word and the tail after the last (docs/CONTRACTS.md).

Method (one decode pass, bounded memory):
  ffmpeg decodes the whole video once, drops to `analysis_fps`, downscales to
  `analysis_width` and streams raw 8-bit grayscale frames over a pipe. Only
  frames that fall inside a gap window [start+edge_guard_s, end-edge_guard_s]
  are analysed; Python holds just the previous frame, the gap's first frame and
  a boolean "union of changes" mask for the current gap, plus a few floats per
  analysed frame.

  Per analysed frame: changed = |frame - prev| > pix_T (0..255);
      frac = fraction of pixels changed; bbox of the changed pixels.
  Per gap:
      change_score = max frac over the gap
      net          = |last frame - first frame| > pix_T   (what stayed changed)
      gross        = union of all per-frame changed masks  (everything that moved)
      persist      = net_px / gross_px     (typing ~1: text stays; cursor ~0: reverts)
      busy         = share of frame pairs with frac > noise_frac

  Classification:
      active  if change_score >= scene_T                (click, page switch, scroll)
              or net_frac >= active_net_frac            (big gradual change: load/fade)
      typing  if busy >= typing_min_busy and persist >= typing_min_persist
              and net_frac >= typing_min_net_frac and the net change is one band
              (net bbox height <= typing_max_h_frac)
      active  if the same typing evidence but the net change spans several lines
              (output streaming in / multi-line edit: keep it, per "if in doubt keep")
      static  otherwise (nothing, a blinking caret, or only the cursor moving)

  must_keep: static -> null; typing -> [end - typing_tail_s, end];
  active -> from the first to the last significant change, padded by
  active_pre_s / active_hold_s, clipped to the gap.

All thresholds are parameters: defaults below, overridden by the profile's
"detect" section, overridden by CLI flags.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

try:
    from .probe import probe
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from probe import probe  # noqa: E402

DEFAULTS: dict = {
    "gap_min_s": 1.0,            # minimum silent gap between words to classify
    "analysis_width": 480,       # px; frames are downscaled to this width
    "analysis_fps": 4.0,         # frames per second analysed
    "pix_T": 12,                 # 0..255 gray-level change for a pixel to count as changed
    "noise_frac": 2e-5,          # per-frame change below this fraction is noise (~3 px @480x300)
    "scene_T": 0.003,            # per-frame changed fraction that means a click/navigation/scroll
    "active_net_frac": 0.02,     # net (first vs last frame) change that means active
    "typing_min_busy": 0.25,     # share of frame pairs that must show change for typing
    "typing_min_persist": 0.5,   # net/gross pixel ratio for typing (cursor motion is ~0.05)
    "typing_min_net_frac": 2e-4, # net change needed for typing (a few characters)
    "typing_max_h_frac": 0.08,   # net-change bbox height (fraction of frame) for "one line"
    "typing_tail_s": 1.0,        # typing gaps keep [end - typing_tail_s, end]
    "active_pre_s": 0.5,         # keep this much before the first significant change
    "active_hold_s": 1.0,        # keep this much after the last significant change
    "edge_guard_s": 0.1,         # ignore frames this close to gap edges (speech-side motion)
    "hwaccel": "none",           # none | videotoolbox | auto (auto = try videotoolbox, then software).
                                 # Software is ~10x faster here: VT download+scale overhead dominates.
    "threads": 0,                # ffmpeg decode threads (0 = ffmpeg default)
}
INT_KEYS = {"analysis_width", "pix_T", "threads"}


# ------------------------------------------------------------------ gaps


def load_words(transcript_path: Path) -> list[tuple[float, float]]:
    data = json.loads(Path(transcript_path).read_text())
    out = []
    for w in data.get("words", []):
        if w.get("type") == "word" and w.get("start") is not None and w.get("end") is not None:
            out.append((float(w["start"]), float(w["end"])))
    out.sort()
    return out


def find_gaps(words: list[tuple[float, float]], duration: float, gap_min_s: float) -> list[tuple[float, float]]:
    """Lead-in, inter-word gaps and tail that are >= gap_min_s."""
    gaps = []
    if not words:
        return [(0.0, duration)] if duration >= gap_min_s else []
    cur = 0.0
    for s, e in words:
        if s - cur >= gap_min_s:
            gaps.append((cur, s))
        cur = max(cur, e)
    if duration - cur >= gap_min_s:
        gaps.append((cur, duration))
    return gaps


# ------------------------------------------------------------------ per-gap state


class GapStats:
    """Accumulates bounded per-gap statistics while frames stream past."""

    def __init__(self, start: float, end: float, shape: tuple[int, int]):
        self.start, self.end = start, end
        self.ref: np.ndarray | None = None
        self.last: np.ndarray | None = None
        self.union = np.zeros(shape, dtype=bool)
        self.times: list[float] = []
        self.fracs: list[float] = []
        self.bboxes: list[tuple[int, int, int, int] | None] = []

    def add(self, t: float, frame: np.ndarray, prev: np.ndarray | None, pix_T: int) -> None:
        if self.ref is None:
            self.ref = frame.copy()
        elif prev is not None:
            mask = changed_mask(frame, prev, pix_T)
            self.union |= mask
            self.times.append(t)
            self.fracs.append(float(mask.mean()))
            self.bboxes.append(bbox(mask))
        self.last = frame  # caller hands us a fresh buffer each frame

    def classify(self, p: dict) -> dict:
        n_px = self.union.size
        fr = np.asarray(self.fracs, dtype=np.float64)
        change_score = float(fr.max()) if fr.size else 0.0
        gross_px = int(self.union.sum())
        if self.ref is not None and self.last is not None:
            net = changed_mask(self.last, self.ref, p["pix_T"])
            net_px = int(net.sum())
            nb = bbox(net)
        else:
            net_px, nb = 0, None
        net_frac = net_px / n_px
        net_h_frac = ((nb[3] - nb[1] + 1) / self.union.shape[0]) if nb else 0.0
        persist = min(1.0, net_px / gross_px) if gross_px else 0.0
        busy = float((fr > p["noise_frac"]).mean()) if fr.size else 0.0
        typing_like = (busy >= p["typing_min_busy"] and persist >= p["typing_min_persist"]
                       and net_frac >= p["typing_min_net_frac"])

        if change_score >= p["scene_T"]:
            cls, why = "active", "scene"
        elif net_frac >= p["active_net_frac"]:
            cls, why = "active", "net"
        elif typing_like and net_h_frac <= p["typing_max_h_frac"]:
            cls, why = "typing", "typing"
        elif typing_like:
            cls, why = "active", "multiline"
        else:
            cls, why = "static", "static"

        must_keep = None
        if cls == "typing":
            must_keep = [round(max(self.start, self.end - p["typing_tail_s"]), 3), round(self.end, 3)]
        elif cls == "active":
            level = p["scene_T"] if why == "scene" else p["noise_frac"]
            hits = [t for t, f in zip(self.times, self.fracs) if f >= level] or self.times
            dt = 1.0 / p["analysis_fps"]
            if hits:
                a = max(self.start, hits[0] - dt - p["active_pre_s"])
                b = min(self.end, hits[-1] + p["active_hold_s"])
            else:
                a, b = self.start, self.end
            must_keep = [round(a, 3), round(b, 3)]

        typing_score = round(busy * persist, 4) if typing_like else 0.0
        return {
            "start": round(self.start, 3), "end": round(self.end, 3), "class": cls,
            "change_score": round(change_score, 5), "typing_score": typing_score,
            "must_keep": must_keep,
            "features": {"reason": why, "net_frac": round(net_frac, 5),
                         "gross_frac": round(gross_px / n_px, 5), "persist": round(persist, 3),
                         "busy": round(busy, 3), "net_h_frac": round(net_h_frac, 4),
                         "frames": len(self.fracs) + (1 if self.ref is not None else 0)},
        }


def changed_mask(a: np.ndarray, b: np.ndarray, pix_T: int) -> np.ndarray:
    # uint8-safe absolute difference without widening the whole frame
    return (np.maximum(a, b) - np.minimum(a, b)) > pix_T


def bbox(mask: np.ndarray) -> tuple[int, int, int, int] | None:
    rows = np.flatnonzero(mask.any(axis=1))
    if rows.size == 0:
        return None
    cols = np.flatnonzero(mask.any(axis=0))
    return int(cols[0]), int(rows[0]), int(cols[-1]), int(rows[-1])


# ------------------------------------------------------------------ decode pass


def analysis_size(src_w: int, src_h: int, width: int) -> tuple[int, int]:
    w = min(width, src_w)
    w -= w % 2
    h = int(round(src_h * w / src_w / 2.0)) * 2
    return w, max(2, h)


def _ffmpeg_cmd(video: Path, w: int, h: int, fps: float, hw: str | None, threads: int) -> list[str]:
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error"]
    if threads:
        cmd += ["-threads", str(threads)]
    if hw:
        cmd += ["-hwaccel", hw]
    cmd += ["-i", str(video), "-an", "-sn", "-dn",
            "-vf", f"fps={fps},scale={w}:{h}:flags=area,format=gray",
            "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    return cmd


def stream_gaps(video: Path, gaps: list[tuple[float, float]], w: int, h: int, p: dict,
                hw: str | None) -> list[dict]:
    """One decode pass; returns classified gaps. Raises CalledProcessError on ffmpeg failure."""
    fsize = w * h
    fps = float(p["analysis_fps"])
    guard = float(p["edge_guard_s"])
    windows = [(s + guard, e - guard) for s, e in gaps]
    results: list[dict | None] = [None] * len(gaps)
    gi = 0
    cur: GapStats | None = None
    prev: np.ndarray | None = None
    prev_in_gap = False
    with tempfile.TemporaryFile() as errf:
        proc = subprocess.Popen(_ffmpeg_cmd(video, w, h, fps, hw, int(p["threads"])),
                                stdout=subprocess.PIPE, stderr=errf, bufsize=fsize * 2)
        idx = 0
        try:
            while True:
                buf = proc.stdout.read(fsize)
                if not buf or len(buf) < fsize:
                    break
                t = idx / fps
                idx += 1
                # close gaps whose window has passed
                while gi < len(gaps) and t > windows[gi][1]:
                    if cur is not None:
                        results[gi] = cur.classify(p)
                        cur = None
                    prev, prev_in_gap = None, False
                    gi += 1
                if gi >= len(gaps):
                    # nothing left to analyse; keep draining so ffmpeg exits cleanly
                    prev, prev_in_gap = None, False
                    continue
                if t >= windows[gi][0]:
                    frame = np.frombuffer(buf, dtype=np.uint8).reshape(h, w)
                    if cur is None:
                        cur = GapStats(gaps[gi][0], gaps[gi][1], (h, w))
                    cur.add(t, frame, prev if prev_in_gap else None, int(p["pix_T"]))
                    prev, prev_in_gap = frame, True
                else:
                    prev, prev_in_gap = None, False
        finally:
            proc.stdout.close()
            rc = proc.wait()
        if rc != 0:
            errf.seek(0)
            raise subprocess.CalledProcessError(rc, "ffmpeg", stderr=errf.read().decode(errors="replace")[-400:])
    if cur is not None and gi < len(gaps):
        results[gi] = cur.classify(p)
    # gaps with no decoded frames (e.g. shorter than the guard, or past EOF)
    for i, (s, e) in enumerate(gaps):
        if results[i] is None:
            results[i] = GapStats(s, e, (h, w)).classify(p)
    return results  # type: ignore[return-value]


def detect(video: Path, transcript: Path, params: dict) -> dict:
    info = probe(video)
    duration = float(info["duration_s"])
    gaps = find_gaps(load_words(transcript), duration, float(params["gap_min_s"]))
    w, h = analysis_size(info["width"], info["height"], int(params["analysis_width"]))
    hw_pref = str(params["hwaccel"]).lower()
    order = {"auto": ["videotoolbox", None], "none": [None]}.get(hw_pref, [hw_pref, None])
    if not gaps:
        results = []
    else:
        last_err: Exception | None = None
        for hw in order:
            try:
                results = stream_gaps(video, gaps, w, h, params, hw)
                break
            except subprocess.CalledProcessError as e:
                last_err = e
                print(f"detect: decode with hwaccel={hw} failed, retrying"
                      f"{' in software' if hw else ''}", file=sys.stderr)
        else:
            raise RuntimeError(f"ffmpeg decode failed: {getattr(last_err, 'stderr', last_err)}")
    return {"gaps": results, "analysis": {"width": w, "height": h, "duration_s": duration}}


# ------------------------------------------------------------------ CLI


def resolve_params(profile_path: Path | None, overrides: dict) -> tuple[dict, str]:
    params = dict(DEFAULTS)
    name = "default"
    if profile_path:
        prof = json.loads(Path(profile_path).read_text())
        name = prof.get("name", Path(profile_path).stem)
        for k, v in (prof.get("detect") or {}).items():
            if k in DEFAULTS:
                params[k] = v
    for k, v in overrides.items():
        if v is not None:
            params[k] = v
    for k in params:
        if k in INT_KEYS:
            params[k] = int(params[k])
        elif k != "hwaccel":
            params[k] = float(params[k])
    return params, name


def main() -> None:
    ap = argparse.ArgumentParser(description="Classify silent gaps: static / typing / active")
    ap.add_argument("--video", type=Path, required=True)
    ap.add_argument("--transcript", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--profile", type=Path, default=None)
    for k, v in DEFAULTS.items():
        typ = str if k == "hwaccel" else (int if k in INT_KEYS else float)
        ap.add_argument(f"--{k.replace('_', '-')}", dest=k, type=typ, default=None,
                        help=f"default {v} (profile detect.{k} overrides the default)")
    args = ap.parse_args()
    overrides = {k: getattr(args, k) for k in DEFAULTS}
    try:
        params, name = resolve_params(args.profile, overrides)
        res = detect(args.video, args.transcript, params)
    except Exception as e:
        sys.exit(f"detect_screen_activity: {e}")
    out = {"source": args.video.name, "profile": name, "params": params,
           "analysis": res["analysis"], "gaps": res["gaps"]}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, indent=1) + "\n")
    counts: dict[str, int] = {}
    for g in res["gaps"]:
        counts[g["class"]] = counts.get(g["class"], 0) + 1
    print(f"{len(res['gaps'])} gaps: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))


if __name__ == "__main__":
    main()
