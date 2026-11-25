import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
STEP = ROOT / "helpers" / "step.py"
sys.path.insert(0, str(ROOT))
from helpers import step  # noqa: E402


@pytest.fixture
def run_step(monkeypatch, capfd):
    """Run step.main in-process with the command allowlist switched off, so these timing
    tests can use `sh -c`. The allowlist itself is tested in tests/security (Leah)."""
    monkeypatch.setattr(step, "check_command", lambda cmd: None)
    monkeypatch.setenv("EDITOR_STUDIO_SCRIPT", "/nonexistent/no-studio.py")
    capfd.readouterr()

    def run(edit_dir, name, *cmd):
        rc = step.main(["--edit-dir", str(edit_dir), "--name", name, "--", *cmd])
        out, err = capfd.readouterr()
        return subprocess.CompletedProcess(cmd, rc, out, err)
    return run


def timings(edit_dir):
    return [json.loads(l) for l in (edit_dir / "timings.jsonl").read_text().splitlines()]


def test_records_wall_time_and_rss(tmp_path, run_step):
    p = run_step(tmp_path, "sleepy", "sh", "-c", "sleep 0.3; echo hello; echo oops >&2")
    assert p.returncode == 0
    assert p.stdout == "hello\n"          # stdout passed through untouched
    assert "oops" in p.stderr and "maximum resident" not in p.stderr  # time report kept out
    (row,) = timings(tmp_path)
    assert row["name"] == "sleepy"
    assert set(row) == {"name", "started_at", "wall_s", "max_rss_mb", "exit_code"}
    assert 0.25 <= row["wall_s"] < 5
    assert row["max_rss_mb"] and row["max_rss_mb"] > 0
    assert row["exit_code"] == 0


def test_passes_exit_code_through_and_appends(tmp_path, run_step):
    assert run_step(tmp_path, "a", "true").returncode == 0
    assert run_step(tmp_path, "b", "sh", "-c", "exit 3").returncode == 3
    rows = timings(tmp_path)
    assert [r["name"] for r in rows] == ["a", "b"]
    assert rows[1]["exit_code"] == 3


def test_missing_command_is_127(tmp_path, run_step):
    p = run_step(tmp_path, "nope", "/nonexistent/binary")
    assert p.returncode == 127
    assert timings(tmp_path)[0]["exit_code"] == 127


def test_bad_edit_dir_fails(tmp_path):
    # Real CLI, real allowlist (`true` is allowed).
    p = subprocess.run([sys.executable, str(STEP), "--edit-dir", str(tmp_path / "missing"), "--name", "x",
                        "--", "true"], capture_output=True, text=True)
    assert p.returncode != 0 and "edit dir not found" in p.stderr
