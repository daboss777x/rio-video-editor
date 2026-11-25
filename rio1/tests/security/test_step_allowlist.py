"""Security tests for helpers/step.py's command allowlist and the run lockdown config.

Refusals are exercised as real subprocesses (exit 126, nothing runs, no timings row).
Acceptance of the SKILL.md command shapes is checked with step.check_command (running them
would call Scribe / ffmpeg); the claude launch is run for real against a fake `claude`.
"""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]  # this system's code root (rio1/ or rio2/)
PROJECT = ROOT.parent  # shared project root: commands run from here
SYS = ROOT.name
sys.path.insert(0, str(ROOT))
from helpers import step  # noqa: E402
from helpers.paths import SKILL_NAME  # noqa: E402

PY = ".venv/bin/python"
STEP = str(ROOT / "helpers" / "step.py")
RUN_SETTINGS = os.path.realpath(ROOT / "config" / "run-settings.json")
E = "runs/2026-01-01_0000_demo/edit"
SRC = "runs/2026-01-01_0000_demo/Screen Recording.mov"
CLAUDE_FLAGS = ["-p", "--model", "sonnet", "--permission-mode", "dontAsk", "--setting-sources", "project",
                "--strict-mcp-config", "--settings", RUN_SETTINGS, "--output-format", "stream-json", "--verbose"]


def base_env(**extra):
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    env["STUDIO_OFF"] = "1"
    env.update(extra)
    return env


def run_step(edit_dir, cmd, env=None):
    return subprocess.run([str(PROJECT / PY), STEP, "--edit-dir", str(edit_dir), "--name", "t", "--", *cmd],
                          cwd=PROJECT, env=env or base_env(), capture_output=True, text=True, timeout=60)


