"""EDL chunking, merging and validation (pure logic, no media access).

CLI (run from the project root with .venv/bin/python):
    rio2/helpers/chunk_edl.py split    --edit-dir E [--profile P] [--chunk-minutes N]
    rio2/helpers/chunk_edl.py merge    --edit-dir E [--profile P]
    rio2/helpers/chunk_edl.py validate --edit-dir E [--profile P]

Public API:
    parse_takes(text)                       -> {stem: [phrase dicts]}
    load_words(edit_dir, stem)              -> [{"text","start","end"}] (type == "word")
    load_sources(edit_dir)                  -> {"order": [stems], "files": {stem: filename}, "plan": str|None}
    load_activity(edit_dir, stem=None)      -> dict | None  (E/activity/<stem>.json, else E/activity.json)
    plan_boundaries(total, target_s, cands) -> [0.0, b1, ..., total]
    split(edit_dir, profile, chunk_minutes=None) -> [chunk meta dicts]
    flag_phrases(phrases)                   -> [bool] (fillers or repeated n-grams)
    snap_range(start, end, words, ...)      -> (start, end, notes) | None
    compact_ranges(ranges, words, settings) -> (ranges, notes)  (silence policy cut_all)
    long_silences(ranges, words, settings)  -> [pauses over the threshold left in kept ranges]
    merge(edit_dir, profile)                -> (edl dict, violations, notes)
    validate_edl(edl, durations, activity, tol) -> [violation strings]
"""
from __future__ import annotations

import argparse
import bisect
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # this system's code root (rio1/ or rio2/): profiles/
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".m4v", ".webm", ".avi"}
EPS = 0.002  # seconds; edl times are rounded to 3 decimals
DEFAULT_PAD_MS = 80  # used when a boundary is mid-word or touching a word

PHRASE_RE = re.compile(r"^\s*\[(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)\](?:\s+S(\w+))?\s+(.*)$")
HEADER_RE = re.compile(r"^##\s+(.*?)\s*\(duration:")
FILLER_RE = re.compile(r"\b(?:um+|uh+|uhm|erm|er|ah|hmm+)\b")
FILLER_MULTI_RE = re.compile(r"\b(?:so basically|you know|i mean|kind of|sort of)\b")
LIKE_RE = re.compile(r"\blike,")


# ---------------------------------------------------------------- loading
def load_profile(path: Path | str | None = None) -> dict:
    p = Path(path) if path else ROOT / "profiles" / "tutorial.json"
    return json.loads(p.read_text())


def parse_takes(text: str) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    cur = None
    for line in text.splitlines():
        m = HEADER_RE.match(line)
        if m:
            cur = m.group(1).strip()
            out[cur] = []
            continue
        m = PHRASE_RE.match(line)
        if m and cur is not None:
            out[cur].append({"start": float(m.group(1)), "end": float(m.group(2)),
                             "speaker": m.group(3), "text": m.group(4).strip(),
                             "raw": line.strip()})
    return out


def load_words(edit_dir: Path, stem: str) -> list[dict]:
    p = Path(edit_dir) / "transcripts" / f"{stem}.json"
    if not p.exists():
        return []
    data = json.loads(p.read_text())
    ws = [{"text": (w.get("text") or "").strip(), "start": float(w["start"]), "end": float(w["end"])}
          for w in data.get("words", [])
          if w.get("type", "word") == "word" and w.get("start") is not None and w.get("end") is not None]
    ws.sort(key=lambda w: (w["start"], w["end"]))
    return ws


def load_silences(edit_dir: Path, stem: str) -> list[list[float]]:
    """Detected silences [[start, end], ...] (source seconds) from E/silences/<stem>.json, written
    by the silencedetect listener. Empty when the file is missing or unreadable (old runs)."""
    p = Path(edit_dir) / "silences" / f"{stem}.json"
    try:
        data = json.loads(p.read_text())
        return sorted([float(a), float(b)] for a, b in data.get("silences", []) if float(b) > float(a))
    except (OSError, ValueError, TypeError, AttributeError):
        return []


class Envelope:
    """Loudness per frame (E/silences/<stem>.envelope.json, written by silences.py). A frame is
    sound when it is louder than floor + margin dB. Cut edges are placed on this, never on the
    transcript alone: AssemblyAI ends sentence-final words 100-200 ms early, and silencedetect's
    -35 dB starts "silence" inside a quiet speaker's soft endings ("ch", "st", "n")."""

    def __init__(self, db: list[int], hop: float = 0.01, floor: float = -100.0, margin: float = 12.0,
                 hold: float = 0.1, search: float = 0.3, edge_margin: float | None = None):
        self.db, self.hop = db, hop
        self.thr = floor + margin
        # where a range's first/last word starts/ends: a gentler line than the pause line, so a soft
        # final "s" about 16 dB over the room still counts (2026-11-03 "websites" clipped at margin 20)
        self.edge_thr = floor + (margin if edge_margin is None else edge_margin)
        # speech level, not room hum or clicks: fixed at floor + 27 dB (the original 12 + 15), so
        # raising margin (where a fade ends) never makes the clipped-word ear check less sensitive
        self.loud = floor + 27
        self.hold = max(1, int(round(hold / hop)))
        self.search = search
        self.duration = len(db) * hop

    def _i(self, t: float) -> int:
        return min(max(int(round(t / self.hop)), 0), len(self.db))

    def _snd(self, i: int, edge: bool = False) -> bool:
        return 0 <= i < len(self.db) and self.db[i] > (self.edge_thr if edge else self.thr)

    def offset(self, t: float, limit: float, edge: bool = False) -> float:
        """Where the sound running through t fades out: the start of the first quiet run (>= hold)
        at or after t, at most `limit`. If t is already quiet, the last sound within `search`
        before t (the transcript ended the word late)."""
        i, end = self._i(t), self._i(limit)
        q = 0
        for j in range(i, end):
            if self._snd(j, edge):
                q = 0
            elif (q := q + 1) >= self.hold:
                k = j - self.hold + 1
                if k > i:
                    return k * self.hop
                for b in range(i - 1, max(i - int(self.search / self.hop), 0) - 1, -1):
                    if self._snd(b, edge):
                        return (b + 1) * self.hop
                return t
        return max(t, limit)

    def onset(self, t: float, limit: float, edge: bool = False) -> float:
        """Where the sound running into t starts: the end of the last quiet run (>= hold) at or
        before t, at least `limit`. If just before t is quiet, the first sound within `search`
        after t (the transcript started the word early)."""
        i, lo = self._i(t), self._i(limit)
        q = 0
        for j in range(i - 1, lo - 1, -1):
            if self._snd(j, edge):
                q = 0
            elif (q := q + 1) >= self.hold:
                k = j + self.hold  # first frame after the quiet run
                if k < i:
                    return k * self.hop
                for f in range(i, min(i + int(self.search / self.hop), len(self.db))):
                    if self._snd(f, edge):
                        return f * self.hop
                return t
        return min(t, limit)

    def quietest(self, a: float, b: float) -> float:
        """Time of the quietest frame in [a, b) (the latest one on a tie): where to cut when the
        sound runs straight from a dropped word into a kept one with no quiet run between."""
        i, j = self._i(a), self._i(b)
        if j <= i:
            return a
        k = min(range(i, j), key=lambda f: (self.db[f], -f))
        return k * self.hop

    def voiced(self, a: float, b: float, edge: bool = False) -> float:
        return sum(1 for j in range(self._i(a), self._i(b)) if self._snd(j, edge)) * self.hop

    def loud_span(self, a: float, b: float, min_s: float = 0.1):
        """(first, last) speech-level frame times in [a, b) when there are at least min_s of them."""
        idx = [j for j in range(self._i(a), self._i(b)) if self.db[j] > self.loud]
        if len(idx) * self.hop < min_s:
            return None
        return idx[0] * self.hop, (idx[-1] + 1) * self.hop

    def is_loud(self, a: float, b: float) -> bool:
        """Mean level over [a, b) at speech loudness."""
        fr = self.db[self._i(a):self._i(b)]
        return bool(fr) and sum(fr) / len(fr) > self.loud


def load_envelope(edit_dir: Path, stem: str, margin: float = 12.0, hold: float = 0.1,
                  edge_margin: float | None = None) -> Envelope | None:
    p = Path(edit_dir) / "silences" / f"{stem}.envelope.json"
    try:
        d = json.loads(p.read_text())
        return Envelope(d["db"], float(d.get("hop_s", 0.01)), float(d["floor_db"]), margin, hold,
                        edge_margin=edge_margin)
    except (OSError, ValueError, TypeError, KeyError):
        return None


