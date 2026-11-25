"""Media checks imported by helpers/verify_edit.py (see docs/CONTRACTS.md).

    probe(path) -> dict                      (re-export of helpers/media/probe.py)
    loudness(path) -> {"integrated_lufs", "true_peak_dbtp"}   (ffmpeg ebur128=peak=true)
    av_durations(path) -> {"video_s", "audio_s"}

CLI (handy for debugging):  .venv/bin/python rio2/helpers/media/checks.py <video>
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

try:  # imported as helpers.media.checks
    from .probe import probe  # noqa: F401  (re-export)
except ImportError:  # run as a script / imported with helpers/media on sys.path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from probe import probe  # noqa: E402,F401

__all__ = ["probe", "loudness", "av_durations"]


def loudness(path: str | Path) -> dict:
    """Integrated loudness (LUFS) and true peak (dBTP) of the first audio stream."""
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
           "-map", "0:a:0", "-af", "ebur128=peak=true", "-f", "null", "-"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ebur128 failed: {proc.stderr.strip()[-300:]}")
    err = proc.stderr
    summary = err[err.rfind("Summary:"):] if "Summary:" in err else err
    m_i = re.search(r"I:\s+(-?[\d.]+|-inf)\s+LUFS", summary)
    m_tp = re.search(r"True peak:\s+Peak:\s+(-?[\d.]+|-inf)\s+dBFS", summary)
    if not m_i or not m_tp:
        raise RuntimeError("could not parse ebur128 summary")

    def f(s: str) -> float:
        return float("-inf") if s == "-inf" else float(s)

    return {"integrated_lufs": f(m_i.group(1)), "true_peak_dbtp": f(m_tp.group(1))}


def _stream_duration(path: str | Path, sel: str) -> float | None:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", sel, "-show_entries",
         "stream=duration,start_time", "-of", "json", str(path)],
        capture_output=True, text=True)
    streams = (json.loads(out.stdout or "{}").get("streams") or []) if out.returncode == 0 else []
    if not streams:
        return None
    d = streams[0].get("duration")
    if d not in (None, "N/A"):
        return float(d)
    # Containers without per-stream duration (mkv/webm): last packet end.
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", sel, "-show_entries",
         "packet=pts_time,duration_time", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True)
    end = None
    start = None
    for line in out.stdout.splitlines():
        parts = line.split(",")
        try:
            pts = float(parts[0])
            dur = float(parts[1]) if len(parts) > 1 and parts[1] not in ("", "N/A") else 0.0
        except ValueError:
            continue
        start = pts if start is None else min(start, pts)
        end = pts + dur if end is None else max(end, pts + dur)
    return (end - start) if end is not None else None


def av_durations(path: str | Path) -> dict:
    """Duration of the first video and first audio stream in seconds (None if absent)."""
    v = _stream_duration(path, "v:0")
    a = _stream_duration(path, "a:0")
    return {"video_s": round(v, 3) if v is not None else None,
            "audio_s": round(a, 3) if a is not None else None}


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: checks.py <video>")
    p = sys.argv[1]
    print(json.dumps({"probe": probe(p), "loudness": loudness(p), "av": av_durations(p)}, indent=2))
