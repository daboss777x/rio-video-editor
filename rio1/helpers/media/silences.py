"""Audio-only silence listener (independent of transcript word timings).

Usage:
    .venv/bin/python rio1/helpers/media/silences.py <video> --out E/silences/<stem>.json
        [--noise-db -35] [--min-s 0.25] [--relative-db N]

Output: {"source": name, "noise_db": N, "min_s": S, "silences": [[start, end], ...]}
in source seconds (3 decimals, sorted, merged when overlapping).

Runs ffmpeg `silencedetect` with -vn (no video decode), streaming stderr line by
line, so RAM stays tiny. A trailing silence with no silence_end ends at the
file duration (ffprobe).

Defaults: noise_db=-35 dB, min_s=0.25 s (see tuning notes in the task report:
speech pauses in screen-recorded tutorials sit well below -35 dB while room
noise/breaths are mostly above it).
--relative-db N (optional, off by default): ignore --noise-db and use
(measured mean volume + N) dB instead, e.g. -20 for very quiet/loud clips.
The threshold actually used is written to "noise_db".

Also writes the loudness envelope next to --out as <stem>.envelope.json (skip with
--no-envelope): {"source", "hop_s": 0.01, "floor_db", "db": [int dB per 10 ms frame]}.
silencedetect's -35 dB sits inside a quiet speaker's soft word endings ("ch", "st", "n"),
so the merge places every cut edge on this envelope instead (where the sound really fades
into the room floor). floor_db = 10th percentile of the frames, ignoring digital silence
(frames at or within 2 dB of the -100 clamp: a muted or zero-filled stretch is not the room;
2026-11-13, drive-organizer mainscreen1 read -100 from a 282 s mute). If the clip has no room
noise at all (every non-mute frame is louder than NOISE_DB, e.g. clean synthetic audio),
the mute is the only quiet there is and the old all-frame value is kept. A second audio-only decode,
streamed in blocks, so RAM stays small.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

NOISE_DB = -35.0
MIN_S = 0.25
# Envelope frames at or below this are digital silence (the clamp is -100; a muted stretch with
# dither or a lossy codec's last bit reads -99/-98). Real room noise sits 25+ dB above it.
DIGITAL_SILENCE_DB = -98


def _fail(msg: str) -> None:
    print(f"silences.py: {msg}", file=sys.stderr)
    sys.exit(1)


def _duration(path: str) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", path], capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        _fail(f"cannot read duration of {path}: {r.stderr.strip()[:120]}")


def _mean_db(path: str) -> float:
    r = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-vn", "-i", path,
                        "-af", "volumedetect", "-f", "null", "-"],
                       capture_output=True, text=True)
    m = re.search(r"mean_volume:\s*(-?[\d.]+)", r.stderr)
    if not m:
        _fail("could not measure mean volume (no audio stream?)")
    return float(m.group(1))


def _num(n: float) -> float:
    return round(float(n), 3)


def detect(path: str, noise_db: float = NOISE_DB, min_s: float = MIN_S,
           relative_db: float | None = None) -> dict:
    if not Path(path).is_file():
        _fail(f"input not found: {path}")
    if relative_db is not None:
        noise_db = round(_mean_db(path) + relative_db, 1)
    dur = _duration(path)
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-vn", "-i", path,
           "-af", f"silencedetect=noise={noise_db}dB:d={min_s}", "-f", "null", "-"]
    p = subprocess.Popen(cmd, stderr=subprocess.PIPE, text=True, bufsize=1)
    spans, start = [], None
    for line in p.stderr:
        m = re.search(r"silence_start:\s*(-?[\d.]+)", line)
        if m:
            start = max(0.0, float(m.group(1)))
            continue
        m = re.search(r"silence_end:\s*(-?[\d.]+)", line)
        if m and start is not None:
            spans.append([start, float(m.group(1))])
            start = None
    if p.wait() != 0:
        _fail(f"ffmpeg failed on {path} (no audio stream?)")
    if start is not None:
        spans.append([start, dur])
    spans.sort()
    merged: list[list[float]] = []
    for s, e in spans:
        e = min(e, dur)
        if e <= s:
            continue
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return {"source": Path(path).name, "noise_db": _num(noise_db) if noise_db % 1 else int(noise_db),
            "min_s": min_s,
            "silences": [[_num(s), _num(e)] for s, e in merged]}


HOP_S = 0.01
ENV_RATE = 16000


VOICE_BAND_AF = "highpass=f=250:poles=2,highpass=f=250:poles=2"  # no low-pass: a final "s" lives above 4 kHz


def envelope(path: str) -> dict:
    """RMS loudness per 10 ms frame (mono, 16 kHz) above the rumble only (high-pass 250 Hz; sibilants kept), as whole dB (floor -100).
    User, 2026-11-03 (claudesetup): a MacBook mic in a boomy room carries low rumble 15-20 dB
    under the speech; measured full-band it read as "still talking" and kept up to 0.8 s of it
    after sentences. Speech keeps most of its energy in VOICE_BAND, rumble doesn't."""
    import numpy as np
    n = int(ENV_RATE * HOP_S)
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-vn", "-i", path, "-ac", "1", "-ar", str(ENV_RATE),
           "-af", VOICE_BAND_AF, "-f", "s16le", "-"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    out: list[int] = []
    rest = b""
    while True:
        buf = p.stdout.read(n * 2 * 6000)
        if not buf:
            break
        buf = rest + buf
        k = len(buf) // (n * 2) * (n * 2)
        rest = buf[k:]
        x = np.frombuffer(buf[:k], np.int16).astype(np.float32).reshape(-1, n) / 32768.0
        db = 20 * np.log10(np.sqrt((x * x).mean(axis=1)) + 1e-5)
        out += np.maximum(np.round(db), -100).astype(int).tolist()
    if p.wait() != 0:
        _fail(f"ffmpeg failed decoding audio of {path}")
    floor = _floor_db(np.asarray(out)) if out else -100
    return {"source": Path(path).name, "hop_s": HOP_S, "floor_db": floor, "band": VOICE_BAND_AF, "db": out}


def _floor_db(db) -> int:
    """10th percentile of the envelope, digital-silence frames left out (see module doc)."""
    import numpy as np
    live = db[db > DIGITAL_SILENCE_DB]
    if live.size:
        floor = np.percentile(live, 10)
        if floor <= NOISE_DB:          # the clip has room noise: that is the floor
            return int(floor)
    return int(np.percentile(db, 10))


def envelope_path(out: Path) -> Path:
    return out.with_name(out.name[:-5] + ".envelope.json" if out.name.endswith(".json") else out.name + ".envelope.json")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("video")
    ap.add_argument("--out", required=True)
    ap.add_argument("--noise-db", type=float, default=NOISE_DB)
    ap.add_argument("--min-s", type=float, default=MIN_S)
    ap.add_argument("--relative-db", type=float, default=None)
    ap.add_argument("--no-envelope", action="store_true", help="skip the loudness envelope file")
    a = ap.parse_args()
    res = detect(a.video, a.noise_db, a.min_s, a.relative_db)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res))
    print(f"{len(res['silences'])} silences -> {out}")
    if not a.no_envelope:
        env = envelope(a.video)
        ep = envelope_path(out)
        ep.write_text(json.dumps(env, separators=(",", ":")))
        print(f"envelope: {len(env['db'])} frames, floor {env['floor_db']} dB -> {ep}")


if __name__ == "__main__":
    main()
