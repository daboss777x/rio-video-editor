"""Screen blurs for the render (Rio 1; the same helpers Rio 2.0 keeps in composite.py).

E/blurs.json (blur_find.py, or written by hand):
    {"version": 1, "blurs": [{"source": <clip stem>, "start", "end" (source s), "rect": [x, y, w, h]
                              (source px), "text", "auto"}]}
render_tutorial.py checks every source against the EDL (check_blur_sources), takes each range's
windows (blurs_for) and blurs them at the source's own size, before scaling to the output
(blur_graph).
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path


def _even(v: float) -> int:
    return int(round(float(v) / 2.0)) * 2


def blur_graph(inp: str, out: str, blurs: list[dict], scale: float = 1.0, tag: str = "b") -> list[str]:
    """Filter-graph lines blurring each {"a", "b", "rect"} (segment-relative seconds, source
    pixels times `scale`) of stream [inp] into [out]. Empty list when there is nothing to blur."""
    if not blurs:
        return []
    f = [f"[{inp}]split={len(blurs) + 1}[{tag}m0]" + "".join(f"[{tag}s{i}]" for i in range(len(blurs)))]
    last = f"{tag}m0"
    for i, b in enumerate(blurs):
        x, y, w, h = (_even(v * scale) for v in b["rect"])
        w, h = max(w, 4), max(h, 4)
        r = max(2, min(40, min(w, h) // 3))
        f.append(f"[{tag}s{i}]crop={w}:{h}:{x}:{y},boxblur=lr={r}:lp=3:cr={max(1, r // 2)}:cp=3[{tag}b{i}]")
        nxt = out if i == len(blurs) - 1 else f"{tag}m{i + 1}"
        f.append(f"[{last}][{tag}b{i}]overlay=x={x}:y={y}:"
                 f"enable='between(t\\,{b['a']:.3f}\\,{b['b']:.3f})'[{nxt}]")
        last = nxt
    return f


def check_blur_sources(blurs: list[dict], sources: dict) -> list[dict]:
    """E/blurs.json entries with `source` as the EDL's clip name. A clip's file name ("main.mp4")
    is mapped to its name ("main"); any other unknown source raises ValueError, so a blur can never
    be skipped silently (2026-11-17)."""
    by_file = {Path(str(p)).name: name for name, p in sources.items()}
    out, bad = [], []
    for b in blurs or []:
        src = b.get("source")
        name = src if src in sources else by_file.get(Path(str(src)).name) or by_file.get(str(src))
        if name is None:
            bad.append(str(src))
            continue
        out.append({**b, "source": name})
    if bad:
        raise ValueError(f"E/blurs.json names source(s) {sorted(set(bad))} that are not clips of this edit "
                         f"({sorted(sources)}); fix the 'source' of those blurs")
    return out


def blurs_for(blurs: list[dict], source: str, start: float, end: float) -> list[dict]:
    """E/blurs.json entries overlapping [start, end] of `source`, as segment-relative windows."""
    out = []
    for b in blurs or []:
        if b.get("source") != source or b["end"] <= start or b["start"] >= end:
            continue
        out.append({"a": round(max(0.0, b["start"] - start), 3), "b": round(b["end"] - start, 3),
                    "rect": list(b["rect"])})
    return out


def probe_video(path: Path) -> dict:
    """{"w", "h"} of a video's first stream, in display space (rotation applied)."""
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height:stream_side_data=rotation",
                          "-of", "json", str(path)], capture_output=True, text=True)
    try:
        st = (json.loads(out.stdout).get("streams") or [])[0]
    except (ValueError, IndexError):
        raise RuntimeError(f"ffprobe found no video stream in {path}")
    w, h = int(st["width"]), int(st["height"])
    for sd in st.get("side_data_list") or []:
        if abs(int(sd.get("rotation", 0))) % 180 == 90:
            w, h = h, w
    return {"w": w, "h": h}
