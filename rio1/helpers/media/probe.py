"""Probe a video with ffprobe and write probe.json (see docs/CONTRACTS.md).

Usage:
    .venv/bin/python rio1/helpers/media/probe.py <video> [<video2> ...] -o E/probe.json
        [--vfr-sample-s 30] [--vfr-cv 0.05]

VFR detection (two signals):
  1. Hint: avg_frame_rate and r_frame_rate disagree by more than 1 %.
  2. Evidence: frame timestamps from the first `vfr_sample_s` seconds of packets
     (cheap; no decoding). If the frame-interval coefficient of variation is
     above `vfr_cv`, or the ratio of the 95th to 5th percentile interval is
     above 1.5, the stream is VFR.
  When the timestamp sample exists it decides; the hint is only used when the
  sample is unavailable (too few packets).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

VFR_SAMPLE_S = 30.0   # seconds of packet timestamps to sample
VFR_CV = 0.05         # frame-interval coefficient of variation above which -> VFR
VFR_P95_P5 = 1.5      # 95th/5th percentile interval ratio above which -> VFR
VFR_HINT_TOL = 0.01   # relative avg vs r fps disagreement that counts as a hint


def _ffprobe_json(args: list[str]) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-of", "json", *args],
                         capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {out.stderr.strip()[:300]}")
    return json.loads(out.stdout or "{}")


def _rate(value: str | None) -> Fraction | None:
    if not value or value in ("0/0", "N/A"):
        return None
    try:
        r = Fraction(value)
    except (ValueError, ZeroDivisionError):
        return None
    return r if r > 0 else None


def frame_interval_stats(path: str | Path, sample_s: float = VFR_SAMPLE_S) -> dict | None:
    """Packet-timestamp stats for the first `sample_s` seconds of video (no decode)."""
    data = _ffprobe_json(["-select_streams", "v:0", "-read_intervals", f"%+{sample_s}",
                          "-show_entries", "packet=pts_time", str(path)])
    pts = sorted(float(p["pts_time"]) for p in data.get("packets", [])
                 if p.get("pts_time") not in (None, "N/A"))
    if len(pts) < 10:
        return None
    iv = [b - a for a, b in zip(pts, pts[1:]) if b - a > 1e-6]
    if len(iv) < 5:
        return None
    n = len(iv)
    mean = sum(iv) / n
    var = sum((x - mean) ** 2 for x in iv) / n
    s = sorted(iv)
    p5, p95 = s[int(0.05 * (n - 1))], s[int(0.95 * (n - 1))]
    return {"n": n, "mean_s": mean, "cv": (var ** 0.5) / mean if mean else 0.0,
            "p95_p5": p95 / p5 if p5 > 0 else float("inf")}


def probe(path: str | Path, vfr_sample_s: float = VFR_SAMPLE_S, vfr_cv: float = VFR_CV) -> dict:
    """Return the probe.json dict for `path`. Raises RuntimeError on failure."""
    path = Path(path)
    if not path.exists():
        raise RuntimeError(f"not found: {path}")
    data = _ffprobe_json(["-show_format", "-show_streams", str(path)])
    streams = data.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if v is None:
        raise RuntimeError(f"no video stream in {path}")
    fmt = data.get("format", {})

    avg, r = _rate(v.get("avg_frame_rate")), _rate(v.get("r_frame_rate"))
    hint = bool(avg and r and abs(float(avg) - float(r)) / float(r) > VFR_HINT_TOL)
    stats = frame_interval_stats(path, vfr_sample_s)
    if stats is not None:
        vfr = stats["cv"] > vfr_cv or stats["p95_p5"] > VFR_P95_P5
    else:
        vfr = hint

    dur = fmt.get("duration") or v.get("duration")
    size = fmt.get("size")
    return {
        "path": str(path.resolve()),
        "duration_s": round(float(dur), 3) if dur not in (None, "N/A") else None,
        "width": int(v["width"]),
        "height": int(v["height"]),
        "avg_fps": v.get("avg_frame_rate"),
        "r_fps": v.get("r_frame_rate"),
        "vfr": bool(vfr),
        "video_codec": v.get("codec_name"),
        "audio_codec": a.get("codec_name") if a else None,
        "audio_channels": int(a["channels"]) if a and a.get("channels") else 0,
        "size_bytes": int(size) if size else path.stat().st_size,
    }


def probe_many(paths: list[str | Path], vfr_sample_s: float = VFR_SAMPLE_S,
               vfr_cv: float = VFR_CV) -> dict:
    """Multi-clip probe.json: top-level = first clip (sets the output format), except
    duration_s / size_bytes are summed and vfr is any(); "clips" = {stem: single probe}."""
    clips: dict[str, dict] = {}
    for p in paths:
        stem = Path(p).stem
        if stem in clips:
            raise RuntimeError(f"duplicate clip name (stem): {stem}")
        clips[stem] = probe(p, vfr_sample_s, vfr_cv)
    infos = list(clips.values())
    out = dict(infos[0])
    durs = [i["duration_s"] for i in infos]
    out["duration_s"] = round(sum(durs), 3) if all(d is not None for d in durs) else None
    out["size_bytes"] = sum(i["size_bytes"] for i in infos)
    out["vfr"] = any(i["vfr"] for i in infos)
    out["clips"] = clips
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="ffprobe one or more videos -> probe.json")
    ap.add_argument("video", type=Path, nargs="+")
    ap.add_argument("-o", "--out", type=Path, required=True)
    ap.add_argument("--vfr-sample-s", type=float, default=VFR_SAMPLE_S)
    ap.add_argument("--vfr-cv", type=float, default=VFR_CV)
    args = ap.parse_args()
    try:
        info = probe_many(args.video, args.vfr_sample_s, args.vfr_cv)
    except Exception as e:  # one-line reason, non-zero exit
        sys.exit(f"probe: {e}")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(info, indent=2) + "\n")
    print(json.dumps(info))


if __name__ == "__main__":
    main()
