"""Generate a synthetic ~2-min screen-recording fixture with ffmpeg lavfi.

Writes into tests/_fixtures/ (gitignored):
    tutorial_fixture.mp4          1280x720 @ 30 fps, H.264 + AAC stereo 48 kHz
    tutorial_fixture.json         fake Scribe response (words: word/spacing)
    tutorial_fixture_truth.json   ground-truth segment labels

Timeline: a 3 s static lead-in, then 8 x [speech (a) 6 s, silent kind 8 s]
with kinds b c d e e d c b, then a final speech (a) 6 s and a 3 s static tail.
  (a) speech  : 440 Hz modulated tone + a moving box (Scribe words cover it)
  (b) static  : silence, nothing changes                   -> expect static
  (c) click   : silence, a large panel appears at +3 s and
                a second page replaces it at +6 s          -> expect active
  (d) typing  : silence, 40 small glyph boxes appear one by
                one on a single line (+caret) from +1 s     -> expect typing
  (e) cursor  : silence, only a 16x22 cursor moves around   -> expect static

Sizes are chosen so that at the detector's analysis width (480 px) a glyph
and the cursor are about as big as they are on a real 2880-px-wide recording.

Usage:  .venv/bin/python rio1/tests/make_fixture.py [--out-dir rio1/tests/_fixtures] [--force]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

FIXTURE_VERSION = 1
W, H, FPS = 1280, 720, 30
SPEECH_S, KIND_S, LEAD_S, TAIL_S = 6.0, 8.0, 3.0, 3.0
KINDS = ["static", "click", "typing", "cursor", "cursor", "typing", "click", "static"]
EXPECTED = {"static": "static", "click": "active", "typing": "typing",
            "cursor": "static", "lead_in": "static", "tail": "static"}

TYPE_START, TYPE_STEP, TYPE_CHARS = 1.0, 0.15, 40
GLYPH_W, GLYPH_H, GLYPH_PITCH, LINE_X, LINE_Y = 6, 11, 9, 260, 150

DEFAULT_DIR = Path(__file__).resolve().parent / "_fixtures"


def timeline() -> list[dict]:
    segs, t = [{"start": 0.0, "end": LEAD_S, "label": "lead_in"}], LEAD_S
    for kind in KINDS:
        segs.append({"start": t, "end": t + SPEECH_S, "label": "speech"})
        t += SPEECH_S
        segs.append({"start": t, "end": t + KIND_S, "label": kind})
        t += KIND_S
    segs.append({"start": t, "end": t + SPEECH_S, "label": "speech"})
    t += SPEECH_S
    segs.append({"start": t, "end": t + TAIL_S, "label": "tail"})
    for s in segs:
        s["expected_class"] = EXPECTED.get(s["label"])  # None for speech
    return segs


def _video_filter(segs: list[dict]) -> str:
    """Build the filter_complex: background + static UI + per-segment content."""
    def en(a: float, b: float) -> str:
        return f"enable='between(t,{a:.3f},{b:.3f})'"

    boxes = [
        "drawbox=x=0:y=0:w=1280:h=40:color=0x333333:t=fill",       # title bar
        "drawbox=x=0:y=40:w=200:h=680:color=0xCCCCCC:t=fill",      # sidebar
        "drawbox=x=220:y=60:w=1040:h=640:color=0xFFFFFF:t=fill",   # editor area
    ]
    for s in segs:
        a, b = s["start"], s["end"] - 0.001
        if s["label"] == "click":
            boxes.append(f"drawbox=x=240:y=120:w=800:h=450:color=0x3366CC:t=fill:{en(a + 3.0, b)}")
            boxes.append(f"drawbox=x=240:y=120:w=800:h=450:color=0xCC6633:t=fill:{en(a + 6.0, b)}")
        elif s["label"] == "typing":
            t0 = a + TYPE_START
            boxes.append(f"drawbox=x={LINE_X}:y={LINE_Y - 1}:w=2:h={GLYPH_H + 2}:color=black:t=fill:{en(a, t0)}")
            for k in range(TYPE_CHARS):
                tk = t0 + k * TYPE_STEP
                x = LINE_X + k * GLYPH_PITCH
                boxes.append(f"drawbox=x={x}:y={LINE_Y}:w={GLYPH_W}:h={GLYPH_H}:color=black:t=fill:{en(tk, b)}")
                caret_end = b if k == TYPE_CHARS - 1 else tk + TYPE_STEP
                boxes.append(f"drawbox=x={x + GLYPH_PITCH}:y={LINE_Y - 1}:w=2:h={GLYPH_H + 2}"
                             f":color=black:t=fill:{en(tk, caret_end)}")
    speech = "+".join(f"between(t,{s['start']:.3f},{s['end'] - 0.001:.3f})"
                      for s in segs if s["label"] == "speech")
    cursor = "+".join(f"between(t,{s['start']:.3f},{s['end'] - 0.001:.3f})"
                      for s in segs if s["label"] == "cursor")
    return (
        f"[0:v]{','.join(boxes)}[bg];"
        f"[bg][1:v]overlay=x='240+mod(t*300,800)':y=300:eval=frame:enable='{speech}'[v1];"
        f"[v1][2:v]overlay=x='600+250*cos(t*1.7)':y='330+150*sin(t*1.7)':eval=frame:enable='{cursor}'[v]"
    )


def scribe_words(segs: list[dict]) -> dict:
    """Fake Scribe response: words every 0.4 s across each speech segment."""
    words: list[dict] = []
    n_word = 0
    for s in segs:
        if s["label"] != "speech":
            continue
        n = int(round((s["end"] - s["start"]) / 0.4))
        for i in range(n):
            ws = s["start"] + 0.4 * i
            we = s["end"] if i == n - 1 else ws + 0.3
            if words:
                words.append({"text": " ", "start": round(words[-1]["end"], 3),
                              "end": round(ws, 3), "type": "spacing", "speaker_id": "speaker_0"})
            words.append({"text": f"word{n_word}", "start": round(ws, 3), "end": round(we, 3),
                          "type": "word", "speaker_id": "speaker_0"})
            n_word += 1
    text = " ".join(w["text"] for w in words if w["type"] == "word")
    return {"language_code": "eng", "language_probability": 1.0, "text": text, "words": words}


def make_fixture(out_dir: Path = DEFAULT_DIR, force: bool = False) -> dict:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    video = out_dir / "tutorial_fixture.mp4"
    scribe = out_dir / "tutorial_fixture.json"
    truth = out_dir / "tutorial_fixture_truth.json"
    paths = {"video": video, "transcript": scribe, "truth": truth}

    if not force and all(p.exists() for p in paths.values()):
        try:
            if json.loads(truth.read_text()).get("version") == FIXTURE_VERSION:
                return paths
        except (OSError, json.JSONDecodeError):
            pass

    segs = timeline()
    total = segs[-1]["end"]
    speech_expr = "+".join(f"between(t,{s['start']:.3f},{s['end'] - 0.001:.3f})"
                           for s in segs if s["label"] == "speech")
    tone = f"0.3*(0.6+0.4*sin(2*PI*3*t))*sin(2*PI*440*t)*({speech_expr})"
    tmp = video.with_suffix(".tmp.mp4")
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"color=c=0xE8E8E8:s={W}x{H}:r={FPS}:d={total}",
        "-f", "lavfi", "-i", f"color=c=0x2255AA:s=120x80:r={FPS}:d={total}",
        "-f", "lavfi", "-i", f"color=c=black:s=16x22:r={FPS}:d={total}",
        "-f", "lavfi", "-i", f"aevalsrc=exprs='{tone}|{tone}':s=48000:d={total}",
        "-filter_complex", _video_filter(segs),
        "-map", "[v]", "-map", "3:a",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
        "-t", f"{total}", "-movflags", "+faststart", str(tmp),
    ]
    subprocess.run(cmd, check=True)
    tmp.replace(video)
    scribe.write_text(json.dumps(scribe_words(segs), indent=1))
    truth.write_text(json.dumps({"version": FIXTURE_VERSION, "duration_s": total,
                                 "width": W, "height": H, "fps": FPS, "segments": segs}, indent=2))
    return paths


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_DIR)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    try:
        paths = make_fixture(args.out_dir, args.force)
    except subprocess.CalledProcessError as e:
        sys.exit(f"make_fixture: ffmpeg failed ({e.returncode})")
    for k, p in paths.items():
        print(f"{k}: {p}")


if __name__ == "__main__":
    main()
