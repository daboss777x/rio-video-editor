import math

import checks
from probe import probe


def test_probe_shape(fixture_paths):
    p = probe(fixture_paths["video"])
    for k in ("path", "duration_s", "width", "height", "avg_fps", "r_fps", "vfr",
              "video_codec", "audio_codec", "audio_channels", "size_bytes"):
        assert k in p
    assert (p["width"], p["height"]) == (1280, 720)
    assert p["vfr"] is False
    assert abs(p["duration_s"] - 124.0) < 0.1
    assert p["video_codec"] == "h264" and p["audio_codec"] == "aac"
    assert checks.probe is probe or checks.probe.__name__ == "probe"


def test_loudness_sane(fixture_paths):
    lo = checks.loudness(fixture_paths["video"])
    assert -40 < lo["integrated_lufs"] < -5
    assert -30 < lo["true_peak_dbtp"] < 0
    assert not math.isinf(lo["integrated_lufs"])


def test_av_durations(fixture_paths):
    d = checks.av_durations(fixture_paths["video"])
    assert abs(d["video_s"] - 124.0) < 0.1
    assert abs(d["audio_s"] - 124.0) < 0.1


def test_probe_detects_vfr(fixture_paths, tmp_path):
    import subprocess
    vfr = tmp_path / "vfr clip.mp4"
    # keep every frame for 1 s, then every 3rd frame: irregular timestamps (VFR)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-t", "12", "-i", str(fixture_paths["video"]),
                    "-vf", "select='lt(n,30)+not(mod(n,3))'", "-fps_mode", "vfr", "-an",
                    "-c:v", "libx264", "-preset", "ultrafast", str(vfr)], check=True)
    assert probe(vfr)["vfr"] is True