def _natural(name: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def load_sources(edit_dir: Path) -> dict:
    """The clips of this run in join order: E/sources.json, else every video in the run dir
    (old single-clip runs). Returns {"order": [stems], "files": {stem: filename}, "plan": str|None}."""
    E = Path(edit_dir)
    p = E / "sources.json"
    if p.exists():
        try:
            d = json.loads(p.read_text())
            order = [str(x) for x in d.get("order", [])]
            files = {str(k): str(v) for k, v in (d.get("files") or {}).items()}
            inserts = {str(k): str(v) for k, v in (d.get("inserts") or {}).items()}
            if order:
                return {"order": order, "files": {**files, **inserts}, "plan": d.get("plan"),
                        "inserts": inserts}
        except (ValueError, AttributeError):
            pass
    run_dir = E.resolve().parent
    vids = []
    if run_dir.is_dir():
        vids = sorted((x for x in run_dir.iterdir() if x.is_file() and x.suffix.lower() in VIDEO_EXTS),
                      key=lambda x: _natural(x.name))
    return {"order": [v.stem for v in vids], "files": {v.stem: v.name for v in vids}, "plan": None}


def load_activity(edit_dir: Path, stem: str | None = None) -> dict | None:
    """E/activity/<stem>.json when it exists, else the old single-clip E/activity.json, else None."""
    E = Path(edit_dir)
    if stem:
        q = E / "activity" / f"{stem}.json"
        if q.exists():
            return json.loads(q.read_text())
    p = E / "activity.json"
    return json.loads(p.read_text()) if p.exists() else None


def activity_gaps(activity: dict | None, stem: str, only_source: bool) -> list[dict]:
    """Gaps belonging to `stem` (activity.json has one source; if only one stem exists it always applies)."""
    if not activity:
        return []
    src = Path(activity.get("source") or "").stem
    if only_source or src == stem or not src:
        return list(activity.get("gaps", []))
    return []


def find_source_file(edit_dir: Path, stem: str, activity: dict | None = None) -> str:
    named = load_sources(edit_dir)["files"].get(stem)
    if named:
        return named
    run_dir = Path(edit_dir).resolve().parent
    if run_dir.is_dir():
        for p in sorted(run_dir.iterdir()):
            if p.is_file() and p.stem == stem and p.suffix.lower() in VIDEO_EXTS:
                return p.name
    if activity and Path(activity.get("source") or "").stem == stem:
        return Path(activity["source"]).name
    return f"{stem}.mp4"


def source_duration(edit_dir: Path, stem: str, words: list[dict], only_source: bool = True) -> float | None:
    p = Path(edit_dir) / "probe.json"
    if p.exists():
        try:
            pj = json.loads(p.read_text())
            clip = (pj.get("clips") or {}).get(stem)
            if clip and clip.get("duration_s") is not None:
                return float(clip["duration_s"])
            if stem not in (load_sources(edit_dir).get("inserts") or {}) and (
                    only_source or Path(pj.get("path") or "").stem == stem):
                return float(pj["duration_s"])
        except (ValueError, KeyError, TypeError):
            pass
    ins = (load_sources(edit_dir).get("inserts") or {}).get(stem)
    if ins:  # an insert clip is not in probe.json: ask ffprobe
        f = Path(edit_dir).resolve().parent / ins
        try:
            out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                  "-of", "csv=p=0", str(f)], capture_output=True, text=True, timeout=60)
            return float(out.stdout.strip())
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    return words[-1]["end"] if words else None


# ---------------------------------------------------------------- flags
def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def flag_phrases(phrases: list[dict], lookback: int = 8) -> list[bool]:
    """True for phrases containing fillers, immediate n-gram repeats, or a 3-gram shared with a recent phrase (retake)."""
    flags = [False] * len(phrases)
    shingles: list[set] = []
    for i, p in enumerate(phrases):
        low = p["text"].lower()
        toks = _tokens(low)
        if FILLER_RE.search(low) or FILLER_MULTI_RE.search(low) or LIKE_RE.search(low):
            flags[i] = True
        for n in (1, 2, 3):
            for k in range(len(toks) - 2 * n + 1):
                if toks[k:k + n] == toks[k + n:k + 2 * n]:
                    flags[i] = True
        sh = {tuple(toks[k:k + 3]) for k in range(len(toks) - 2)}
        shingles.append(sh)
        for j in range(max(0, i - lookback), i):
            if sh & shingles[j]:
                flags[i] = flags[j] = True
    return flags


# ---------------------------------------------------------------- slides + camera covers
def load_slides(edit_dir: Path) -> dict[str, list[dict]]:
    """{stem: [{"t", "still"}]} from E/slides/<stem>.json (slides.py), still paths relative to E."""
    out: dict[str, list[dict]] = {}
    for f in sorted((Path(edit_dir) / "slides").glob("*.json")):
        try:
            d = json.loads(f.read_text())
            out[f.stem] = [{"t": float(x["t"]), "still": str(x["still"])} for x in d.get("slides", [])]
        except (ValueError, KeyError, TypeError, OSError):
            continue
    return out


