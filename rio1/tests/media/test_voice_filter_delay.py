"""The voice filter's denoiser (afftdn) delays audio ~25 ms; the renderer must cancel that so
speech stays in sync with the picture and the edit keeps its exact length (user, 2026-11-05)."""
import json
import subprocess
from pathlib import Path

import numpy as np

import render_tutorial as rt

ROOT = Path(__file__).resolve().parents[2]
SR = 48000
DUR = 8


def _speechish(path: Path) -> np.ndarray:
    """Noise bursts with a syllable-like envelope: non-stationary, so the denoiser keeps it."""
    rng = np.random.default_rng(7)
    t = np.arange(DUR * SR) / SR
    env = np.clip(np.sin(2 * np.pi * 3.1 * t) * np.sin(2 * np.pi * 0.37 * t + 1), 0, None) ** 2
    tone = np.sin(2 * np.pi * 180 * t) + 0.5 * np.sin(2 * np.pi * 1300 * t)
    x = (0.25 * env * (tone + 0.6 * rng.standard_normal(len(t)))).astype(np.float32)
    subprocess.run(["ffmpeg", "-y", "-nostdin", "-loglevel", "error", "-f", "f32le", "-ar", str(SR),
                    "-ac", "1", "-i", "-", "-c:a", "pcm_s16le", str(path)],
                   input=x.tobytes(), check=True)
    return x.astype(np.float64)


def _decode(path: Path) -> np.ndarray:
    out = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-map", "0:a",
                          "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(out, np.float32).astype(np.float64)


def _lag_ms(ref: np.ndarray, test: np.ndarray, max_ms=60) -> float:
    m = int(SR * max_ms / 1000)
    a = ref[m:-m] - ref[m:-m].mean()
    vals = [float(np.dot(a, test[m + k:m + k + len(a)])) for k in range(-m, m + 1)]
    return (int(np.argmax(vals)) - m) * 1000 / SR


def _profile_chain() -> str:
    vf = json.loads((ROOT / "profiles" / "tutorial.json").read_text())["render"]["voice_filter"]
    assert "afftdn" in vf, "test assumes the profile's chain has the delaying denoiser"
    return vf


def test_filter_delay_samples_measures_a_known_delay(tmp_path):
    src = tmp_path / "src.wav"
    _speechish(src)
    late = tmp_path / "late.wav"
    subprocess.run(["ffmpeg", "-y", "-nostdin", "-loglevel", "error", "-i", str(src),
                    "-af", "adelay=25ms:all=1", str(late)], check=True)
    assert abs(rt.filter_delay_samples(src, late) - 1200) <= 2
    assert rt.filter_delay_samples(src, src) == 0


def test_final_encode_cancels_voice_filter_delay(tmp_path):
    base_a = tmp_path / "base_a.wav"
    ref = _speechish(base_a)
    base_v = tmp_path / "base_v.mp4"
    subprocess.run(["ffmpeg", "-y", "-nostdin", "-loglevel", "error", "-f", "lavfi",
                    "-i", f"color=black:size=320x240:rate=30:duration={DUR}",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(base_v)],
                   check=True)
    out = tmp_path / "final.mp4"
    total = DUR * SR
    rt.final_encode(base_v, base_a, out, total, voice_af=_profile_chain())
    got = _decode(out)
    assert abs(len(got) - total) <= 1024  # AAC frame granularity
    assert abs(_lag_ms(ref, got)) <= 0.5
