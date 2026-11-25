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

Two-camera fixtures (make_cam_fixtures, Rio 2.0; same folder):
    sync_screen.mp4      640x400 (16:10) @ 30 fps, 60 s, speech-like audio (seeded, aperiodic:
                         the main fixture's tone repeats every 14 s, so it cannot be synced)
    fixture_cam.mov      1280x720 @ 30000/1001 CFR, the SAME speech heard by a second "mic":
                         cam_t = 3.2 + (1 + 50e-6) * screen_t (evaluated analytically, so the
                         drift is exact), gain 0.6, its own noise at about -40 dB, unrelated
                         talk before screen t=0, and it stops at screen t=45 (covers [0, 45])
    fixture_cam_vfr.mov  same, with jittered frame times on a 1/600 timescale (iPhone-style VFR)
    noise_cam.mov        20 s of noise only (sync must give ok: false)
    cam_truth.json       offset_s, drift, covers

Usage:  .venv/bin/python rio2/tests/make_fixture.py [--out-dir rio2/tests/_fixtures] [--force]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np

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


CAM_FIXTURE_VERSION = 1
CAM_OFFSET_S, CAM_DRIFT, CAM_COVERS_END = 3.2, 50e-6, 45.0
SYNC_SCREEN_S, SR = 60.0, 48000


def syllables(seed: int, start: float, end: float) -> list[tuple]:
    """Random words of 1-4 voiced syllables with pauses: (t0, dur, f0, f1, F1, F2, amp)."""
    rng = np.random.default_rng(seed)
    out, t = [], start + float(rng.uniform(0.1, 0.4))
    while t < end - 0.3:
        for _ in range(int(rng.integers(1, 5))):
            dur = float(rng.uniform(0.08, 0.26))
            f0 = float(rng.uniform(100, 220))
            out.append((t, dur, f0, f0 * float(rng.uniform(0.85, 1.15)), float(rng.uniform(300, 900)),
                        float(rng.uniform(900, 2500)), float(rng.uniform(0.4, 1.0))))
            t += dur + float(rng.uniform(0.0, 0.05))
        t += float(rng.uniform(0.6, 1.6)) if rng.random() < 0.15 else float(rng.uniform(0.08, 0.45))
    return out


def speech(t: np.ndarray, syl: list[tuple]) -> np.ndarray:
    """Evaluate the voiced syllables at arbitrary times t (s), so a resampled/drifted copy is exact."""
    y = np.zeros(len(t))
    for t0, dur, f0, f1, F1, F2, amp in syl:
        lo, hi = np.searchsorted(t, t0), np.searchsorted(t, t0 + dur)
        if hi <= lo:
            continue
        tau = t[lo:hi] - t0
        env = np.sin(np.pi * tau / dur) ** 2
        phase = 2 * np.pi * (f0 * tau + 0.5 * (f1 - f0) / dur * tau ** 2)
        acc = np.zeros(hi - lo)
        for k in range(1, int(3500 / max(f0, f1)) + 1):
            fk = k * f0
            w = np.exp(-((fk - F1) / 150) ** 2) + 0.6 * np.exp(-((fk - F2) / 250) ** 2) + 0.05
            acc += w * np.sin(k * phase)
        y[lo:hi] += amp * env * acc
    return y


def _write_wav(path: Path, mono: np.ndarray) -> None:
    pcm = np.clip(mono / max(1e-9, float(np.abs(mono).max())) * 0.5 * 32767, -32767, 32767)
    st = np.repeat(pcm.astype("<i2")[:, None], 2, axis=1)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(st.tobytes())


def _mux(video_args: list[str], wav: Path, out: Path, dur: float, extra: list[str]) -> None:
    tmp = out.with_name(out.stem + ".tmp" + out.suffix)
    cmd = (["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"] + video_args + ["-i", str(wav),
           "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
           "-pix_fmt", "yuv420p"] + extra + ["-c:a", "aac", "-b:a", "128k", "-ar", str(SR),
           "-t", f"{dur:.3f}", str(tmp)])
    subprocess.run(cmd, check=True)
    tmp.replace(out)


def make_cam_fixtures(out_dir: Path = DEFAULT_DIR, force: bool = False) -> dict:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {"screen": out_dir / "sync_screen.mp4", "cam": out_dir / "fixture_cam.mov",
             "cam_vfr": out_dir / "fixture_cam_vfr.mov", "noise_cam": out_dir / "noise_cam.mov",
             "truth": out_dir / "cam_truth.json"}
    if not force and all(p.exists() for p in paths.values()):
        try:
            if json.loads(paths["truth"].read_text()).get("version") == CAM_FIXTURE_VERSION:
                return paths
        except (OSError, json.JSONDecodeError):
            pass
    syl = syllables(7, 0.0, SYNC_SCREEN_S)
    t = np.arange(int(SYNC_SCREEN_S * SR)) / SR
    wav = out_dir / "_sync_screen.wav"
    _write_wav(wav, speech(t, syl))
    _mux(["-f", "lavfi", "-i", f"testsrc2=s=640x400:r={FPS}:d={SYNC_SCREEN_S}"], wav,
         paths["screen"], SYNC_SCREEN_S, ["-r", str(FPS)])

    cam_dur = CAM_OFFSET_S + (1 + CAM_DRIFT) * CAM_COVERS_END
    u = np.arange(int(cam_dur * SR)) / SR                       # camera time
    ts = (u - CAM_OFFSET_S) / (1 + CAM_DRIFT)                   # screen time heard at u
    y = 0.6 * speech(ts, syl)
    pre = u < CAM_OFFSET_S
    y[pre] += 0.6 * speech(u[pre], syllables(99, 0.0, CAM_OFFSET_S))   # unrelated talk
    y += np.random.default_rng(5).normal(0, 0.006, len(u))
    _write_wav(wav, y)
    cam_v = (["-f", "lavfi", "-i", f"color=c=red:s=1280x720:r=30000/1001:d={cam_dur:.3f}"])
    box = "drawbox=x=40:y='mod(t*200,600)':w=120:h=120:color=white:t=fill"
    _mux(cam_v, wav, paths["cam"], cam_dur, ["-vf", box, "-r", "30000/1001"])
    _mux(cam_v, wav, paths["cam_vfr"], cam_dur,
         ["-vf", f"{box},setpts='(N+0.3*sin(N*1.7))/(30000/1001)/TB'",
          "-fps_mode", "passthrough", "-video_track_timescale", "600"])
    n = np.random.default_rng(11).normal(0, 0.1, int(20 * SR))
    _write_wav(wav, n)
    _mux(["-f", "lavfi", "-i", "color=c=gray:s=320x180:r=30:d=20"], wav, paths["noise_cam"], 20.0, [])
    wav.unlink(missing_ok=True)
    paths["truth"].write_text(json.dumps({"version": CAM_FIXTURE_VERSION, "offset_s": CAM_OFFSET_S,
                                          "drift": CAM_DRIFT, "covers": [0.0, CAM_COVERS_END],
                                          "screen_duration_s": SYNC_SCREEN_S}, indent=1))
    return paths


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_DIR)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--cams", action="store_true", help="also make the two-camera fixtures")
    args = ap.parse_args()
    try:
        paths = make_fixture(args.out_dir, args.force)
        if args.cams:
            paths.update({f"cam_{k}": v for k, v in make_cam_fixtures(args.out_dir, args.force).items()})
    except subprocess.CalledProcessError as e:
        sys.exit(f"make_fixture: ffmpeg failed ({e.returncode})")
    for k, p in paths.items():
        print(f"{k}: {p}")


if __name__ == "__main__":
    main()
