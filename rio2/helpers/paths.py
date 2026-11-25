"""Where this system's code lives vs. where the shared project data lives.

The project holds two editing systems side by side (rio1/ = the original Rio,
rio2/ = Rio 2.0). Each system's code is one level below the project root:

    CODE_ROOT    = <project>/rio1 (or rio2): helpers/, profiles/, config/, tests/, docs/, edit.sh
    PROJECT_ROOT = <project>: .env, .venv/, vendor/, history/, input/, output/, Reports/,
                   runs/, cache/, brief.md, glossary.md, .claude/

Everything that needs a path imports it from here instead of counting parents.
No imports beyond the standard library, so any helper can use it.
"""
from __future__ import annotations

from pathlib import Path

CODE_ROOT = Path(__file__).resolve().parent.parent
PROJECT_ROOT = CODE_ROOT.parent
SYSTEM = CODE_ROOT.name  # "rio1" or "rio2"

# Each system is driven by its own orchestrator skill in the shared .claude/skills/.
_SKILLS = {"rio1": "tutorial-edit", "rio2": "tutorial-edit-2"}
SKILL_NAME = _SKILLS.get(SYSTEM, "tutorial-edit")
SKILL_DIR = PROJECT_ROOT / ".claude" / "skills" / SKILL_NAME
