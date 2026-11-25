"""sync_cam.py: audio sync of a camera file to the screen clip (CONTRACTS "Rio 2.0")."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "helpers" / "media" / "sync_cam.py"

import sync_cam  # noqa: E402


@pytest.mark.parametrize("which", ["cam", "cam_vfr"])
def test_recovers_offset_and_drift(cam_paths, cam_truth, which):
    res = sync_cam.sync([Path(cam_paths[which])], [("sync_screen", Path(cam_paths["screen"]))])
    assert res["ok"] is True, res["notes"]
    c = res["cams"][0]
    assert c["screen"] == "sync_screen"
    assert abs(c["offset_s"] - cam_truth["offset_s"]) < 0.005
    assert abs(c["drift"] - cam_truth["drift"]) < 30e-6
    # what matters for lips: the mapped time anywhere in the covered span
    for t in (1.0, 20.0, 44.0):
        want = cam_truth["offset_s"] + (1 + cam_truth["drift"]) * t
        got = c["offset_s"] + (1 + c["drift"]) * t
        assert abs(got - want) < 0.002, (t, got, want)
    assert c["residual_ms"] < 2.0
    lo, hi = c["covers"]
    assert abs(lo - cam_truth["covers"][0]) < 0.05 and abs(hi - cam_truth["covers"][1]) < 0.1
    assert len(c["points"]) >= 3 and all(len(p) == 3 for p in c["points"])


def test_noise_is_not_ok(cam_paths):
    res = sync_cam.sync([Path(cam_paths["noise_cam"])], [("sync_screen", Path(cam_paths["screen"]))])
    assert res["ok"] is False
    assert res["cams"][0]["residual_ms"] is None
    assert any("no confident" in n for n in res["notes"])


def test_residual_limit_makes_not_ok(cam_paths):
    res = sync_cam.sync([Path(cam_paths["cam"])], [("sync_screen", Path(cam_paths["screen"]))],
                        {"max_residual_ms": 0.0})
    assert res["ok"] is False


def test_picks_first_decodable_audio_stream(cam_paths, tmp_path):
    # video + a silent PCM track first would be decodable; the rule is by codec, never "unknown"
    assert sync_cam._decodable("aac") and sync_cam._decodable("pcm_s16le")
    assert not sync_cam._decodable("unknown") and not sync_cam._decodable("none")
    two = tmp_path / "two audio.mov"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(cam_paths["cam"]), "-i",
                    str(cam_paths["cam"]), "-map", "0:v", "-map", "0:a", "-map", "1:a", "-t", "2",
                    "-c", "copy", str(two)], check=True)
    info = sync_cam.probe_audio(two)
    assert info["index"] == 1 and info["codec"] == "aac"


def test_cli_edit_dir_writes_contract_shape(cam_paths, tmp_path):
    run = tmp_path / "run dir"
    (run / "edit").mkdir(parents=True)
    (run / "screen one.mp4").symlink_to(cam_paths["screen"])
    (run / "deskcam.mov").symlink_to(cam_paths["cam"])
    (run / "edit" / "sources.json").write_text(json.dumps(
        {"order": ["screen one"], "files": {"screen one": "screen one.mp4"},
         "cams": {"deskcam": "deskcam.mov"}}))
    p = subprocess.run([sys.executable, str(SCRIPT), "--edit-dir", str(run / "edit")],
                       capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    d = json.loads((run / "edit" / "cam_sync.json").read_text())
    assert set(d) == {"ok", "cams", "notes"} and d["ok"] is True
    c = d["cams"][0]
    assert set(c) >= {"cam", "file", "screen", "offset_s", "drift", "residual_ms", "covers", "points"}
    assert (c["cam"], c["file"], c["screen"]) == ("deskcam", "deskcam.mov", "screen one")
