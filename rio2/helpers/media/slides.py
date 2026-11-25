"""Slide stills for the layout decision (Rio 2.0 "layouts and inserts", docs/CONTRACTS.md).

    .venv/bin/python rio2/helpers/media/slides.py --edit-dir E [--threshold 0.06] [--min-gap 3]

For each screen clip in E/sources.json "order": one ffmpeg pass (software decode, 2 fps,
scaled to 480 px wide, scene score) writes a small JPEG at t=0 and at every visual change
(scene score > threshold) to E/slides/<stem>/s_NNNN.jpg, then E/slides/<stem>.json:
    {"source": <file>, "threshold": T, "slides": [{"t": seconds, "still": "slides/<stem>/s_NNNN.jpg"}]}
Changes closer than --min-gap seconds to the previous kept still are dropped (typing, scrolling
and cursor moves make bursts). The editor looks at the stills of her chunk to judge whether the
screen shows something visual (split layout) or nothing worth showing (face layout).
Peak RAM: one 480 px frame pipeline (~250 MB with the software decoder). No LLM tokens.
Software decode on purpose (2026-11-10): on the M3, VideoToolbox decodes the 60 fps 3024x1964 H.264
screen recordings about 8x slower than ffmpeg's own decoder (48 s vs 6.5 s per 2 source minutes,
same stills): a 40-min clip took 19 min with -hwaccel, about 2.5 min without.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

PTS_RE = re.compile(r"pts_time:\s*([\d.]+)")


def stills(video: Path, out_dir: Path, threshold: float = 0.06, min_gap: float = 3.0,
           width: int = 480) -> list[dict]:
    out_dir.mkdir(parents=True, exist_ok=True)
    for f in out_dir.glob("*.jpg"):
        f.unlink()
    vf = (f"fps=2,scale={width}:-2,select='eq(n\\,0)+gt(scene\\,{threshold})',showinfo")
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-i", str(video),   # software decode: see the docstring
           "-map", "0:v:0", "-an", "-vf", vf, "-fps_mode", "vfr", "-q:v", "5",
           str(out_dir / "raw_%05d.jpg")]
    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"slides: ffmpeg failed on {video.name}: {proc.stderr.strip()[-300:]}")
    times = [float(m.group(1)) for line in proc.stderr.splitlines()
             if "Parsed_showinfo" in line and (m := PTS_RE.search(line))]
    raws = sorted(out_dir.glob("raw_*.jpg"))
    kept: list[dict] = []
    last = None
    for t, f in zip(times, raws):
        if last is not None and t - last < min_gap:
            f.unlink()
            continue
        name = f"s_{len(kept) + 1:04d}.jpg"
        f.rename(out_dir / name)
        kept.append({"t": round(t, 2), "still": f"slides/{out_dir.name}/{name}"})
        last = t
    for f in out_dir.glob("raw_*.jpg"):
        f.unlink()
    return kept


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--edit-dir", type=Path, required=True)
    ap.add_argument("--threshold", type=float, default=0.06)
    ap.add_argument("--min-gap", type=float, default=3.0)
    a = ap.parse_args(argv)
    E = a.edit_dir
    src = json.loads((E / "sources.json").read_text())
    for stem in src.get("order") or []:
        video = E.parent / src["files"][stem]
        try:
            ss = stills(video, E / "slides" / stem, a.threshold, a.min_gap)
        except RuntimeError as ex:
            print(ex, file=sys.stderr)
            return 1
        (E / "slides" / f"{stem}.json").write_text(json.dumps(
            {"source": video.name, "threshold": a.threshold, "slides": ss}, indent=1) + "\n")
        print(f"slides: {stem}: {len(ss)} stills -> {E / 'slides' / stem}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