def camera_covers(edit_dir: Path) -> dict[str, list[list[float]]]:
    """{screen stem: [[lo, hi], ...]} from an ok E/cam_sync.json, else {}."""
    try:
        d = json.loads((Path(edit_dir) / "cam_sync.json").read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(d, dict) or d.get("ok") is not True:
        return {}
    out: dict[str, list[list[float]]] = {}
    for c in d.get("cams") or []:
        if c.get("screen") and c.get("covers"):
            out.setdefault(c["screen"], []).append([float(c["covers"][0]), float(c["covers"][1])])
    return out


def uncovered(covers: list[list[float]], lo: float, hi: float, min_s: float = 1.0) -> list[tuple[float, float]]:
    """Stretches of [lo, hi] no camera covers, at least min_s long."""
    out, pos = [], lo
    for a, b in sorted(covers):
        if a > pos and min(a, hi) - pos >= min_s:
            out.append((pos, min(a, hi)))
        pos = max(pos, b)
        if pos >= hi:
            break
    if hi - pos >= min_s:
        out.append((pos, hi))
    return out


# ---------------------------------------------------------------- split
def plan_boundaries(total: float, target_s: float, candidates: list[float]) -> list[float]:
    """Boundaries [0, b1, ..., total]. Each b is the candidate nearest cur+target_s within [0.5T, 1.5T];
    falls back to cur+target_s itself when no candidate is in the window."""
    bounds = [0.0]
    cands = sorted(candidates)
    cur = 0.0
    while total - cur > target_s * 1.25:
        want = cur + target_s
        window = [c for c in cands if cur + 0.5 * target_s <= c <= cur + 1.5 * target_s and c < total]
        b = min(window, key=lambda c: abs(c - want)) if window else want
        bounds.append(round(b, 3))
        cur = b
    bounds.append(round(total, 3))
    return bounds


def _fmt(t: float) -> str:
    return f"{t:.2f}"


def split(edit_dir: Path, profile: dict, chunk_minutes: float | None = None) -> list[dict]:
    edit_dir = Path(edit_dir)
    takes_p = edit_dir / "takes_packed.md"
    if not takes_p.exists():
        raise SystemExit(f"missing {takes_p}")
    takes = parse_takes(takes_p.read_text())
    if not any(takes.values()):
        raise SystemExit(f"no phrases found in {takes_p}")
    gap_min = float(profile.get("detect", {}).get("gap_min_s", 1.0))
    minutes = chunk_minutes or float(profile.get("edit", {}).get("chunk_minutes", 15))
    target = minutes * 60.0
    out_dir = edit_dir / "chunks"
    out_dir.mkdir(exist_ok=True)
    for old in list(out_dir.glob("chunk_*.md")) + list(out_dir.glob("chunk_*.json")):
        old.unlink()
    srcs = load_sources(edit_dir)
    insert_stems = set(srcs.get("inserts") or {})
    if insert_stems & set(takes):
        # Rio 2.0 inserts: never chunked; the editor gets them whole in E/inserts_packed.md
        lines = ["# INSERT CLIPS (place pieces full-screen; source = the stem below; times are the clip's own)"]
        for st in sorted(insert_stems & set(takes)):
            lines.append(f"## {st} | {srcs['files'].get(st, st)}")
            lines += ["  " + ph["raw"] for ph in takes[st]]
        (edit_dir / "inserts_packed.md").write_text("\n".join(lines) + "\n")
        takes = {k: v for k, v in takes.items() if k not in insert_stems}
    only = len(takes) == 1
    index = 0
    metas: list[dict] = []
    order = [x for x in srcs["order"] if x in takes]
    order += [x for x in takes if x not in order]
    slides = load_slides(edit_dir)
    covers = camera_covers(edit_dir)
    for stem in order:
        phrases = takes[stem]
        if not phrases:
            continue
        words = load_words(edit_dir, stem)
        activity = load_activity(edit_dir, stem)
        gaps = activity_gaps(activity, stem, only)
        total = source_duration(edit_dir, stem, words, only) or phrases[-1]["end"]
        total = max(total, phrases[-1]["end"])
        cands = [(g["start"] + g["end"]) / 2 for g in gaps if g["end"] - g["start"] >= gap_min]
        cands += [(a["end"] + b["start"]) / 2 for a, b in zip(phrases, phrases[1:]) if b["start"] - a["end"] >= gap_min]
        if not cands:
            cands = [p["start"] for p in phrases]
        bounds = plan_boundaries(total, target, cands)
        flags = flag_phrases(phrases)
        wstarts = [w["start"] for w in words]
        fname = find_source_file(edit_dir, stem, activity)
        for b0, b1 in zip(bounds, bounds[1:]):
            index += 1
            items: list[tuple[float, str]] = []
            for p, fl in zip(phrases, flags):
                if not (b0 <= p["start"] < b1 or (b1 == bounds[-1] and p["start"] >= b1)):
                    continue
                block = "  " + p["raw"]
                if fl:
                    lo = bisect.bisect_left(wstarts, p["start"] - 0.02)
                    hi = bisect.bisect_right(wstarts, p["end"] + 0.02)
                    ws = " ".join(f"{w['text']}@{_fmt(w['start'])}-{_fmt(w['end'])}" for w in words[lo:hi])
                    block += f"\n    WORDS {ws}"
                items.append((p["start"], block))
            for g in gaps:
                if g["end"] > b0 and g["start"] < b1:
                    line = f"  ACTIVITY {_fmt(g['start'])}-{_fmt(g['end'])} {g.get('class')}"
                    mk = g.get("must_keep")
                    if mk:
                        line += f" keep={_fmt(mk[0])}-{_fmt(mk[1])}"
                    items.append((g["start"] + 1e-6, line))
            for sl in slides.get(stem, []):
                if b0 <= sl["t"] < b1:
                    items.append((sl["t"] - 1e-6, f"  SLIDE {_fmt(sl['t'])} still={sl['still']}"))
            if stem in covers:
                for a, b in uncovered(covers[stem], b0, b1):
                    items.append((a - 2e-6, f"  NO CAMERA {_fmt(a)}-{_fmt(b)}"))
            items.sort(key=lambda x: x[0])
            head = f"# chunk {index:02d} | {fname} | {_fmt(b0)}-{_fmt(b1)}"
            (out_dir / f"chunk_{index:02d}.md").write_text(head + "\n" + "\n".join(i[1] for i in items) + "\n")
            meta = {"index": index, "start": round(b0, 3), "end": round(b1, 3), "source": fname}
            (out_dir / f"chunk_{index:02d}.json").write_text(json.dumps(meta) + "\n")
            metas.append(meta)
    return metas


# ---------------------------------------------------------------- snapping
def _first_index(t: float, words: list[dict]):
    """(index of first included word, gap distance d to its start or None if mid-word)."""
    k = next((i for i, w in enumerate(words) if w["end"] > t), len(words))
    if k == len(words):
        return k, None
    w = words[k]
    if w["start"] >= t:
        return k, w["start"] - t
    return (k if t - w["start"] <= w["end"] - t else k + 1), None


def _last_index(t: float, words: list[dict]):
    j = -1
    for i, w in enumerate(words):
        if w["start"] < t:
            j = i
        else:
            break
    if j < 0:
        return -1, None
    w = words[j]
    if w["end"] <= t:
        return j, t - w["end"]
    return (j if w["end"] - t <= t - w["start"] else j - 1), None


def snap_range(start: float, end: float, words: list[dict], pad_min: float = 0.03, pad_max: float = 0.2,
               default_pad: float = DEFAULT_PAD_MS / 1000, keeps: list | None = None,
               duration: float | None = None):
    """Snap a [start,end] cut to word boundaries.

    * a boundary inside a word moves to the nearest word edge (the word is then kept or dropped whole);
    * the result sits pad outside the first/last kept word, pad within [pad_min, pad_max] and at most half
      the gap to the neighbouring word (so it never crosses into it);
    * a boundary already in a gap with pad_min <= d <= pad_max is left alone; with d > pad_max it is pulled
      in to pad_max unless that would cut into a `keeps` span (must_keep of non-static gaps).
    Returns (start, end, notes) or None if the range contained words and none survive.
    """
    keeps = keeps or []
    notes: list[str] = []
    overlaps = any(w["end"] > start and w["start"] < end for w in words)
    if not overlaps:
        s, e = max(0.0, start), end if duration is None else min(end, duration)
        return s, e, notes
    first, d0 = _first_index(start, words)
    last, d1 = _last_index(end, words)
    if first >= len(words) or last < 0 or first > last:
        return None
    # start
    wf = words[first]
    prev_end = words[first - 1]["end"] if first > 0 else 0.0
    avail = max(0.0, wf["start"] - prev_end)
    if d0 is not None and d0 >= pad_min:
        if d0 <= pad_max:
            ns = start
        else:
            kmin = min([k[0] for k in keeps if k[1] > start and k[0] < wf["start"]], default=float("inf"))
            ns = max(start, min(wf["start"] - pad_max, kmin))
    else:
        ns = wf["start"] - min(default_pad, avail / 2)
        if d0 is None:
            notes.append(f"start {start:.3f} snapped to word edge")
    # end
    wl = words[last]
    next_start = words[last + 1]["start"] if last + 1 < len(words) else (duration if duration is not None else wl["end"] + 1.0)
    avail = max(0.0, next_start - wl["end"])
    if d1 is not None and d1 >= pad_min:
        if d1 <= pad_max:
            ne = end
        else:
            kmax = max([k[1] for k in keeps if k[0] < end and k[1] > wl["end"]], default=float("-inf"))
            ne = min(end, max(wl["end"] + pad_max, kmax))
    else:
        ne = wl["end"] + min(default_pad, avail / 2)
        if d1 is None:
            notes.append(f"end {end:.3f} snapped to word edge")
    ns = max(0.0, ns)
    if duration is not None:
        ne = min(ne, duration)
    if ne <= ns:
        return None
    return round(ns, 3), round(ne, 3), notes


# ---------------------------------------------------------------- silence compaction
# Clips are named <video name><role> ("setterintro", "settermain1") or role-first ("intro").
# Keep in sync with intake.py ROLE_RE.
ROLE_RE = re.compile(r"^(?P<prefix>.*?)[ _.-]*(?P<role>intro|outro|main)(?P<n>\d*)$", re.I)


def is_intro_stem(stem: str) -> bool:
    s = stem.strip()
    m = ROLE_RE.match(s)
    return (m.group("role").lower() == "intro") if m else s.lower().startswith("intro")


def silence_settings(profile: dict, edit_dir: Path | None = None) -> dict:
    """The edit's silence policy: "cut_all" (default: every pause shrinks to the join air, screen
    activity or not) or "keep_active" (the original rule: active must_keep spans are protected).
    With edit_dir, also notes the run's intro clips (stems whose role is intro, e.g. "setterintro"): when there are
    any, only they get intro pacing, whatever beat labels the editor wrote."""
    e = profile.get("edit", {})
    ev = e.get("envelope", {})
    intro_stems = []
    silences: dict[str, list[list[float]]] = {}
    envs: dict[str, Envelope] = {}
    if edit_dir is not None:
        srcs = load_sources(edit_dir)
        order = srcs["order"]
        intro_stems = [s for s in order if is_intro_stem(s)]
        silences = {s: v for s in order if (v := load_silences(edit_dir, s))}
        order = order + [s for s in (srcs.get("inserts") or {}) if s not in order]  # envelopes: inserts too
        em = ev.get("edge_margin_db")
        envs = {s: v for s in order if (v := load_envelope(edit_dir, s, float(ev.get("margin_db", 12)),
                                                           ev.get("hold_ms", 100) / 1000,
                                                           None if em is None else float(em)))}
    air = e.get("air_ms", {"intro": 30, "body": 120})
    thr = e.get("compact_gap_ms", {"intro": 150, "body": 300})
    tail = e.get("tail_ms", {"intro": 60, "body": 200})
    lead = e.get("lead_ms", {"intro": 40, "body": 80})
    return {"policy": e.get("silence_policy", "cut_all"),
            "air": {k: v / 1000 for k, v in air.items()},
            "thr": {k: v / 1000 for k, v in thr.items()},
            "tail": {k: v / 1000 for k, v in tail.items()},
            "lead": {k: v / 1000 for k, v in lead.items()},
            "max_tail": ev.get("max_tail_ms", 400) / 1000,
            "max_lead": ev.get("max_lead_ms", 300) / 1000,
            "intro_stems": intro_stems, "silences": silences, "envelopes": envs}


def section(r: dict, intro_stems: list[str] | None = None) -> str:
    """intro only where the editor labelled the hook: beat starting with INTRO (user, 2026-10-29:
    "the hook is short" - an intro clip can run 20+ min, so the clip name alone never makes a range
    punchy). When the run has separate intro clips (`intro_stems`), INTRO beats count only inside
    them, so a stray INTRO label in a main clip stays body."""
    if not str(r.get("beat", "")).strip().upper().startswith("INTRO"):
        return "body"
    return "intro" if not intro_stems or r.get("source") in intro_stems else "body"


SPEECH_GUARD = 0.12   # never cut within this long after a word's start
MIN_PIECE = 0.08      # kept pieces shorter than this are slivers and are dropped


def _in_range(ws: list[dict], lo: float, hi: float) -> list[dict]:
    return [w for w in ws if w["start"] >= lo - EPS and w["end"] <= hi + EPS]


NEIGHBOUR_GAP = 0.04  # never extend an edge closer than this to a word the range does not keep
SQUASHED_S = 0.045    # words this short come in runs where AssemblyAI lost the alignment


def _squashed(ws: list[dict], n: int = 3) -> bool:
    tail = ws[-n:]
    return len(tail) == n and all(w["end"] - w["start"] <= SQUASHED_S for w in tail)


def end_fade_if_clipped(env: Envelope, t: float, last_end: float, next_start: float):
    """THE definition of "this range end cuts a word short", shared by the merge/refit (heal_edges,
    _env_edges) and by verify_edit's clipped_words (clipped_edges). Returns the time the sound
    really fades when the end `t` sits in speech-level sound that carries on past it, with room
    before the next (dropped) word; otherwise None."""
    off = env.offset(max(last_end, t - 0.03), next_start)
    if env.is_loud(t - 0.03, t) and off > t + 0.02 and off < next_start - NEIGHBOUR_GAP:
        return off
    return None


def start_onset_if_clipped(env: Envelope, t: float, first_start: float, prev_end: float):
    """Mirror of end_fade_if_clipped for a range start: the real onset when `t` is inside a word."""
    on = env.onset(min(first_start, t + 0.03), prev_end)
    if env.is_loud(t, t + 0.03) and on < t - 0.02 and on > prev_end + NEIGHBOUR_GAP:
        return on
    return None


def _env_edges(r: dict, ws: list[dict], allw: list[dict], env: Envelope, settings: dict, sec: str,
               silences: list[list[float]]):
    """Range edges on the audio: lead before the real onset of the first kept word, tail after
    the real fade of the last one, never into a neighbouring (dropped) word."""
    tail, lead = settings["tail"][sec], settings["lead"][sec]
    i0 = next(i for i, w in enumerate(allw) if w is ws[0])
    i1 = next(i for i, w in enumerate(allw) if w is ws[-1])
    w0, w1 = ws[0], ws[-1]
    prev_end = allw[i0 - 1]["end"] if i0 > 0 else 0.0
    next_start = allw[i1 + 1]["start"] if i1 + 1 < len(allw) else env.duration
    lo_lim = max(w0["start"] - settings["max_lead"], min(w0["start"], prev_end + NEIGHBOUR_GAP), 0.0)
    hi_lim = min(w1["end"] + settings["max_tail"], max(w1["end"], next_start - NEIGHBOUR_GAP), env.duration)
    scan_from, notes = w1["end"], []
    if _squashed(ws):
        # the real words run on past the transcript's times: follow the speech to the next silence
        nxt = next((s for s, e in silences if s >= w1["end"] - EPS), next_start)
        scan_from = max(w1["end"], min(nxt, next_start - NEIGHBOUR_GAP))
        hi_lim = max(hi_lim, min(scan_from + settings["max_tail"], next_start - NEIGHBOUR_GAP, env.duration))
        notes.append(f"{r['start']}-{r['end']}: squashed word timings, end followed the audio to {scan_from:.2f}")
    on0 = env.onset(w0["start"], lo_lim, edge=True)
    lo = max(lo_lim, on0 - lead)
    if on0 <= lo_lim + EPS and prev_end < w0["start"] and prev_end + NEIGHBOUR_GAP > w0["start"] - settings["max_lead"]:
        # no quiet between a dropped word and the first kept one: the sound before the onset is
        # the dropped word's tail (the 2026-11-03 "my... and I can see" join). Cut at the dip.
        lo = max(lo_lim, env.quietest(prev_end, w0["start"]))
        notes.append(f"{r['start']}-{r['end']}: no quiet before '{w0['text']}', start at the dip {lo:.2f}")
    off1 = env.offset(scan_from, hi_lim, edge=True)
    hi = min(hi_lim, off1 + tail)
    if (not _squashed(ws) and off1 >= hi_lim - EPS and next_start > w1["end"]
            and next_start - NEIGHBOUR_GAP < w1["end"] + settings["max_tail"]):
        # mirror: the sound runs straight on into a dropped word; cut at the dip between them
        hi = min(hi_lim, max(w1["end"], env.quietest(w1["end"], next_start)))
        notes.append(f"{r['start']}-{r['end']}: no quiet after '{w1['text']}', end at the dip {hi:.2f}")
    # never shrink a range across speech it deliberately covers (words the transcript missed, or a
    # verifier fix that extended it): follow that speech, still clear of the neighbouring words
    lim_hi = min(next_start - NEIGHBOUR_GAP, env.duration)
    sp = env.loud_span(hi, min(r["end"], lim_hi))
    if sp:
        hi = max(hi, min(lim_hi, env.offset(sp[1], lim_hi) + tail))
    lim_lo = max(prev_end + NEIGHBOUR_GAP, 0.0)
    sp = env.loud_span(max(r["start"], lim_lo), lo)
    if sp:
        lo = min(lo, max(lim_lo, env.onset(sp[0], lim_lo) - lead))
    # last word: whatever the limits above did, the result must pass the ear check (clipped_words)
    off = end_fade_if_clipped(env, hi, w1["end"], next_start)
    if off is not None:
        hi = min(lim_hi, off + tail)
    on = start_onset_if_clipped(env, lo, w0["start"], prev_end)
    if on is not None:
        lo = max(lim_lo, on - lead)
    return lo, hi, notes


def _held(r: dict, ws: list[dict], cuts: list) -> list:
    """hold_edges ranges (a user's revise note set the outer edges): keep the lead-in before the first
    word and the tail after the last one (a hissy "T" or "s" the voice-band envelope can't see);
    pauses between words are still cut."""
    if not r.get("hold_edges") or not ws:
        return cuts
    return [c for c in cuts if c[0] > ws[0]["start"] - EPS and c[1] < ws[-1]["end"] + EPS]


def _env_pause_cuts(ws: list[dict], lo: float, hi: float, env: Envelope, settings: dict, sec: str,
                    silences: list[list[float]]) -> list[list[float]]:
    """The pauses inside [lo, hi] to cut, measured on the audio: word gaps and detected silences
    whose real quiet (sound fade to next onset) is longer than the threshold (and than tail + lead)
    become [fade + tail, onset - lead]. Merged, sorted, clipped to [lo, hi]."""
    tail, lead = settings["tail"][sec], settings["lead"][sec]
    min_quiet = max(settings["thr"][sec], tail + lead + 0.05)
    cands = [(a["end"], b["start"]) for a, b in zip(ws, ws[1:]) if b["start"] - a["end"] > 0.05]
    cands += [(s, e) for s, e in silences if e > lo and s < hi]
    cuts = []
    for s, e in cands:
        qs = env.offset(s, min(e, s + settings["max_tail"]))
        qe = env.onset(e, max(qs, e - settings["max_lead"]))
        if qe - qs >= min_quiet:
            cuts.append((qs + tail, qe - lead))
    merged: list[list[float]] = []
    for a, b in sorted((max(a, lo), min(b, hi)) for a, b in cuts):
        if b <= a:
            continue
        if merged and a <= merged[-1][1] + EPS:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return merged


def _holds_word_sound(env: Envelope, a: float, b: float, w: dict) -> bool:
    """True when [a, b] holds most (>= 60 %) of a word's real sound, even though the transcript's
    word times spill past it. The transcriber stretches words so they touch, so a word like
    "system" can be timed from 20.77 s while its sound is 21.1-21.6 s; a piece holding that sound
    is the word, never a scrap."""
    if w["end"] <= a or w["start"] >= b or not str(w.get("text", "")).strip():
        return False
    total = env.voiced(w["start"], w["end"])
    if total < 0.1:
        return False
    return env.voiced(max(a, w["start"]), min(b, w["end"])) >= 0.6 * total


def _compact_env(r: dict, allw: list[dict], env: Envelope, settings: dict, sec: str):
    """compact_ranges for one range when the clip has a loudness envelope. Returns (pieces, notes)."""
    silences = (settings.get("silences") or {}).get(r["source"], [])
    ws = [w for w in allw if r["start"] - EPS <= (w["start"] + w["end"]) / 2 <= r["end"] + EPS]
    if not ws:
        return [], [f"dropped wordless range {r['start']}-{r['end']} ({r.get('beat', '')}): silence"]
    lo, hi, notes = _env_edges(r, ws, allw, env, settings, sec, silences)
    if r.get("hold_edges"):  # outer edges set on purpose (a user's revise note): pauses inside still cut
        lo, hi = float(r["start"]), float(r["end"])
    pieces, pos = [], lo
    for a, b in _held(r, ws, _env_pause_cuts(ws, lo, hi, env, settings, sec, silences)) + [[hi, hi]]:
        if a - pos >= MIN_PIECE:
            pieces.append((pos, a))
        pos = b
    out = []
    for a, b in pieces:
        whole = any(a - EPS <= w["start"] and w["end"] <= b + EPS for w in ws) or \
            any(_holds_word_sound(env, a, b, w) for w in ws)
        v = env.voiced(a, b)
        if v < MIN_PIECE or (not whole and (v < 0.25 or env.loud_span(a, b) is None)):
            notes.append(f"dropped a {b - a:.2f}s scrap at {a:.2f} ({r['source']}): no whole word, {v:.2f}s of sound")
            continue
        out.append({**r, "start": round(a, 3), "end": round(b, 3)})
    return out, notes


def compact_ranges(ranges: list[dict], words: dict[str, list[dict]], settings: dict):
    """Cut the pauses out of kept ranges (silence policy cut_all).

    Two sources of cuts inside each range, merged so cuts never overlap:
    1. word gaps: any word-to-word gap longer than the section threshold is cut, leaving half
       the join air after the earlier word and half before the later one;
    2. detected silences (settings["silences"][stem], from ffmpeg silencedetect; transcribers
       such as AssemblyAI stretch word times so words touch and hide pauses): any silence longer
       than the threshold is cut to [s + air/2, e - air/2]. A silence covering a range edge pulls
       that edge in to half air from the speech.
    Speech guard: a silence that starts inside a word (the word was stretched, or a held sound)
    is never cut within the first 120 ms after that word's start (cut start >= word start + 0.12).
    Range edges are pulled in to half the air from their outer words (intro 30 ms, body 120 ms
    by default). Kept pieces under 80 ms are dropped. Ranges flagged "keep_silence" (a silent
    demo the speaker announced) are left untouched. Ranges with no words are dropped.

    When the clip has a loudness envelope (settings["envelopes"][stem]) all of the above is
    measured on the audio instead (_compact_env): edges sit `lead` before the real onset and
    `tail` after the real fade (never into a dropped neighbouring word), pauses are cut when
    their real quiet is over the threshold, and scraps with no whole word and under 0.25 s of
    sound (a broken-off "oop") are dropped.
    Returns (ranges, notes)."""
    out: list[dict] = []
    notes: list[str] = []
    envs = settings.get("envelopes") or {}
    for r in ranges:
        if r.get("keep_silence"):
            out.append(r)
            continue
        sec = section(r, settings.get("intro_stems"))
        if r["source"] in envs:
            got, n = _compact_env(r, words.get(r["source"], []), envs[r["source"]], settings, sec)
            notes += n
            for p in got:  # extended edges of neighbouring ranges must not overlap
                q = out[-1] if out else None
                if q and q["source"] == p["source"] and p["start"] < q["end"] + EPS and p["end"] > q["start"]:
                    q["end"] = max(q["end"], p["end"])
                else:
                    out.append(p)
            continue
        air, thr = settings["air"][sec], settings["thr"][sec]
        half = air / 2
        allw = words.get(r["source"], [])
        ws = _in_range(allw, r["start"], r["end"])
        if not ws:
            notes.append(f"dropped wordless range {r['start']}-{r['end']} ({r.get('beat', '')}): silence")
            continue
        lo = max(r["start"], ws[0]["start"] - half)
        hi = min(r["end"], ws[-1]["end"] + half)
        cuts = [(a["end"] + half, b["start"] - half) for a, b in zip(ws, ws[1:]) if b["start"] - a["end"] > thr]
        for s, e in (settings.get("silences") or {}).get(r["source"], []):
            if e - s <= thr or e <= lo or s >= hi:
                continue
            cs, ce_ = s + half, e - half
            for w in allw:  # speech guard
                if w["start"] < s < w["end"]:
                    cs = max(cs, w["start"] + SPEECH_GUARD)
                    break
            cuts.append((cs, ce_))
        merged_cuts: list[list[float]] = []
        for a, b in sorted((max(a, lo), min(b, hi)) for a, b in cuts):
            if b <= a:
                continue
            if merged_cuts and a <= merged_cuts[-1][1] + EPS:
                merged_cuts[-1][1] = max(merged_cuts[-1][1], b)
            else:
                merged_cuts.append([a, b])
        pos = lo
        for a, b in merged_cuts + [[hi, hi]]:
            if a - pos >= MIN_PIECE:
                out.append({**r, "start": round(pos, 3), "end": round(a, 3)})
            pos = b
    return out, notes


HEAL_WORD_MS = 0.06   # an edge this close to the inside of a dropped word's edge is rounded out of it


def heal_edges(ranges: list[dict], words: dict[str, list[dict]], settings: dict):
    """Make every edge pass the ear checks, only ever by moving it to better audio (used by
    `validate --refit` on ALL ranges and by merge after compaction):
    1. word edges: a start a few ms inside the tail of a word the range does not keep (or an end a
       few ms inside the head of one) moves to that word's edge, so no edge sits inside a word;
    2. clipped words: an end still in audible speech grows to the real fade + tail, a start to
       the real onset - lead, never closer than NEIGHBOUR_GAP to a dropped neighbouring word.
    Step 2 only extends, so a fix that added audio is never undone. Ranges the speaker marked
    keep_silence are skipped. Returns (ranges, notes)."""
    envs = settings.get("envelopes") or {}
    out, notes = [], []
    for r in ranges:
        r = dict(r)
        allw = words.get(r["source"], [])
        if r.get("keep_silence") or r.get("hold_edges") or not allw:
            out.append(r)
            continue
        ks =[k for k, w in enumerate(allw) if r["start"] - EPS <= (w["start"] + w["end"]) / 2 <= r["end"] + EPS]
        for w in allw:  # 1. word edges
            mid = (w["start"] + w["end"]) / 2
            if mid < r["start"] and r["start"] < w["end"] - EPS and w["end"] - r["start"] <= HEAL_WORD_MS:
                notes.append(f"start {r['start']} was {1000 * (w['end'] - r['start']):.0f} ms inside '{w['text']}': moved to {w['end']}")
                r["start"] = w["end"]
            elif mid > r["end"] and r["end"] > w["start"] + EPS and r["end"] - w["start"] <= HEAL_WORD_MS:
                notes.append(f"end {r['end']} was {1000 * (r['end'] - w['start']):.0f} ms inside '{w['text']}': moved to {w['start']}")
                r["end"] = w["start"]
        env = envs.get(r["source"])
        if env is not None and ks:
            sec = section(r, settings.get("intro_stems"))
            tail, lead = settings["tail"][sec], settings["lead"][sec]
            w0, w1 = allw[ks[0]], allw[ks[-1]]
            prv = allw[ks[0] - 1]["end"] if ks[0] > 0 else 0.0
            nxt = allw[ks[-1] + 1]["start"] if ks[-1] + 1 < len(allw) else env.duration
            off = end_fade_if_clipped(env, r["end"], w1["end"], nxt)
            if off is not None:
                new = round(min(max(r["end"], min(nxt - NEIGHBOUR_GAP, env.duration)), off + tail), 3)
                if new > r["end"]:
                    notes.append(f"end {r['end']} cut '{w1['text']}' while still audible: extended to {new}")
                    r["end"] = new
            on = start_onset_if_clipped(env, r["start"], w0["start"], prv)
            if on is not None:
                new = round(max(min(r["start"], prv + NEIGHBOUR_GAP), on - lead), 3)
                if new < r["start"]:
                    notes.append(f"start {r['start']} cut '{w0['text']}' while still audible: extended to {new}")
                    r["start"] = new
        out.append(r)
    return out, notes


def _moved(r: dict):
    """The source time a moved range belongs after (`move_after`), or None for a normal range."""
    v = r.get("move_after")
    return None if v is None else round(float(v), 3)


def place_moved(ranges: list[dict]) -> tuple[list[dict], list[str]]:
    """Move ranges marked `move_after: T` (source seconds) out of recording order: the speaker
    recorded a section late and says where it belongs. Each group (same source and T) keeps its
    own order and goes right after the last normal range of that source starting before T."""
    moved = [r for r in ranges if _moved(r) is not None and not r.get("insert")]
    if not moved:
        return ranges, []
    rest = [r for r in ranges if not (_moved(r) is not None and not r.get("insert"))]
    groups: dict[tuple[str, float], list[dict]] = {}
    for r in moved:
        groups.setdefault((r["source"], _moved(r)), []).append(r)
    notes, places = [], []
    for (src, t), g in groups.items():
        idx = 0
        for i, r in enumerate(rest):
            if r["source"] == src and not r.get("insert") and r["start"] < t:
                idx = i + 1
        places.append((idx, src, t, sorted(g, key=lambda r: (r["start"], r["end"]))))
    for idx, src, t, g in sorted(places, key=lambda x: -x[0]):
        rest[idx:idx] = g
        notes.append(f"moved {len(g)} range(s) {g[0]['start']}-{g[-1]['end']} of {src} to after source {t}")
    return rest, notes


def _layout(r: dict) -> str:
    return "full" if r.get("insert") else ("face" if r.get("layout") == "face" else "split")


def _silent(r: dict) -> bool:
    """A silent demo the speaker announced is never joined into speech (it would lose its flag)."""
    return bool(r.get("keep_silence"))


def _trim_into_demos(ranges: list[dict]):
    """Speech that runs into a neighbouring silent demo (same clip) is cut back to the demo's edge:
    the demo already plays that audio. A speech scrap under MIN_PIECE left over is dropped."""
    out, notes = [dict(r) for r in ranges], []
    for i, r in enumerate(out):
        if not _silent(r):
            continue
        for j in (i - 1, i + 1):
            if not 0 <= j < len(out) or _silent(out[j]) or out[j]["source"] != r["source"]:
                continue
            q = out[j]
            if j < i and q["end"] > r["start"]:
                notes.append(f"speech end {q['end']} cut back to the silent demo at {r['start']}")
                q["end"] = r["start"]
            elif j > i and q["start"] < r["end"]:
                notes.append(f"speech start {q['start']} moved to the silent demo's end {r['end']}")
                q["start"] = r["end"]
    return [r for r in out if r["end"] - r["start"] >= MIN_PIECE], notes


def _join_touching(ranges: list[dict], order: dict | None = None) -> list[dict]:
    """Sort main ranges by (clip order, start) and join touching ones of the same clip and layout.
    Insert ranges ("insert": true) stay where they are: the main ranges are sorted only within
    the blocks between inserts, so a placed testimonial never moves (Rio 2.0 inserts)."""
    order = order or {}
    blocks: list[list[dict]] = [[]]
    for r in ranges:
        if r.get("insert"):
            blocks.append([r])
            blocks.append([])
        else:
            # a moved block (move_after) is its own block: sorted within, never against the rest
            if blocks[-1] and _moved(blocks[-1][-1]) != _moved(r):
                blocks.append([])
            blocks[-1].append(r)
    merged: list[dict] = []
    for b in blocks:
        if len(b) == 1 and b[0].get("insert"):
            merged.append(dict(b[0]))
            continue
        b = sorted(b, key=lambda r: (order.get(r["source"], len(order)), r["start"], r["end"]))
        start_len = len(merged)
        for r in b:
            q = merged[-1] if len(merged) > start_len else None
            if (q and q["source"] == r["source"] and r["start"] <= q["end"] + 0.001 and _layout(q) == _layout(r)
                    and _silent(q) == _silent(r)):
                q["end"] = max(q["end"], r["end"])
            else:
                merged.append(dict(r))
    return merged


def compact_insert(r: dict, allw: list[dict], max_pause: float, air: float) -> list[dict]:
    """Light touch for an insert clip (a testimonial): cut only word gaps over max_pause, leaving
    `air` each side; fillers and short pauses stay. Edges sit `air` outside the outer words."""
    ws = [w for w in allw if r["start"] - EPS <= (w["start"] + w["end"]) / 2 <= r["end"] + EPS]
    if not ws:
        return []
    lo = max(float(r["start"]), ws[0]["start"] - air)
    hi = min(float(r["end"]), ws[-1]["end"] + air) if float(r["end"]) >= ws[-1]["end"] else float(r["end"])
    lo = min(lo, ws[0]["start"])
    hi = max(hi, ws[-1]["end"])
    out, pos = [], lo
    for a, b in zip(ws, ws[1:]):
        if b["start"] - a["end"] > max_pause:
            out.append({**r, "start": round(pos, 3), "end": round(a["end"] + air, 3)})
            pos = b["start"] - air
    out.append({**r, "start": round(pos, 3), "end": round(hi, 3)})
    return [x for x in out if x["end"] - x["start"] > 0.05]


def smooth_layouts(ranges: list[dict], min_face_s: float = 6.0, min_split_s: float = 2.0) -> list[str]:
    """In place: a face run (consecutive face ranges, on screen) shorter than min_face_s becomes
    split; then a split run shorter than min_split_s between two face runs becomes face (no
    flicker). Inserts end a run. Returns notes."""
    notes: list[str] = []

    def runs():
        out, cur = [], None
        for i, r in enumerate(ranges):
            k = _layout(r)
            if cur and cur[0] == k:
                cur[2] = i
                cur[3] += r["end"] - r["start"]
            else:
                cur = [k, i, i, r["end"] - r["start"]]
                out.append(cur)
        return out
    for k, i, j, d in runs():
        if k == "face" and d < min_face_s:
            for r in ranges[i:j + 1]:
                r["layout"] = "split"
            notes.append(f"layout: face run of {d:.1f}s at range {i} is under {min_face_s}s -> split")
    rs = runs()
    for n, (k, i, j, d) in enumerate(rs):
        if k == "split" and 0 < n < len(rs) - 1 and rs[n - 1][0] == "face" and rs[n + 1][0] == "face" and d < min_split_s:
            for r in ranges[i:j + 1]:
                r["layout"] = "face"
            notes.append(f"layout: split blip of {d:.1f}s at range {i} between face runs -> face")
    return notes


def long_silences(ranges: list[dict], words: dict[str, list[dict]], settings: dict) -> list[dict]:
    """Pauses longer than the section threshold left inside kept ranges (keep_silence ranges
    excepted): word gaps, plus detected silences (settings["silences"]) whose part inside the
    range is still over the threshold. Each report is {"range_index", "start", "end"}.
    With a loudness envelope for the clip: the pauses compact_ranges would still cut there."""
    found = []
    envs = settings.get("envelopes") or {}
    for i, r in enumerate(ranges):
        if r.get("keep_silence") or r.get("insert"):
            continue
        sec = section(r, settings.get("intro_stems"))
        if r["source"] in envs:
            ws = [w for w in words.get(r["source"], []) if r["start"] - EPS <= (w["start"] + w["end"]) / 2 <= r["end"] + EPS]
            sil = (settings.get("silences") or {}).get(r["source"], [])
            env = envs[r["source"]]
            for a, b in _held(r, ws, _env_pause_cuts(ws, r["start"], r["end"], env, settings, sec, sil)):
                if b - a <= 0.02:
                    continue
                if ws and b >= r["end"] - EPS and r["end"] <= env.offset(ws[-1]["end"], r["end"], edge=True) + settings["tail"][sec] + 0.05:
                    continue  # the last word's soft ending plus its tail, not a pause
                if ws and a <= r["start"] + EPS and r["start"] >= env.onset(ws[0]["start"], r["start"], edge=True) - settings["lead"][sec] - 0.05:
                    continue  # the lead before the first word's soft onset, not a pause
                found.append({"range_index": i, "start": round(a, 3), "end": round(b, 3)})
            continue
        thr = settings["thr"][sec]
        # words overlapping the range (a word straddling an edge counts as speech up to the edge)
        ws = [w for w in words.get(r["source"], []) if w["end"] > r["start"] and w["start"] < r["end"]]
        lead = [(r["start"], ws[0]["start"])] if ws else [(r["start"], r["end"])]
        tail = [(ws[-1]["end"], r["end"])] if ws else []
        mine = []
        for a, b in lead + [(x["end"], y["start"]) for x, y in zip(ws, ws[1:])] + tail:
            if b - a > thr + EPS:
                mine.append({"range_index": i, "start": round(a, 3), "end": round(b, 3)})
        for s, e in (settings.get("silences") or {}).get(r["source"], []):
            if e - s <= thr:
                continue
            a, b = max(s, r["start"]), min(e, r["end"])
            if b - a > thr + EPS and not any(a < m["end"] and b > m["start"] for m in mine):
                mine.append({"range_index": i, "start": round(a, 3), "end": round(b, 3)})
        found += mine
    return found


def refit_ranges(old: list[dict], words: dict[str, list[dict]], sil: dict, stems: list[str]):
    """The `validate --refit` path as a function (also used by revise.py). Only ranges that now hold
    a pause over the threshold are re-cut: compacting an already compacted range again is not
    idempotent with stretched word timings (the 2026-10-29 1-hour run moved 70 edges from inside
    silences into words). Then heal_edges on all ranges and join touching ones.
    Returns (ranges, notes)."""
    dirty = {p["range_index"] for p in long_silences(old, words, sil)}
    ranges, notes = [], []
    for i, r in enumerate(old):
        if i in dirty and not r.get("insert"):
            got, n = compact_ranges([r], words, sil)
            ranges += got
            notes += n
        else:
            ranges.append(r)
    ranges, n = heal_edges(ranges, words, sil)
    notes += n
    return _join_touching(ranges, {s: i for i, s in enumerate(stems)}), notes


def clipped_edges(ranges: list[dict], words: dict[str, list[dict]], settings: dict) -> list[dict]:
    """Range edges that cut a kept word short (needs the clips' loudness envelopes): the end of a
    range comes before its last word has faded out, although the sound does fade before the next
    (dropped) word, so there was room to cut cleanly (the mirror for a start before the first
    word's onset). The edge must also sit in speech-level sound. A join inside continuous
    speech (a filler cut out of "so uh the") is not clipping; the verifier listens to those.
    Each report is {"range_index", "edge": "start"|"end", "time", "word"}."""
    envs = settings.get("envelopes") or {}
    found = []
    for i, r in enumerate(ranges):
        env = envs.get(r["source"])
        if env is None or r.get("keep_silence"):
            continue
        allw = words.get(r["source"], [])
        ks = [k for k, w in enumerate(allw) if r["start"] - EPS <= (w["start"] + w["end"]) / 2 <= r["end"] + EPS]
        if not ks:
            continue
        w0, w1 = allw[ks[0]], allw[ks[-1]]
        prv = allw[ks[0] - 1]["end"] if ks[0] > 0 else 0.0
        nxt = allw[ks[-1] + 1]["start"] if ks[-1] + 1 < len(allw) else env.duration
        t = r["end"]
        if end_fade_if_clipped(env, t, w1["end"], nxt) is not None:
            found.append({"range_index": i, "edge": "end", "time": round(t, 3), "word": w1["text"]})
        t = r["start"]
        if start_onset_if_clipped(env, t, w0["start"], prv) is not None:
            found.append({"range_index": i, "edge": "start", "time": round(t, 3), "word": w0["text"]})
    return found


def cuts_in_sound(ranges: list[dict], settings: dict, probe_s: float = 0.03, run_s: float = 0.08) -> list[dict]:
    """The independent ear check (2026-11-03): range edges where the audio is still sounding (on the
    gentle edge line) AND the cut-away side keeps sounding for at least run_s. No word timings, no
    max_tail or neighbour limits, so the merge's own blind spots can't hide a clipped word here.
    A filler cut out of running speech lands here too, by necessity: the list is for review.
    Each report is {"range_index", "edge", "time"}."""
    envs = settings.get("envelopes") or {}
    found = []
    for i, r in enumerate(ranges):
        env = envs.get(r["source"])
        if env is None or r.get("keep_silence"):
            continue
        t = r["end"]
        if env.voiced(t - probe_s, t, edge=True) >= probe_s * 0.67 and env.voiced(t, t + run_s + 0.02, edge=True) >= run_s:
            found.append({"range_index": i, "edge": "end", "time": round(t, 3)})
        t = r["start"]
        if env.voiced(t, t + probe_s, edge=True) >= probe_s * 0.67 and env.voiced(t - run_s - 0.02, t, edge=True) >= run_s:
            found.append({"range_index": i, "edge": "start", "time": round(t, 3)})
    return found


# ---------------------------------------------------------------- merge / validate
def validate_edl(edl: dict, durations: dict[str, float | None], activity: dict | None,
                 tol: float = EPS, activities: dict[str, dict | None] | None = None) -> list[str]:
    v: list[str] = []
    ranges = edl.get("ranges", [])
    sources = edl.get("sources", {})
    if not ranges:
        v.append("edl has no ranges")
    last: dict[tuple, dict] = {}
    for i, r in enumerate(ranges):
        src = r.get("source")
        tag = f"range {i} [{r.get('start')}-{r.get('end')}]"
        if src not in sources:
            v.append(f"{tag}: unknown source {src!r}")
        s, e = r.get("start"), r.get("end")
        if not isinstance(s, (int, float)) or not isinstance(e, (int, float)):
            v.append(f"{tag}: start/end not numbers")
            continue
        if e <= s:
            v.append(f"{tag}: end <= start")
        if r.get("layout") not in (None, "split", "face"):
            v.append(f"{tag}: layout must be split or face, not {r.get('layout')!r}")
        if r.get("insert") and src not in (edl.get("inserts") or [src]):
            v.append(f"{tag}: insert range from {src!r}, which is not an insert clip")
        if s < -tol:
            v.append(f"{tag}: starts before 0")
        dur = durations.get(src)
        if dur is not None and e > dur + tol:
            v.append(f"{tag}: ends after source duration {dur:.3f}")
        if r.get("insert"):   # inserts are cherry-picked in story order, not the clip's own order
            continue
        if _moved(r) is not None:   # a moved section: in order within its block, overlapping nothing
            for j, q in enumerate(ranges):
                if (j != i and q.get("source") == src and _moved(q) != _moved(r) and not q.get("insert")
                        and isinstance(q.get("start"), (int, float)) and isinstance(q.get("end"), (int, float))
                        and s < q["end"] - tol and e > q["start"] + tol):
                    v.append(f"{tag}: moved range overlaps range {j} [{q['start']}-{q['end']}]")
        key = (src, _moved(r))
        prev = last.get(key)
        if prev is not None:
            if s < prev["start"] - tol:
                v.append(f"{tag}: out of order (previous starts {prev['start']})")
            elif s < prev["end"] - tol:
                v.append(f"{tag}: overlaps previous range ending {prev['end']}")
        last[key] = r
    if activities is not None:
        pairs = [(st, a) for st, a in activities.items() if a is not None]
    elif activity is not None:
        stems = list(sources)
        act_src = Path(activity.get("source") or "").stem
        pairs = [(act_src if act_src in stems else (stems[0] if len(stems) == 1 else act_src), activity)]
    else:
        pairs = []
    for stem, act in pairs:
        mine = [r for r in ranges if r.get("source") == stem and isinstance(r.get("start"), (int, float))]
        for g in act.get("gaps", []):
            if g.get("class") != "active" or not g.get("must_keep"):
                continue
            a, b = g["must_keep"]
            if not any(r["start"] <= a + tol and r["end"] >= b - tol for r in mine):
                v.append(f"active step {a:.2f}-{b:.2f} (gap {g['start']:.2f}-{g['end']:.2f}) is not fully inside a kept range")
    return v


def _context(edit_dir: Path, ranges_src: list[str]):
    """(activity, words, durations, only). `activity` is the first clip's activity (single-clip
    compatible); use `activities(edit_dir, stems)` for one per clip."""
    stems = list(dict.fromkeys(ranges_src))
    only = len(stems) <= 1
    acts = activities(edit_dir, stems)
    activity = next((a for a in acts.values() if a is not None), None)
    words = {s: load_words(edit_dir, s) for s in stems}
    durs = {s: source_duration(edit_dir, s, words[s], only) for s in stems}
    return activity, words, durs, only


def activities(edit_dir: Path, stems: list[str]) -> dict[str, dict | None]:
    """Per-clip activity. A clip falls back to the old E/activity.json only if that file is its own
    (matching `source`) or there is a single clip."""
    out: dict[str, dict | None] = {}
    for st in stems:
        a = load_activity(edit_dir, st)
        if a is not None and not (Path(edit_dir) / "activity" / f"{st}.json").exists():
            src = Path(a.get("source") or "").stem
            if len(stems) > 1 and src and src != st:
                a = None
        out[st] = a
    return out


def merge(edit_dir: Path, profile: dict):
    edit_dir = Path(edit_dir)
    files = sorted((edit_dir / "chunks").glob("edl_*.json"))
    if not files:
        raise SystemExit(f"no chunks/edl_NN.json files in {edit_dir}")
    raw: list[dict] = []
    for f in files:
        try:
            data = json.loads(f.read_text())
        except ValueError as ex:
            raise SystemExit(f"{f.name}: invalid JSON ({ex})")
        if not isinstance(data, list):
            raise SystemExit(f"{f.name}: must be a JSON array")
        raw.extend(r for r in data if isinstance(r, dict))
    for r in raw:
        if not all(k in r for k in ("source", "start", "end")):
            raise SystemExit(f"range missing source/start/end: {r}")
    # Rio 2.0 inserts: a range from an insert clip is placed straight after the main range the
    # editor wrote before it (its anchor; chunk files are read in order). Main ranges merge as
    # before; inserts get the light touch and are spliced back in at their anchors.
    insert_stems = set(load_sources(edit_dir).get("inserts") or {})
    ins_raw: list[tuple[int | None, dict]] = []
    main_raw: list[dict] = []
    for r in raw:
        if r["source"] in insert_stems or r.get("insert"):
            ins_raw.append((len(main_raw) - 1 if main_raw else None, {**r, "insert": True}))
            r.pop("layout", None)
        else:
            main_raw.append(r)
    raw = main_raw
    anchor_ends: dict[int, float] = {}
    pad_lo, pad_hi = (x / 1000 for x in profile.get("edit", {}).get("pad_ms", [30, 200]))
    sil = silence_settings(profile, edit_dir)
    keep_active = sil["policy"] == "keep_active"
    present = list(dict.fromkeys(r["source"] for r in raw + [x[1] for x in ins_raw]))
    src_order = load_sources(edit_dir)["order"]
    order = [x for x in src_order if x in present] + [x for x in present if x not in src_order]
    all_stems = list(src_order) + [x for x in present if x not in src_order]
    activity, words, durs, only = _context(edit_dir, all_stems)
    acts = activities(edit_dir, all_stems)
    only = len(all_stems) <= 1
    notes: list[str] = []
    snapped: list[dict] = []
    for ri, r in enumerate(raw):
        anchor_ends[ri] = float(r["end"])
        stem = r["source"]
        gaps = activity_gaps(acts.get(stem), stem, only) if keep_active else []
        keeps = [g["must_keep"] for g in gaps if g.get("must_keep")]
        # a silent demo keeps its silent stretch: only an edge inside a word is moved
        res = snap_range(float(r["start"]), float(r["end"]), words[stem], pad_lo,
                         float("inf") if _silent(r) else pad_hi,
                         keeps=keeps, duration=durs[stem])
        if res is None:
            notes.append(f"dropped range {r['start']}-{r['end']} ({r.get('beat', '')}): no words survive snapping")
            continue
        ns, ne, n = res
        notes += [f"{r['start']}-{r['end']}: {x}" for x in n]
        anchor_ends[ri] = ne
        snapped.append({**r, "start": ns, "end": ne})
    snapped.sort(key=lambda r: (all_stems.index(r["source"]), r["start"], r["end"]))
    merged: list[dict] = []
    for r in snapped:
        p = merged[-1] if merged else None
        if (p and p["source"] == r["source"] and r["start"] <= p["end"] + 0.001
                and _layout(p) == _layout(r) and _moved(p) == _moved(r)
                and _silent(p) == _silent(r)):
            if r["end"] > p["end"]:
                p["end"] = r["end"]
            notes.append(f"merged overlapping/touching ranges at {r['start']}")
        else:
            merged.append(dict(r))
    if not keep_active:
        merged, n = compact_ranges(merged, words, sil)
        notes += n
        merged, n = heal_edges(merged, words, sil)
        notes += n
    merged, n = place_moved(merged)
    notes += n
    merged = _join_touching(merged, {x: i for i, x in enumerate(all_stems)})
    merged, n = _trim_into_demos(merged)
    notes += n
    if ins_raw:
        merged, n = place_inserts(merged, ins_raw, raw, anchor_ends, words, profile, sil)
        notes += n
    lay = profile.get("layout") or {}
    notes += smooth_layouts(merged, float(lay.get("min_face_s", 6.0)), float(lay.get("min_split_s", 2.0)))
    for r in merged:
        r["start"], r["end"] = round(r["start"], 3), round(r["end"], 3)
    sources = {}
    for stem in all_stems:
        fname = find_source_file(edit_dir, stem, acts.get(stem))
        sources[stem] = f"../{fname}"
    edl = {"version": 1, "sources": sources, "ranges": merged, "grade": "none", "overlays": [],
           "total_duration_s": round(sum(r["end"] - r["start"] for r in merged), 3)}
    if insert_stems & set(present):
        edl["inserts"] = sorted(insert_stems & set(present))
    if not keep_active:
        violations = validate_edl(edl, durs, None)
    elif all(a is None for a in acts.values()):
        violations = ["activity.json missing: cannot validate step coverage"]
        violations += validate_edl(edl, durs, None)
    else:
        violations = validate_edl(edl, durs, None, activities=acts)
    return edl, violations, notes


def place_inserts(merged: list[dict], ins_raw: list[tuple[int | None, dict]], raw: list[dict],
                  anchor_ends: dict[int, float], words: dict[str, list[dict]], profile: dict,
                  sil: dict) -> tuple[list[dict], list[str]]:
    """Splice insert ranges into the merged main ranges. Each goes right after the merged range
    holding its anchor's (snapped) end; a merged range that runs past that point is split there
    first, so the insert sits exactly where the editor put it. Anchor None = before everything."""
    ins = profile.get("insert") or {}
    max_pause, air = float(ins.get("max_pause_s", 0.6)), float(ins.get("air_ms", 150)) / 1000
    notes: list[str] = []
    out = [dict(r) for r in merged]
    # group inserts by anchor, keeping the editor's order
    groups: dict[int | None, list[dict]] = {}
    for a, r in ins_raw:
        pieces = compact_insert(r, words.get(r["source"], []), max_pause, air)
        if not pieces:
            notes.append(f"dropped insert {r['source']} {r['start']}-{r['end']}: no words")
            continue
        try:
            pieces, n = heal_edges(pieces, words, sil)
            notes += n
        except Exception as ex:  # an insert without an envelope heals nothing
            notes.append(f"insert heal skipped: {ex}")
        groups.setdefault(a, []).extend(pieces)
    for a in sorted((k for k in groups if k is not None), key=lambda k: -k):
        src, t = raw[a]["source"], anchor_ends.get(a, float(raw[a]["end"]))
        idx = None
        for i, r in enumerate(out):
            if r.get("insert") or r["source"] != src:
                continue
            # runs past the anchor with real words after it: split it. Only a word's fading tail
            # (heal/compact keep the sound past the transcript end) stays with its range, or it
            # would play as a stray blip after the insert (HMJ run, 2026-11-09: "said-" at 1:38).
            later = [w for w in words.get(src, []) if t + 0.01 <= w["start"] < r["end"] - 0.01]
            if r["start"] < t - 0.01 < r["end"] - 0.01 and not later:
                idx = i
                continue
            if r["start"] < t - 0.01 < r["end"] - 0.01:     # runs past the anchor: split it
                out[i:i + 1] = [{**r, "end": round(t, 3)}, {**r, "start": round(t, 3)}]
                notes.append(f"split range at {t:.3f} to place an insert")
                idx = i
                break
            if r["start"] <= t + 0.01:
                idx = i
        pos = 0 if idx is None else idx + 1
        while pos < len(out) and out[pos].get("insert"):
            pos += 1
        out[pos:pos] = groups[a]
        notes.append(f"placed {len(groups[a])} insert range(s) after {src}@{t:.2f}")
    if None in groups:
        out[0:0] = groups[None]
        notes.append(f"placed {len(groups[None])} insert range(s) at the very start")
    return out, notes


# ---------------------------------------------------------------- CLI
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cmd", choices=["split", "merge", "validate"])
    ap.add_argument("--edit-dir", required=True, type=Path)
    ap.add_argument("--profile", type=Path, default=None)
    ap.add_argument("--chunk-minutes", type=float, default=None)
    ap.add_argument("--refit", action="store_true",
                    help="validate only: first re-cut the pauses a fix pass put back (cut_all), then rewrite edl.json")
    a = ap.parse_args(argv)
    prof = load_profile(a.profile)
    E = a.edit_dir
    if a.cmd == "split":
        metas = split(E, prof, a.chunk_minutes)
        print(f"wrote {len(metas)} chunks to {E / 'chunks'}")
        return 0
    if a.cmd == "merge":
        edl, viol, notes = merge(E, prof)
        for n in notes:
            print(f"note: {n}", file=sys.stderr)
        if viol:
            (E / "edl.rejected.json").write_text(json.dumps(edl, indent=2) + "\n")
            print(f"merge FAILED: {len(viol)} violation(s) (candidate saved to edl.rejected.json)", file=sys.stderr)
            for x in viol:
                print(f"  - {x}", file=sys.stderr)
            return 1
        (E / "edl.json").write_text(json.dumps(edl, indent=2) + "\n")
        print(f"merged {len(edl['ranges'])} ranges, {edl['total_duration_s']:.1f}s -> {E / 'edl.json'}")
        return 0
    # validate
    p = E / "edl.json"
    if not p.exists():
        print(f"missing {p}", file=sys.stderr)
        return 1
    edl = json.loads(p.read_text())
    stems = list(edl.get("sources") or []) or [r.get("source") for r in edl.get("ranges", [])]
    _, words, durs, _ = _context(E, stems)
    sil = silence_settings(prof, E)
    if a.refit and sil["policy"] != "keep_active":
        # Fixes that extend a range re-add pauses; re-cut them so the fix pass needs one render only.
        merged, notes = refit_ranges(edl.get("ranges", []), words, sil, stems)
        edl["ranges"] = merged
        edl["total_duration_s"] = round(sum(r["end"] - r["start"] for r in merged), 3)
        for n in notes:
            print(f"note: {n}", file=sys.stderr)
    acts = activities(E, stems)
    if silence_settings(prof)["policy"] == "keep_active":
        viol = ([] if any(a is not None for a in acts.values()) else ["activity.json missing: cannot validate step coverage"])
        viol += validate_edl(edl, durs, None, activities=acts)
    else:
        viol = validate_edl(edl, durs, None)
    if viol:
        print(f"validate FAILED: {len(viol)} violation(s)", file=sys.stderr)
        for x in viol:
            print(f"  - {x}", file=sys.stderr)
        return 1
    if a.refit:
        p.write_text(json.dumps(edl, indent=2) + "\n")
    print(f"edl.json OK ({len(edl['ranges'])} ranges{', refit' if a.refit else ''})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
