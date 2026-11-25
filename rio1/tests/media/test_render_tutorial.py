import json
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "helpers" / "media" / "render_tutorial.py"

import checks  # noqa: E402
from probe import probe  # noqa: E402


def test_render_is_4k_16x9_and_keeps_duration(fixture_paths, tmp_path):
    """User, 2026-11-19: every Rio 1 video comes out 3840x2160, whatever the recording's size."""
    edit = tmp_path / "run dir" / "edit"   # space in path on purpose
    edit.mkdir(parents=True)
    ranges = [{"source": "clip", "start": 3.0, "end": 9.0},
              {"source": "clip", "start": 26.0, "end": 30.5},
              {"source": "clip", "start": 44.0, "end": 51.37}]
    edl = {"version": 1, "sources": {"clip": str(fixture_paths["video"])}, "ranges": ranges,
           "grade": "none", "overlays": []}
    (edit / "edl.json").write_text(json.dumps(edl))
    out = edit / "final.mp4"
    subprocess.run([sys.executable, str(SCRIPT), str(edit / "edl.json"), "-o", str(out),
                    "--threads", "2"], check=True)
    p = probe(out)
    src = probe(fixture_paths["video"])
    assert (p["width"], p["height"]) == (3840, 2160)
    assert p["avg_fps"] == src["avg_fps"]
    want = sum(r["end"] - r["start"] for r in ranges)
    assert abs(p["duration_s"] - want) <= 0.5, (p["duration_s"], want)
    d = checks.av_durations(out)
    assert abs(d["video_s"] - d["audio_s"]) < 0.1
    lo = checks.loudness(out)
    assert lo["true_peak_dbtp"] <= -0.5
    assert not (edit / "clips_tutorial").exists() and not (edit / "base.mp4").exists()


def test_many_short_ranges_no_drift(fixture_paths, tmp_path):
    """45 short ranges: A/V stay in step and total length is the sum of frame-aligned ranges."""
    edit = tmp_path / "many dir" / "edit"
    edit.mkdir(parents=True)
    src = probe(fixture_paths["video"])
    fps = float(Fraction(src["avg_fps"]))
    ranges = []
    for i in range(45):
        s = 1.0 + i * 1.07
        ranges.append({"source": "clip", "start": round(s, 3), "end": round(s + 0.3 + (i % 7) * 0.113, 3)})
    edl = {"version": 1, "sources": {"clip": str(fixture_paths["video"])}, "ranges": ranges,
           "grade": "none", "overlays": []}
    (edit / "edl.json").write_text(json.dumps(edl))
    out = edit / "final.mp4"
    subprocess.run([sys.executable, str(SCRIPT), str(edit / "edl.json"), "-o", str(out),
                    "--threads", "2"], check=True)
    d = checks.av_durations(out)
    assert abs(d["video_s"] - d["audio_s"]) <= 0.025, d
    want = sum(round((r["end"] - r["start"]) * fps) / fps for r in ranges)
    assert abs(d["video_s"] - want) <= 1 / fps + 1e-3, (d, want)
    assert checks.loudness(out)["true_peak_dbtp"] <= -1.0


def _synth(path, size, fps, channels, dur=6, freq=440):
    lav = ["-f", "lavfi", "-i", f"testsrc2=size={size}:rate={fps}:duration={dur}",
           "-f", "lavfi", "-i", f"sine=frequency={freq}:sample_rate=44100:duration={dur}"]
    subprocess.run(["ffmpeg", "-y", "-nostdin", "-loglevel", "error", *lav, "-ac", str(channels),
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", str(path)], check=True)


def test_probe_two_clips(tmp_path):
    from probe import probe_many
    a, b = tmp_path / "a clip.mp4", tmp_path / "b.mp4"
    _synth(a, "320x180", 30, 2, dur=3)
    _synth(b, "160x90", 25, 1, dur=2)
    out = tmp_path / "e" / "probe.json"
    subprocess.run([sys.executable, str(ROOT / "helpers" / "media" / "probe.py"),
                    str(a), str(b), "-o", str(out)], check=True, capture_output=True)
    p = json.loads(out.read_text())
    assert list(p["clips"]) == ["a clip", "b"]
    assert (p["width"], p["height"], p["avg_fps"]) == (320, 180, "30/1")
    assert p["audio_channels"] == 2 and p["clips"]["b"]["audio_channels"] == 1
    assert abs(p["duration_s"] - sum(c["duration_s"] for c in p["clips"].values())) < 0.01
    assert p["size_bytes"] == a.stat().st_size + b.stat().st_size
    assert p["vfr"] is False
    assert probe_many([a])["clips"].keys() == {"a clip"}


def test_render_mixed_sources(tmp_path):
    """720p30 stereo + 360p25 mono -> first source's size/fps (render.output.size null: the
    recording's own size, the old Rio), stereo 48k, in sync."""
    edit = tmp_path / "mixed dir" / "edit"
    edit.mkdir(parents=True)
    a, b = edit / "intro.mp4", edit / "main1.mp4"
    _synth(a, "1280x720", 30, 2)
    _synth(b, "640x360", 25, 1, freq=660)
    ranges = [{"source": "intro", "start": 0.5, "end": 3.0},
              {"source": "main1", "start": 1.0, "end": 4.3},
              {"source": "intro", "start": 4.0, "end": 5.5}]
    edl = {"version": 1, "sources": {"intro": "intro.mp4", "main1": "main1.mp4"},
           "ranges": ranges, "grade": "none", "overlays": []}
    (edit / "edl.json").write_text(json.dumps(edl))
    prof = json.loads((ROOT / "profiles" / "tutorial.json").read_text())
    prof["render"]["output"] = {"size": None}
    (edit / "own_size.json").write_text(json.dumps(prof))
    out = edit / "final.mp4"
    subprocess.run([sys.executable, str(SCRIPT), str(edit / "edl.json"), "-o", str(out),
                    "--threads", "2", "--profile", str(edit / "own_size.json")], check=True, capture_output=True)
    p = probe(out)
    assert (p["width"], p["height"], p["avg_fps"]) == (1280, 720, "30/1")
    assert p["audio_channels"] == 2
    d = checks.av_durations(out)
    assert abs(d["video_s"] - d["audio_s"]) <= 0.025, d
    want = (round(2.5 * 30) + round(3.3 * 30) + round(1.5 * 30)) / 30
    assert abs(d["video_s"] - want) <= 1 / 30 + 1e-3, (d, want)
    assert checks.loudness(out)["true_peak_dbtp"] <= -1.0
