#!/usr/bin/env python3
"""Turn `claude -p` output (json result, or the stream-json file) into E/tokens.json.

Usage:
    .venv/bin/python rio1/helpers/tokens.py --result E/claude_result.json --edit-dir E
        [--cwd <dir claude ran in>] [--projects-dir ~/.claude/projects]

Findings (Claude Code 2.1.305, measured 2026-10-28):
- The result's top-level `usage` covers the MAIN thread only (it equals the
  main-thread `iterations`). Sub-agent tokens are not in it.
- `modelUsage` / `total_cost_usd` are the session cost tracker: every API call,
  including small Haiku side calls (titles etc.). Not split by agent.
- Transcripts: main thread  = <projects>/<slug>/<session_id>.jsonl
               sub-agents   = <projects>/<slug>/<session_id>/subagents/agent-*.jsonl
  where <slug> = cwd with every non-alphanumeric char replaced by "-".
  Each assistant API response is written as one line per content block, all
  carrying the same message.id and usage, so we dedupe by message.id.

So: main and subagents are summed from transcripts when they exist
(source="transcripts"); otherwise main comes from the result's `usage` and
subagents are zero (source="claude-json").
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

KEYS = ("input", "output", "cache_read", "cache_creation")
USAGE_MAP = {
    "input": "input_tokens",
    "output": "output_tokens",
    "cache_read": "cache_read_input_tokens",
    "cache_creation": "cache_creation_input_tokens",
}


def zero() -> dict:
    return {k: 0 for k in KEYS}


def from_usage(u: dict | None) -> dict:
    u = u or {}
    return {k: int(u.get(src) or 0) for k, src in USAGE_MAP.items()}


def add(a: dict, b: dict) -> dict:
    return {k: a[k] + b[k] for k in KEYS}


def project_slug(cwd: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def sum_transcript(path: Path) -> dict:
    """Sum assistant usage in one transcript, deduped by message.id (last line wins)."""
    by_id: dict[str, dict] = {}
    anon = zero()
    with open(path, errors="replace") as f:
        for line in f:
            if '"usage"' not in line:
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            m = o.get("message")
            if o.get("type") != "assistant" or not isinstance(m, dict) or not m.get("usage"):
                continue
            u = from_usage(m["usage"])
            if m.get("id"):
                by_id[m["id"]] = u
            else:
                anon = add(anon, u)
    total = anon
    for u in by_id.values():
        total = add(total, u)
    return total


def find_session(projects_dir: Path, cwd: str, session_id: str) -> Path | None:
    """Return the dir holding <session_id>.jsonl (preferring the cwd's slug)."""
    preferred = projects_dir / project_slug(cwd)
    if (preferred / f"{session_id}.jsonl").exists():
        return preferred
    if projects_dir.is_dir():
        for d in projects_dir.iterdir():
            if (d / f"{session_id}.jsonl").exists():
                return d
    return None


def build(result: dict, projects_dir: Path, cwd: str) -> dict:
    session_id = result.get("session_id")
    main = from_usage(result.get("usage"))
    subs = zero()
    source = "claude-json"
    proj = find_session(projects_dir, cwd, session_id) if session_id else None
    if proj is not None:
        main = sum_transcript(proj / f"{session_id}.jsonl")
        sub_dir = proj / session_id / "subagents"
        if sub_dir.is_dir():
            for p in sorted(sub_dir.glob("*.jsonl")):
                subs = add(subs, sum_transcript(p))
        source = "transcripts"
    return {
        "main": main,
        "subagents": subs,
        "total": sum(main.values()) + sum(subs.values()),
        "num_turns": int(result.get("num_turns") or 0),
        "cost_usd_equiv": round(float(result.get("total_cost_usd") or 0.0), 4),
        "source": source,
    }


def load_result(path: Path) -> dict:
    """The final {"type":"result"} object from a result JSON, a JSON array of messages,
    or a stream-json (JSONL) file. Raises ValueError when there is none."""
    text = path.read_text(errors="replace")
    try:
        items = [json.loads(text)]
    except json.JSONDecodeError:
        items = []
        for line in text.splitlines():
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    if len(items) == 1 and isinstance(items[0], list):
        items = items[0]
    if len(items) == 1 and isinstance(items[0], dict):
        return items[0]  # a bare result object (older --output-format json)
    for x in reversed(items):
        if isinstance(x, dict) and x.get("type") == "result":
            return x
    raise ValueError("no result object found")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Write E/tokens.json from claude -p JSON output")
    ap.add_argument("--result", required=True, type=Path)
    ap.add_argument("--edit-dir", required=True, type=Path)
    ap.add_argument("--cwd", default=os.getcwd(), help="dir claude ran in (sets the transcript slug)")
    ap.add_argument("--projects-dir", type=Path,
                    default=Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) / "projects")
    a = ap.parse_args(argv)
    try:
        result = load_result(a.result)
    except (OSError, ValueError) as e:
        print(f"tokens.py: cannot read result JSON {a.result}: {e}", file=sys.stderr)
        return 1
    out = build(result, a.projects_dir, os.path.abspath(a.cwd))
    (a.edit_dir / "tokens.json").write_text(json.dumps(out, indent=2) + "\n")
    print(f"tokens: total {out['total']:,} (main {sum(out['main'].values()):,}, "
          f"subagents {sum(out['subagents'].values()):,}) source={out['source']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
