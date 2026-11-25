"""Two-camera mode plumbing (docs/CONTRACTS.md "Rio 2.0: two-camera 4K mode"): intake's camera
classification, edit.sh's estimate/prepare/finish with a camera file, and the revise path leaving
E/cam_sync.json alone. No Claude call, no transcription, no render."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]  # rio2/
PROJECT = ROOT.parent
EDIT = ROOT / "edit.sh"
SKILL_DIR = PROJECT / ".claude" / "skills" / "tutorial-edit-2"
sys.path.insert(0, str(SKILL_DIR / "scripts"))
import intake  # noqa: E402

PYBIN = str(PROJECT / ".venv" / "bin" / "python")
INTAKE = str(SKILL_DIR / "scripts" / "intake.py")


def touch(d, *names):
    for n in names:
        (d / n).write_bytes(b"x" + n.encode())


# ---------- intake ----------

def test_cams_are_split_from_the_clips(tmp_path):
    touch(tmp_path, "tdevsetupintro.mp4", "tdevsetupmain1.mp4", "tdevsetupcam.MOV", "Cam2.mov")
    s = intake.scan(tmp_path)
    assert s["order"] == ["tdevsetupintro", "tdevsetupmain1"]
    assert set(s["files"]) == {"tdevsetupintro", "tdevsetupmain1"}
    assert s["cams"] == {"Cam2": "Cam2.mov", "tdevsetupcam": "tdevsetupcam.MOV"}
    assert s["n"] == 2 and s["n_cams"] == 2 and s["cam_bytes"] > 0
    assert s["out_stem"] == "tdevsetup"                 # the camera doesn't spoil the shared name
    assert "cam" not in s["order_line"] and len(s["paths"]) == 2
    assert s["label"].endswith("+ camera")


def test_cam_only_input_is_refused(tmp_path):
    touch(tmp_path, "tdevsetupcam.MOV")
    with pytest.raises(intake.Refuse, match="only camera file"):
        intake.scan(tmp_path)
    r = subprocess.run([PYBIN, INTAKE, "scan", str(tmp_path)], capture_output=True, text=True)
    assert r.returncode == 1 and r.stderr.count("\n") == 1 and "screen recording" in r.stderr


def test_duplicate_stem_across_cam_and_clip_is_refused(tmp_path):
    touch(tmp_path, "cam.mp4", "CAM.mov", "main.mp4")
    with pytest.raises(intake.Refuse, match="share the name"):
        intake.scan(tmp_path)


def test_no_camera_is_unchanged(tmp_path):
    touch(tmp_path, "My Clip.mp4")
    s = intake.scan(tmp_path)
    assert s["label"] == "My Clip.mp4" and s["cams"] == {} and s["n_cams"] == 0 and s["cam_bytes"] == 0
    r = subprocess.run([PYBIN, INTAKE, "scan", str(tmp_path)], capture_output=True, text=True)
    assert r.stdout.strip() == "order: My Clip.mp4  | no plan doc"


def test_prepare_moves_cams_and_writes_sources_but_never_caches_them(tmp_path):
    inp, run, cache = tmp_path / "in", tmp_path / "run", tmp_path / "cache"
    inp.mkdir(); cache.mkdir()
    touch(inp, "main1.mp4", "facecam.mov")
    cam_key = intake.cache_key(inp / "facecam.mov")
    (cache / f"{cam_key}.json").write_text('{"words": []}')   # even a cache "hit" is never restored
    intake.prepare(inp, run, cache)
    e = run / "edit"
    src = json.loads((e / "sources.json").read_text())
    assert src == {"order": ["main1"], "files": {"main1": "main1.mp4"}, "plan": None,
                   "cams": {"facecam": "facecam.mov"}}
    assert (run / "facecam.mov").exists() and list(inp.iterdir()) == []
    assert not (e / "transcripts" / "facecam.json").exists()
    (e / "transcripts" / "main1.json").write_text('{"words": []}')
    shutil.rmtree(cache); cache.mkdir()
    assert intake.store(run, cache) == 1                         # the clip only
    assert not (cache / f"{cam_key}.json").exists()
    rp = subprocess.run([PYBIN, INTAKE, "runpaths", str(run)], capture_output=True, text=True).stdout
    cp = subprocess.run([PYBIN, INTAKE, "runcampaths", str(run)], capture_output=True, text=True).stdout
    assert rp.split("\0")[:-1] == [str(run / "main1.mp4")]
    assert cp.split("\0")[:-1] == [str(run / "facecam.mov")]


def test_campaths_and_old_sources_json(tmp_path):
    touch(tmp_path, "main1.mp4", "facecam.mov")
    out = subprocess.run([PYBIN, INTAKE, "campaths", str(tmp_path)], capture_output=True, text=True).stdout
    assert out == str(tmp_path / "facecam.mov") + "\0"
    run = tmp_path / "r"
    (run / "edit").mkdir(parents=True)
    (run / "edit" / "sources.json").write_text('{"order": [], "files": {}, "plan": null}')  # a run before cams
    r = subprocess.run([PYBIN, INTAKE, "runcampaths", str(run)], capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout == ""


# ---------- edit.sh ----------

@pytest.fixture
def dirs(tmp_path):
    d = {k: tmp_path / k for k in ("input", "runs", "output")}
    for p in d.values():
        p.mkdir()
    return d


def run(dirs, *args, extra_env=None, timeout=180):
    env = dict(os.environ, EDITOR_INPUT_DIR=str(dirs["input"]), EDITOR_RUNS_DIR=str(dirs["runs"]),
               EDITOR_OUTPUT_DIR=str(dirs["output"]), EDITOR_STUDIO_SCRIPT=str(dirs["input"].parent / "no-studio.py"))
    env.pop("CLAUDECODE", None)
    env.update(extra_env or {})
    return subprocess.run(["bash", str(EDIT), *map(str, args)], capture_output=True, text=True, env=env, timeout=timeout)


def make_clip(path, dur=2):
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"testsrc=size=320x180:rate=30:duration={dur}",
                    "-f", "lavfi", "-i", f"sine=frequency=440:duration={dur}", "-shortest",
                    "-c:v", "libx264", "-c:a", "aac", str(path)], check=True)


needs_media = pytest.mark.skipif(not shutil.which("ffmpeg") or not (ROOT / "helpers" / "estimate.py").exists(),
                                 reason="needs ffmpeg and helpers/estimate.py")
ENV = {"EDITOR_SKIP_PREFLIGHT": "1", "EDITOR_NO_RECORD": "1", "MONITOR_INTERVAL_S": "0.2"}


@needs_media
def test_estimate_shows_the_camera_and_moves_nothing(dirs):
    make_clip(dirs["input"] / "tdevsetupmain1.mp4")
    shutil.copy(dirs["input"] / "tdevsetupmain1.mp4", dirs["input"] / "tdevsetupcam.mp4")
    p = run(dirs, "--estimate")
    out = p.stdout + p.stderr
    assert p.returncode == 0, out
    assert "order: tdevsetupmain1.mp4  | no plan doc  | camera: tdevsetupcam.mp4" in out
    assert "COMPOSITE: 1 camera file(s)" in out          # edit.sh passed --cam to estimate.py
    assert sorted(x.name for x in dirs["input"].iterdir()) == ["tdevsetupcam.mp4", "tdevsetupmain1.mp4"]


@needs_media
def test_estimate_passes_one_cam_flag_per_camera_file(dirs):
    make_clip(dirs["input"] / "main1.mp4")
    for n in ("facecam.mp4", "Cam 2.mov"):                  # a space in the name must survive the NUL list
        shutil.copy(dirs["input"] / "main1.mp4", dirs["input"] / n)
    p = run(dirs, "--estimate")
    out = p.stdout + p.stderr
    assert p.returncode == 0, out
    assert "COMPOSITE: 2 camera file(s)" in out


@needs_media
def test_estimate_without_a_camera_is_not_composite(dirs):
    make_clip(dirs["input"] / "main1.mp4")
    p = run(dirs, "--estimate")
    assert p.returncode == 0, p.stdout + p.stderr
    assert "COMPOSITE" not in p.stdout and "sync " not in p.stdout


def test_estimate_refuses_a_camera_alone(dirs):
    touch(dirs["input"], "tdevsetupcam.MOV")
    p = run(dirs, "--estimate")
    assert p.returncode == 1 and "only camera file" in p.stderr


@needs_media
def test_prepare_with_a_camera_then_finish_copies_layout_stills(dirs):
    make_clip(dirs["input"] / "main1.mp4")
    shutil.copy(dirs["input"] / "main1.mp4", dirs["input"] / "facecam.mp4")
    p = run(dirs, "--prepare", extra_env=ENV)
    out = p.stdout + p.stderr
    assert p.returncode == 0, out
    assert "RIO_CLIPS=main1.mp4" in p.stdout and "RIO_CAMS=facecam.mp4" in p.stdout
    (run_dir,) = list(dirs["runs"].iterdir())
    e = run_dir / "edit"
    assert json.loads((e / "sources.json").read_text())["cams"] == {"facecam": "facecam.mp4"}
    assert (run_dir / "facecam.mp4").exists()
    est = json.loads((e / "estimate.json").read_text())
    assert est["source"]["duration_s"] < 3                          # the camera isn't counted as a clip
    if '"--cam"' in (ROOT / "helpers" / "estimate.py").read_text():
        assert "COMPOSITE" in out
    (e / "final.mp4").write_bytes(b"x")
    (e / "layout").mkdir()
    for n in ("at_0120.png", "at_0005.png"):
        (e / "layout" / n).write_bytes(b"png")
    p = run(dirs, "--finish", run_dir, "0", extra_env=ENV)
    assert p.returncode == 0, p.stdout + p.stderr
    stem = json.loads((e / "rio.json").read_text())["stem"]
    assert (dirs["output"] / f"{stem}_layout_1.png").exists() and (dirs["output"] / f"{stem}_layout_2.png").exists()
    assert not (dirs["output"] / f"{stem}_layout_3.png").exists()


@needs_media
def test_prepare_without_a_camera_prints_no_cams_line(dirs):
    make_clip(dirs["input"] / "main1.mp4")
    p = run(dirs, "--prepare", extra_env=ENV)
    assert p.returncode == 0, p.stdout + p.stderr
    assert "RIO_CAMS=" not in p.stdout and "| camera:" not in p.stdout
    (run_dir,) = list(dirs["runs"].iterdir())
    assert json.loads((run_dir / "edit" / "sources.json").read_text())["cams"] == {}
    run(dirs, "--finish", run_dir, "1", extra_env=ENV)   # stop the monitor
    assert not list(dirs["output"].glob("*_layout_*.png"))   # no layout stills, nothing copied


def test_disk_preflight_counts_the_camera(dirs):
    """Without EDITOR_SKIP_PREFLIGHT the disk need grows by 1.5x the camera size (rounded up to GB).
    Simulated: a sparse 'camera' far larger than any free disk must make preflight refuse on disk."""
    touch(dirs["input"], "main1.mp4")
    cam = dirs["input"] / "facecam.mov"
    with open(cam, "wb") as f:
        f.truncate(1 << 50)   # 1 PiB sparse file: takes no real space
    fake = dirs["input"].parent / "bin"
    fake.mkdir()
    (fake / "pmset").write_text("#!/bin/sh\necho \"Now drawing from 'AC Power'\"\n")   # pretend the charger is in
    (fake / "pmset").chmod(0o755)
    p = run(dirs, "--dry-run", extra_env={"EDITOR_NO_RECORD": "1", "PATH": f"{fake}:{os.environ.get('PATH', '')}"},
            timeout=60)
    err = p.stderr
    assert p.returncode == 1 and "(camera included)" in err, err
    assert cam.exists()   # refused before anything moved


def test_revise_paths_never_delete_cam_sync():
    """The revise render reuses E/cam_sync.json from the first run: nothing on the revise path may remove it."""
    for f in (EDIT, ROOT / "helpers" / "revise.py", SKILL_DIR / "REVISE.md"):
        for line in f.read_text().splitlines():
            if any(k in line for k in ("rm ", "unlink", "rmtree", "-delete", "remove(")):
                assert "cam_sync" not in line and "edit/*" not in line, f"{f.name}: {line.strip()}"


# ---------- step names ----------

def test_sync_step_is_timed_and_counts_as_sync(tmp_path, monkeypatch, capfd):
    """step.py accepts any --name; record_run files `sync_cam` under the estimator's `sync` step and
    leaves `layout_preview` out of `render` (stills must not inflate the composite render prior)."""
    sys.path.insert(0, str(ROOT))
    from helpers import record_run, step
    monkeypatch.setattr(step, "check_command", lambda cmd: None)
    monkeypatch.setenv("EDITOR_STUDIO_SCRIPT", "/nonexistent/no-studio.py")
    for name in ("sync", "sync_cam"):
        assert step.main(["--edit-dir", str(tmp_path), "--name", name, "--", "true"]) == 0
    rows = [json.loads(l) for l in (tmp_path / "timings.jsonl").read_text().splitlines()]
    assert [r["name"] for r in rows] == ["sync", "sync_cam"] and all(r["exit_code"] == 0 for r in rows)
    assert record_run.step_of("sync_cam") == "sync" and record_run.step_of("sync") == "sync"
    # Rio 2.0 layout steps are forecast steps of their own (2026-11-10); detect_slides is not screen detection
    assert record_run.step_of("layout_preview") == "layout"
    assert record_run.step_of("detect_slides") == "slides" and record_run.step_of("face_track") == "face_track"
    assert record_run.step_of("detect_mainscreen1") == "detect"


def test_skill_uses_the_step_names_the_history_expects():
    md = (SKILL_DIR / "SKILL.md").read_text()
    assert "--name sync_cam " in md and "--name layout_preview " in md and "--name render_layout" not in md