@pytest.fixture
def fake_claude(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    marker = tmp_path / "claude_ran"
    fake = bindir / "claude"
    fake.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 0\n")
    fake.chmod(0o755)
    env = base_env(PATH=f"{bindir}:{os.environ.get('PATH', '/usr/bin:/bin')}")
    return env, marker


# ---------- refusals ----------

def _refusals(tmp_path):
    canary = tmp_path / "canary.txt"
    if tmp_path.is_dir():
        canary.write_text("keep me")
    return {
        "rm": ["rm", "-f", str(canary)],
        "bin_rm": ["/bin/rm", "-f", str(canary)],
        "sh_c": ["sh", "-c", f"rm -f '{canary}'"],
        "bin_bash": ["/bin/bash", "-c", "true"],
        "curl": ["curl", "https://example.com"],
        "python_c": [PY, "-c", "print(1)"],
        "python_m": [PY, "-m", "http.server"],
        "traversal_script": [PY, f"{SYS}/helpers/../runs/evil.py"],
        "traversal_arg": [PY, f"{SYS}/helpers/media/probe.py", f"{SYS}/helpers/../input/x.mp4", "-o", f"{E}/p.json"],
        "script_in_runs": [PY, "runs/evil.py"],
        "url_arg": [PY, f"{SYS}/helpers/media/probe.py", "https://evil.example/x.mp4", "-o", f"{E}/p.json"],
        "ffprobe_url": ["ffprobe", "http://evil.example/x.mp4"],
        "env_arg": [PY, f"{SYS}/helpers/media/probe.py", ".env", "-o", f"{E}/p.json"],
        "env_arg_nested": ["ffprobe", "-i", "runs/../.env"],
        "out_tmp": [PY, f"{SYS}/helpers/media/probe.py", SRC, "-o", "/tmp/x.json"],
        "write_input": [PY, f"{SYS}/helpers/media/probe.py", SRC, "-o", "input/x.json"],
        "ffmpeg_write_input": ["ffmpeg", "-i", SRC, "input/out.mp4"],
        "caffeinate_sh": ["caffeinate", "-i", "/bin/sh", "-c", "true"],
    }


@pytest.mark.parametrize("key", list(_refusals(Path("/nonexistent")).keys()))
def test_refuses(tmp_path, key):
    cmd = _refusals(tmp_path)[key]
    r = run_step(tmp_path, cmd)
    assert r.returncode == step.REJECT_EXIT, (key, r.stderr)
    assert "REFUSED" in r.stderr
    assert not (tmp_path / "timings.jsonl").exists(), "a refused command must not run or be logged"
    assert (tmp_path / "canary.txt").read_text() == "keep me"


def _claude_variants():
    good = ["caffeinate", "-i", "claude", *CLAUDE_FLAGS, "edit the run"]
    i = good.index("--settings") + 1
    other = str(ROOT / "config" / "other.json")
    yield "no_settings", [a for j, a in enumerate(good) if j not in (i - 1, i)]
    yield "other_settings", good[:i] + [other] + good[i + 1:]
    yield "relative_settings", good[:i] + ["config/run-settings.json"] + good[i + 1:]
    yield "traversal_settings", good[:i] + [str(ROOT / "config" / ".." / "config" / "run-settings.json")] + good[i + 1:]
    yield "json_output", [("json" if a == "stream-json" else a) for a in good]
    yield "opus", [("opus" if a == "sonnet" else a) for a in good]
    yield "bypass", [("bypassPermissions" if a == "dontAsk" else a) for a in good]
    yield "extra_flag", good[:-1] + ["--dangerously-skip-permissions", "edit the run"]
    yield "flag_as_prompt", good[:-1] + ["--dangerously-skip-permissions"]
    yield "no_prompt", good[:-1]
    yield "user_sources", [("user,project" if a == "project" else a) for a in good]
    yield "no_strict_mcp", [a for a in good if a != "--strict-mcp-config"]
    yield "path_claude", ["caffeinate", "-i", "/usr/local/bin/claude", *CLAUDE_FLAGS, "edit the run"]


@pytest.mark.parametrize("key,cmd", list(_claude_variants()))
def test_refuses_wrong_claude_launch(tmp_path, fake_claude, key, cmd):
    env, marker = fake_claude
    r = run_step(tmp_path, cmd, env)
    assert r.returncode == step.REJECT_EXIT, (key, r.stderr)
    assert not marker.exists()


def test_refuses_claude_inside_claude_session(tmp_path, fake_claude):
    env, marker = fake_claude
    env["CLAUDECODE"] = "1"
    r = run_step(tmp_path, ["caffeinate", "-i", "claude", *CLAUDE_FLAGS, "edit the run"], env)
    assert r.returncode == step.REJECT_EXIT and "inside a Claude session" in r.stderr
    assert not marker.exists()


def test_refuses_claude_when_run_settings_hash_drifts(monkeypatch):
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.setattr(step, "RUN_SETTINGS_SHA256", "0" * 64)
    with pytest.raises(step.Rejected, match="sha256"):
        step.check_command(["caffeinate", "-i", "claude", *CLAUDE_FLAGS, "edit the run"])


# ---------- acceptance ----------

def test_accepts_exact_claude_launch(tmp_path, fake_claude):
    env, marker = fake_claude
    r = run_step(tmp_path, ["caffeinate", "-i", "claude", *CLAUDE_FLAGS, "Run the tutorial-edit skill on runs/x"], env)
    assert r.returncode == 0, r.stderr
    assert marker.exists(), "the fake claude should have run"
    row = json.loads((tmp_path / "timings.jsonl").read_text().splitlines()[-1])
    assert row["exit_code"] == 0


def test_accepts_true_subprocess(tmp_path):
    r = run_step(tmp_path, ["true"])
    assert r.returncode == 0, r.stderr


SKILL_COMMANDS = [
    [PY, f"{SYS}/helpers/media/probe.py", SRC, "-o", f"{E}/probe.json"],
    [PY, "vendor/video-use/helpers/transcribe.py", SRC, "--edit-dir", E],
    [PY, f"{SYS}/helpers/stt/transcribe.py", SRC, "--edit-dir", E],
    [PY, f"{SYS}/helpers/media/silences.py", SRC, "--out", f"{E}/silences/Screen Recording.json"],
    [PY, "vendor/video-use/helpers/pack_transcripts.py", "--edit-dir", E, "--silence-threshold", "0.5"],
    [PY, f"{SYS}/helpers/media/detect_screen_activity.py", "--video", SRC, "--transcript", f"{E}/transcripts/Screen Recording.json",
     "--out", f"{E}/activity.json", "--profile", f"{SYS}/profiles/tutorial.json"],
    [PY, f"{SYS}/helpers/chunk_edl.py", "split", "--edit-dir", E],
    [PY, f"{SYS}/helpers/chunk_edl.py", "merge", "--edit-dir", E],
    [PY, f"{SYS}/helpers/chunk_edl.py", "validate", "--edit-dir", E],
    [PY, f"{SYS}/helpers/media/render_tutorial.py", f"{E}/edl.json", "-o", f"{E}/final.mp4"],
    [PY, f"{SYS}/helpers/verify_edit.py", "--edit-dir", E],
    [PY, f"{SYS}/helpers/hear_check.py", "--edit-dir", E],
    [PY, f"{SYS}/helpers/media/blur_find.py", "--edit-dir", E, "--sensitive", "--text", "Barbara"],
    [PY, f".claude/skills/{SKILL_NAME}/scripts/edit_tools.py", "stats", "--edit-dir", E],
    ["ffprobe", "-v", "error", "-show_format", f"{E}/final.mp4"],
    ["ffmpeg", "-y", "-i", SRC, "-t", "1", f"{E}/x.mp4"],
    ["caffeinate", "-i", PY, f"{SYS}/helpers/media/render_tutorial.py", f"{E}/edl.json", "-o", f"{E}/final.mp4"],
    [str(PROJECT / PY), str(ROOT / "helpers/verify_edit.py"), "--edit-dir", str(PROJECT / E)],
]


@pytest.mark.parametrize("cmd", SKILL_COMMANDS, ids=lambda c: " ".join(c[:3])[-50:])
def test_accepts_skill_command_shapes(cmd, monkeypatch):
    monkeypatch.chdir(PROJECT)
    step.check_command(cmd)  # raises Rejected on refusal


def test_skill_md_step_commands_are_covered():
    """Every script SKILL.md runs through step.py is one this suite accepts."""
    text = (PROJECT / ".claude" / "skills" / SKILL_NAME / "SKILL.md").read_text()
    import re
    used = set(re.findall(r"-- py (\S+\.py)", text))
    covered = {c[1] for c in SKILL_COMMANDS if c[0] == PY}
    assert used and used <= covered, used - covered


def test_print_run_settings_hash_matches_file():
    r = subprocess.run([str(PROJECT / PY), STEP, "--print-run-settings-hash"], cwd=PROJECT, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    actual = hashlib.sha256(Path(RUN_SETTINGS).read_bytes()).hexdigest()
    assert r.stdout.strip() == actual == step.RUN_SETTINGS_SHA256


# ---------- config ----------

def _perms(path):
    data = json.loads(Path(path).read_text())
    p = data["permissions"]
    return p.get("allow", []), p.get("deny", []), p


def test_run_settings_lockdown():
    allow, deny, p = _perms(RUN_SETTINGS)
    for bad in ("Bash(cat *)", "Bash(ffmpeg *)", "Edit(/**)", "Edit(**)", "Bash(*)", "Bash"):
        assert bad not in allow
    assert not any(a.startswith("Bash(cat") or a.startswith("Bash(ffmpeg") for a in allow)
    # In a --settings file a leading "/" anchors at config/, not the project: no such paths.
    assert not any(r.startswith(("Read(/", "Edit(/")) and not r.startswith(("Read(//", "Edit(//")) for r in allow + deny)
    for need in ("Bash(*.env*)", "Read(**/.env)", "Bash(*../*)", "Bash(jq *-R*)", "Bash(curl *)", "WebFetch"):
        assert need in deny
    assert p.get("disableBypassPermissionsMode") == "disable"


def test_developer_settings_has_no_broad_allows():
    allow, _, _ = _perms(PROJECT / ".claude" / "settings.json")
    assert "Edit(/**)" not in allow
    assert "Bash(ffmpeg *)" not in allow


def test_py_alias_expands_to_venv_python_and_is_still_checked(monkeypatch):
    monkeypatch.chdir(PROJECT)
    cmd = step.expand_py_alias(["py", f"{SYS}/helpers/verify_edit.py", "--edit-dir", E])
    assert cmd[0] == str(PROJECT / PY)
    step.check_command(cmd)
    with pytest.raises(step.Rejected):
        step.check_command(step.expand_py_alias(["py", f"{SYS}/tests/make_fixture.py"]))
    assert step.expand_py_alias(["ffprobe", "py"]) == ["ffprobe", "py"]
