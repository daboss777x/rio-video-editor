"""silences.py on synthetic audio with known gaps."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "helpers" / "media" / "silences.py"


def make_audio(path, segs):
    """segs: list of (kind, seconds), kind 'tone', 'sil' (pure digital silence) or 'noise' (quiet room hiss)."""
    parts, labels = [], []
    for i, (k, d) in enumerate(segs):
        src = {"tone": f"sine=f=440:d={d}:r=16000",
               "noise": f"anoisesrc=r=16000:a=0.001:c=white:seed=7:d={d}",
               "sil": f"anullsrc=r=16000:cl=mono:d={d}"}[k]
        parts += ["-f", "lavfi", "-i", src]
        labels.append(f"[{i}:a]")
    fc = "".join(labels) + f"concat=n={len(segs)}:v=0:a=1[o]"
    subprocess.run(["ffmpeg", "-y", "-v", "error", *parts, "-filter_complex", fc,
                    "-map", "[o]", str(path)], check=True)


def run(wav, out, *extra):
    return subprocess.run([sys.executable, str(SCRIPT), str(wav), "--out", str(out), *extra],
                          capture_output=True, text=True)


def test_known_gaps_and_contract(tmp_path):
    wav = tmp_path / "a b.wav"  # space in name
    make_audio(wav, [("tone", 1), ("sil", 0.3), ("tone", 1), ("sil", 0.6),
                     ("tone", 1), ("sil", 1.2), ("tone", 1)])
    out = tmp_path / "deep" / "dir" / "x.json"
    r = run(wav, out)
    assert r.returncode == 0, r.stderr
    d = json.loads(out.read_text())
    assert list(d) == ["source", "noise_db", "min_s", "silences"]
    assert d["source"] == "a b.wav" and d["noise_db"] == -35 and d["min_s"] == 0.25
    exp = [[1.0, 1.3], [2.3, 2.9], [3.9, 5.1]]
    assert len(d["silences"]) == 3
    for (s, e), (es, ee) in zip(d["silences"], exp):
        assert abs(s - es) < 0.05 and abs(e - ee) < 0.05


def test_trailing_silence_ends_at_duration(tmp_path):
    wav = tmp_path / "t.wav"
    make_audio(wav, [("tone", 1), ("sil", 1.0)])
    out = tmp_path / "t.json"
    assert run(wav, out).returncode == 0
    s = json.loads(out.read_text())["silences"]
    assert len(s) == 1 and abs(s[0][0] - 1.0) < 0.05 and abs(s[0][1] - 2.0) < 0.05


def test_short_gap_ignored(tmp_path):
    wav = tmp_path / "s.wav"
    make_audio(wav, [("tone", 1), ("sil", 0.15), ("tone", 1)])
    out = tmp_path / "s.json"
    assert run(wav, out).returncode == 0
    assert json.loads(out.read_text())["silences"] == []


def test_failure_is_one_line_nonzero(tmp_path):
    r = run(tmp_path / "missing.wav", tmp_path / "o.json")
    assert r.returncode != 0 and len(r.stderr.strip().splitlines()) == 1


def test_envelope_written_next_to_silences(tmp_path):
    wav = tmp_path / "e.wav"
    make_audio(wav, [("tone", 1), ("sil", 0.5), ("tone", 1)])
    out = tmp_path / "e.json"
    assert run(wav, out).returncode == 0
    env = json.loads((tmp_path / "e.envelope.json").read_text())
    assert env["hop_s"] == 0.01 and abs(len(env["db"]) - 250) <= 2
    db = env["db"]
    assert db[50] > -30 and db[125] == -100 and env["floor_db"] <= -90
    assert not (tmp_path / "x.envelope.json").exists()
    assert run(wav, tmp_path / "n.json", "--no-envelope").returncode == 0
    assert not (tmp_path / "n.envelope.json").exists()


def test_floor_ignores_digital_silence(tmp_path):
    """Real bug (drive-organizer mainscreen1): 282 s of muted audio (frames clamped at -100 dB)
    made up 13% of the clip, so the 10th-percentile floor read -100 instead of the room's ~-69,
    and the speech line (floor + 27) sat inside the room noise. The floor is the room, not the mute."""
    wav = tmp_path / "d.wav"
    make_audio(wav, [("noise", 0.6), ("tone", 1), ("noise", 0.6), ("sil", 2.0),
                     ("tone", 1), ("noise", 0.6)])
    assert run(wav, tmp_path / "d.json").returncode == 0
    env = json.loads((tmp_path / "d.envelope.json").read_text())
    db = env["db"]
    assert sum(v == -100 for v in db) > 0.3 * len(db)       # the mute really is in the envelope
    noise = sorted(db[10:50])                               # inside the first room-noise stretch
    assert -90 < env["floor_db"] <= noise[len(noise) // 2] + 3


def test_floor_unchanged_without_digital_silence(tmp_path):
    import numpy as np
    wav = tmp_path / "n.wav"
    make_audio(wav, [("noise", 0.8), ("tone", 1), ("noise", 0.8), ("tone", 1), ("noise", 0.8)])
    assert run(wav, tmp_path / "n.json").returncode == 0
    env = json.loads((tmp_path / "n.envelope.json").read_text())
    assert min(env["db"]) > -98
    assert env["floor_db"] == int(np.percentile(env["db"], 10))
