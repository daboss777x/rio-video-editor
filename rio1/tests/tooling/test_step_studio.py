"""step.py Studio hooks: best effort, never affect the step. Also --begin/--end timing."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
STEP = ROOT / "helpers" / "step.py"
sys.path.insert(0, str(ROOT))
from helpers import step  # noqa: E402


def run_step(edit_dir, *args, studio_script=None):
    """The real CLI with the real allowlist: only allowed commands (`true`) go through here."""
    env = dict(os.environ)
    env.pop("STUDIO_OFF", None)
    env["EDITOR_STUDIO_SCRIPT"] = str(studio_script or edit_dir / "no-such-studio.py")
    return subprocess.run([sys.executable, str(STEP), "--edit-dir", str(edit_dir), *args],
                          capture_output=True, text=True, env=env, timeout=60)


@pytest.fixture
def run_sh(monkeypatch, capfd):
    """step.main in-process with the allowlist switched off, for tests that need `sh -c`
    to fake output and exit codes. The allowlist itself is tested in tests/security."""
    monkeypatch.setattr(step, "check_command", lambda cmd: None)
    monkeypatch.delenv("STUDIO_OFF", raising=False)
    capfd.readouterr()

    def run(edit_dir, *args, studio_script=None):
        monkeypatch.setenv("EDITOR_STUDIO_SCRIPT", str(studio_script or edit_dir / "no-such-studio.py"))
        rc = step.main(["--edit-dir", str(edit_dir), *args])
        out, err = capfd.readouterr()
        return subprocess.CompletedProcess(args, rc, out, err)
    return run


def rows(edit_dir):
    return [json.loads(l) for l in (edit_dir / "timings.jsonl").read_text().splitlines()]


def recorder(tmp_path, body=""):
    """A fake studio.py that logs its argv as JSON lines, then runs `body`."""
    log = tmp_path / "studio_calls.jsonl"
    script = tmp_path / "fake_studio.py"
    script.write_text(f"import json,sys\nopen({str(log)!r},'a').write(json.dumps(sys.argv[1:])+'\\n')\n{body}\n")
    return script, log


def calls(log):
    return [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []


def test_missing_studio_script_is_a_silent_noop(tmp_path, run_sh):
    p = run_sh(tmp_path, "--name", "detect", "--studio", "watcher", "--stage", "screen",
                 "--", "sh", "-c", "echo out; exit 4")
    assert p.returncode == 4 and p.stdout == "out\n" and p.stderr == ""
    assert rows(tmp_path)[0]["exit_code"] == 4


def test_studio_calls_wrap_the_command(tmp_path):
    script, log = recorder(tmp_path)
    p = run_step(tmp_path, "--name", "render", "--studio", "render", "--stage", "render", "--task", "final.mp4",
                 "--", "true", studio_script=script)
    assert p.returncode == 0
    c = calls(log)
    assert c[0] == ["stage", "render", "running"]
    assert c[1] == ["busy", "render", "final.mp4"]
    assert c[2][:2] == ["idle", "render"] and "ok" in c[2][2]
    assert c[3] == ["stage", "render", "done"]


def test_failed_step_marks_stage_failed(tmp_path, run_sh):
    script, log = recorder(tmp_path)
    p = run_sh(tmp_path, "--name", "verify", "--studio", "qc", "--stage", "qc", "--", "sh", "-c", "exit 2",
                 studio_script=script)
    assert p.returncode == 2
    assert calls(log)[-1] == ["stage", "qc", "failed"]
    assert "exit 2" in calls(log)[-2][2]


def test_broken_studio_never_changes_the_step(tmp_path, run_sh):
    script, _ = recorder(tmp_path, "sys.stderr.write('boom\\n'); sys.stdout.write('noise\\n'); sys.exit(9)")
    p = run_sh(tmp_path, "--name", "detect", "--studio", "watcher", "--stage", "screen",
                 "--", "sh", "-c", "echo real", studio_script=script)
    assert p.returncode == 0 and p.stdout == "real\n" and "boom" not in p.stderr
    assert len(rows(tmp_path)) == 1


def test_hanging_studio_times_out(tmp_path):
    script, _ = recorder(tmp_path, "import time; time.sleep(30)")
    p = run_step(tmp_path, "--name", "x", "--studio", "lead", "--", "true", studio_script=script)
    assert p.returncode == 0  # each call capped at 5 s, so the step still finishes


def test_no_studio_flags_means_no_calls(tmp_path):
    script, log = recorder(tmp_path)
    assert run_step(tmp_path, "--name", "a", "--", "true", studio_script=script).returncode == 0
    assert calls(log) == []


def test_begin_end_times_a_subagent(tmp_path):
    script, log = recorder(tmp_path)
    assert run_step(tmp_path, "--name", "edl_01", "--studio", "story", "--task", "chunk 1/3", "--begin",
                    studio_script=script).returncode == 0
    assert not (tmp_path / "timings.jsonl").exists()
    assert run_step(tmp_path, "--name", "edl_01", "--studio", "story", "--end", "--exit-code", "1",
                    studio_script=script).returncode == 0
    (row,) = rows(tmp_path)
    assert row["name"] == "edl_01" and row["exit_code"] == 1 and row["max_rss_mb"] is None
    assert set(row) == {"name", "started_at", "wall_s", "max_rss_mb", "exit_code"}
    assert calls(log)[0] == ["busy", "story", "chunk 1/3"]
    assert calls(log)[1][:2] == ["idle", "story"]
    assert not list(tmp_path.glob(".step_*"))


def test_end_without_begin_fails(tmp_path):
    p = run_step(tmp_path, "--name", "verifier", "--end")
    assert p.returncode == 2 and "no --begin marker" in p.stderr
