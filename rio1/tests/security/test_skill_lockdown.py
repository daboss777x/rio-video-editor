"""Lockdown simulation: every command tutorial-edit's SKILL.md tells the headless run to type must
be allowed by config/run-settings.json (and hit no deny rule), and every step.py-wrapped command must
pass step.py's own allowlist. Covers the blur step (blur_find, sensitive.json, blurs.json).

The Bash rule matcher is a simulation of Claude Code's: `Bash(<glob>)`, `*` matches anything, deny
beats allow, anything not allowed is denied (headless runs use --permission-mode dontAsk).
"""
import fnmatch
import json
import re
import shlex
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]  # rio1/
PROJECT = ROOT.parent
SYS = ROOT.name
sys.path.insert(0, str(ROOT))
from helpers import step  # noqa: E402
from helpers.paths import SKILL_NAME  # noqa: E402

SKILL_MD = PROJECT / ".claude" / "skills" / SKILL_NAME / "SKILL.md"
RUN_SETTINGS = ROOT / "config" / "run-settings.json"
RUN = "runs/2026-11-03_2300_tdevsetup"
PREFIXES = (".venv/bin/python ", "jq ", "ls ", "ffprobe ", "mkdir -p ", "python3 .claude/")


def _rules():
    p = json.loads(RUN_SETTINGS.read_text())["permissions"]
    bash = lambda rules: [r[5:-1] for r in rules if r.startswith("Bash(") and r.endswith(")")]
    return bash(p["allow"]), bash(p["deny"])


def permitted(cmd: str) -> bool:
    """Each part of a ` ; ` chain (the SKILL's studio.py + next-step pattern) must be allowed."""
    allow, deny = _rules()
    for part in cmd.split(" ; "):
        if any(fnmatch.fnmatchcase(part, d) for d in deny) or not any(fnmatch.fnmatchcase(part, a) for a in allow):
            return False
    return True


def concrete(cmd: str) -> str:
    """Fill SKILL.md's placeholders the way the orchestrator does."""
    cmd = cmd.replace('"E/', f'"{RUN}/edit/').replace('"E"', f'"{RUN}/edit"').replace('"RUN/', f'"{RUN}/')
    cmd = re.sub(r"<[^>]+>", "tdevsetupmain1.mp4", cmd)
    cmd = cmd.replace(" ...", "").replace("STEM", "tdevsetupmain1").replace(" PS", " 0.5")
    return cmd


def skill_commands() -> list[str]:
    spans = re.findall(r"`([^`\n]+)`", SKILL_MD.read_text())
    return sorted({concrete(s) for s in spans if s.startswith(PREFIXES) and " -- <cmd" not in s
                   and "..." not in s.split(" -- ")[0] and " [--" not in s})


BLUR_COMMANDS = [   # Rio 1 blur step (2026-11-19); the --text form has "[--" so skill_commands() skips it
    f'.venv/bin/python {SYS}/helpers/step.py --edit-dir "{RUN}/edit" --name blur_find --studio watcher --stage screen '
    f'--task "Blurring sensitive text" -- py {SYS}/helpers/media/blur_find.py --edit-dir "{RUN}/edit" --sensitive',
    f"jq -c '.' \"{RUN}/edit/sensitive.json\"",
    f"jq -c '[.blurs[] | {{text, source, start, end}}]' \"{RUN}/edit/blurs.json\"",
]


def test_skill_md_has_the_blur_commands():
    text = SKILL_MD.read_text()
    assert "blur_find.py --edit-dir \"E\" --sensitive [--text" in text
    cmds = skill_commands()
    for c in BLUR_COMMANDS[1:]:
        assert c in cmds, f"SKILL.md no longer says: {c}"
    assert not re.search(r"`grep ", text)                  # a shell grep command is denied headless


@pytest.mark.parametrize("cmd", BLUR_COMMANDS, ids=lambda c: c[:60])
def test_blur_commands_are_allowed_by_the_lockdown(cmd, monkeypatch):
    assert permitted(cmd), f"run-settings.json would deny: {cmd}"
    if " -- py " in cmd:
        monkeypatch.chdir(PROJECT)
        step.check_command(step.expand_py_alias(shlex.split(cmd.split(" -- ", 1)[1])))


@pytest.mark.parametrize("cmd", skill_commands(), ids=lambda c: c[:70])
def test_every_skill_command_is_allowed_by_the_lockdown(cmd):
    assert permitted(cmd), f"run-settings.json would deny: {cmd}"
    unquoted = re.sub(r"'[^']*'|\"[^\"]*\"", "", cmd)
    assert not re.search(r"&&|\||(?<! );|&\s*$", unquoted), f"one command per call: {cmd}"


@pytest.mark.parametrize("cmd", [c for c in skill_commands() if " -- py " in c], ids=lambda c: c.split(" -- py ")[1][:60])
def test_step_wrapped_commands_pass_step_allowlist(cmd, monkeypatch):
    inner = shlex.split(cmd.split(" -- ", 1)[1])
    monkeypatch.chdir(PROJECT)
    step.check_command(step.expand_py_alias(inner))


def test_simulator_denies_what_the_lockdown_denies():
    for bad in (f'cat "{RUN}/edit/blurs.json"', f'ffmpeg -i "{RUN}/x.mp4" x.mp4', f'grep -n -i blur "{RUN}/edit/takes_packed.md"',
                f'rm "{RUN}/edit/blurs.json"', "jq -R . .env", f'.venv/bin/python {SYS}/../x.py'):
        assert not permitted(bad), bad
    assert permitted(f'.venv/bin/python {SYS}/helpers/media/blur_find.py --edit-dir "{RUN}/edit" --sensitive')


def test_developer_settings_allow_the_blur_finder():
    """The /rio chat session uses .claude/settings.json: blur_find.py sits under rio1/helpers/."""
    allow = json.loads((PROJECT / ".claude" / "settings.json").read_text())["permissions"]["allow"]
    cmd = f'.venv/bin/python {SYS}/helpers/media/blur_find.py --edit-dir "{RUN}/edit" --sensitive'
    assert any(fnmatch.fnmatchcase(cmd, r[5:-1]) for r in allow if r.startswith("Bash("))
