#!/usr/bin/env python3
"""Run one pipeline step under /usr/bin/time -l and log wall time + peak RSS.

Usage:
    .venv/bin/python rio2/helpers/step.py --edit-dir E --name <step> [studio flags] -- <cmd...>
    .venv/bin/python rio2/helpers/step.py --edit-dir E --name <step> [studio flags] --begin
    .venv/bin/python rio2/helpers/step.py --edit-dir E --name <step> [studio flags] --end [--exit-code N]
(Run from the project root. Each system, rio1/ = Rio and rio2/ = Rio 2.0, has its own copy
of this file, and each copy allows only its own system's helpers.)

Appends {"name", "started_at", "wall_s", "max_rss_mb", "exit_code"} to
E/timings.jsonl (see docs/CONTRACTS.md, "Measurement"). The command's stdout and
stderr are inherited (never captured, so nothing is buffered in RAM) and its exit
code is passed through. /usr/bin/time writes to a temp file via -o so its report
never mixes with the command's stderr.

--begin / --end time work that is not a shell command (a sub-agent call): --begin
drops a marker in E, --end appends the row (max_rss_mb null) and removes it.

Studio hooks (optional; a live view, see .claude/skills/studio):
    --studio <cast_id>   mark that crew member busy before and idle after
    --stage <stage>      mark that stage running before and done/failed after
    --task "<text>"      what the board says they are doing (default: the step name)
    --lead "<text>"      Rio's status line, posted as the step starts (saves a separate studio call)
Studio calls are best effort: a missing script, a crash or a hang (5 s timeout)
never changes the step's exit code or output. EDITOR_STUDIO_SCRIPT overrides the
script path (tests); STUDIO_OFF=1 is honoured by studio.py itself.

Command allowlist (security): the headless run may call this script freely, so it
is NOT a generic "run anything" wrapper. The command after `--` must be one of:
    <project>/.venv/bin/python <script.py under <CODE>/helpers/, vendor/video-use/helpers/
                                or .claude/skills/<this system's skill>/scripts/> [args]
        (CODE = rio1/ with skill tutorial-edit, or rio2/ with tutorial-edit-2; see helpers/paths.py)
        (`py` is accepted as shorthand for <project>/.venv/bin/python; SKILL.md uses it)
    ffmpeg [args] | ffprobe [args]
    true
    caffeinate [-dimsu] <an allowed command>
    caffeinate [-dimsu] claude <exactly edit.sh's flags> <prompt>   (edit.sh only)
The claude launch must be exactly
    caffeinate -i claude -p --model sonnet --permission-mode dontAsk --setting-sources project
        --strict-mcp-config --settings <CODE>/config/run-settings.json
        --output-format stream-json --verbose <prompt>
where --settings is that absolute, symlink-free path, the file's sha256 equals
RUN_SETTINGS_SHA256 below, the prompt does not start with "-", and CLAUDECODE is unset
(i.e. not launched from inside a Claude session). `step.py --print-run-settings-hash`
prints the expected hash and exits 1 if the file has drifted from it.
Arguments of python/ffmpeg/ffprobe may not be URLs or protocol paths, may not name a
.env file, may not point outside the project, and may not point into protected
project folders (input/, history/, rio1/, rio2/, vendor/, .claude/, .venv/, .git/, ...).
Anything else exits 126 without running. No env var or flag turns this off.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

TIME_BIN = "/usr/bin/time"
RSS_RE = re.compile(r"^\s*(\d+)\s+maximum resident set size", re.M)
CODE = Path(__file__).resolve().parent.parent  # this system's code root (rio1/ or rio2/)
if str(CODE) not in sys.path:
    sys.path.insert(0, str(CODE))
from helpers.paths import PROJECT_ROOT, SKILL_DIR, SYSTEM  # noqa: E402

PROJECT = PROJECT_ROOT  # shared data root: .venv/, vendor/, .claude/, runs/, ...
DEFAULT_STUDIO = PROJECT / ".claude" / "skills" / "studio" / "scripts" / "studio.py"
STUDIO_TIMEOUT_S = 5

# ---------- command allowlist ----------
REJECT_EXIT = 126
PROJECT_REAL = Path(os.path.realpath(PROJECT))
VENV_BIN_REAL = os.path.realpath(PROJECT / ".venv" / "bin")
SCRIPT_DIRS = tuple(Path(os.path.realpath(d)) for d in
                    (CODE / "helpers", PROJECT / "vendor" / "video-use" / "helpers", SKILL_DIR / "scripts"))
TRUSTED_BIN_DIRS = ("/usr/bin", "/bin", "/usr/sbin", "/sbin", "/opt/homebrew/bin", "/usr/local/bin")
SIMPLE_PROGRAMS = {"ffmpeg", "ffprobe", "true", "caffeinate"}
PY_ALIAS = "py"  # first word after `--` only; expanded to the venv python before the check
PYTHON_NAME_RE = re.compile(r"^python(3(\.\d+)?)?$")
CAFFEINATE_FLAG_RE = re.compile(r"^-[dimsu]+$")
# The run-only permission lockdown handed to the headless run, pinned by content hash.
# Each system has its own (rio1/config/, rio2/config/), so the pin is per system; this
# file is identical in both copies. After an intentional edit of a run-settings.json,
# update its entry (`step.py --print-run-settings-hash` shows the actual hash on drift).
RUN_SETTINGS = Path(os.path.realpath(CODE)) / "config" / "run-settings.json"
RUN_SETTINGS_SHA256S = {
    "rio1": "2415d5fda8c441d1c5c63d9db01b03d544b8a7c3e19895cedea71c75d267ff07",
    "rio2": "fd7f0187e70520dc0086bf4c28ae6098b4be45dec0067dc58f6aef17807f1ad8",
}
RUN_SETTINGS_SHA256 = RUN_SETTINGS_SHA256S.get(SYSTEM, "")
# The exact claude invocation edit.sh builds (only the trailing prompt is free).
CLAUDE_FLAGS = ["-p", "--model", "sonnet", "--permission-mode", "dontAsk",
                "--setting-sources", "project", "--strict-mcp-config",
                "--settings", str(RUN_SETTINGS),
                "--output-format", "stream-json", "--verbose"]
# Project paths a step may never name (read or write): sources, history, code, config, git.
PROTECTED = {"input", "history", "helpers", "vendor", ".claude", ".venv", ".git", "tests",
             "output", "docs", "edit.sh", "edit2.sh", "CLAUDE.md", "brief.md", ".gitignore"}
# The systems' code folders: inside them only profiles/ may be named (detect_screen_activity
# --profile); code, tests, docs, config (the pinned run lockdown) and edit.sh stay protected.
SYSTEM_DIRS = {"rio1", "rio2"}
SYSTEM_OPEN = {"profiles"}
# ffmpeg options that read extra files/scripts or switch off its path safety.
FF_BAD_OPTS = re.compile(r"^-(safe|protocol_whitelist|protocol_blacklist|attach|dump_attachment|"
                         r"\w*_script|filter_script|filter_complex_script|i_qmeta)(:|$)")
URL_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.,-]*:\S")
ENV_FILE_RE = re.compile(r"(^|[/\\:=,|;'\"])\.env")
MOVIE_FILTER_RE = re.compile(r"(^|[,;\[\]\s])a?movie=")
SPLIT_RE = re.compile(r"[=,|;:'\"]")


class Rejected(Exception):
    pass


def _inside(p: Path, root: Path) -> bool:
    return p == root or root in p.parents


def check_args(args: list[str], ff: bool = False) -> None:
    """Reject URLs, .env files, paths outside the project and protected project paths."""
    for a in args:
        flat = a.replace("\\", "")
        if "://" in flat or URL_SCHEME_RE.match(flat):
            raise Rejected(f"URL/protocol argument not allowed: {a[:60]!r}")
        if ENV_FILE_RE.search(flat):
            raise Rejected("arguments may not name a .env file")
        if ff and FF_BAD_OPTS.match(a):
            raise Rejected(f"ffmpeg option not allowed: {a}")
        if ff and MOVIE_FILTER_RE.search(flat):
            raise Rejected("movie/amovie filters (they open extra inputs) are not allowed")
        for c in {a, *SPLIT_RE.split(a)}:
            if not c or not ("/" in c or c.startswith(".") or c in PROTECTED):
                continue
            rp = Path(os.path.realpath(c))
            explicit = c.startswith("/") or ".." in Path(c).parts
            if not _inside(rp, PROJECT_REAL):
                if explicit and str(rp) != "/dev/null":
                    raise Rejected(f"path outside the project: {c[:80]!r}")
                continue
            rel = rp.relative_to(PROJECT_REAL).parts
            if rel and (rel[0] in PROTECTED or
                        (rel[0] in SYSTEM_DIRS and (len(rel) < 2 or rel[1] not in SYSTEM_OPEN))):
                raise Rejected(f"protected project path: {'/'.join(rel)[:80]!r}")


def run_settings_sha256() -> str | None:
    try:
        return hashlib.sha256(RUN_SETTINGS.read_bytes()).hexdigest()
    except OSError:
        return None


def check_claude(argv: list[str]) -> None:
    """argv = ["claude", *flags, prompt]: exactly edit.sh's launch, nothing else."""
    if os.environ.get("CLAUDECODE"):
        raise Rejected("claude may only be launched by edit.sh, not from inside a Claude session")
    if len(argv) != len(CLAUDE_FLAGS) + 2 or argv[1:-1] != CLAUDE_FLAGS:
        raise Rejected("claude must be called with exactly edit.sh's flags "
                       f"(including --settings {RUN_SETTINGS})")
    prompt = argv[-1]
    if not prompt.strip() or prompt.startswith("-"):
        raise Rejected("the claude prompt must be non-empty and may not start with '-'")
    actual = run_settings_sha256()
    if actual != RUN_SETTINGS_SHA256:
        raise Rejected(f"{RUN_SETTINGS} is missing or its sha256 ({actual}) does not match "
                       "RUN_SETTINGS_SHA256 in step.py")


def program(prog: str) -> str | None:
    """Map argv[0] to an allowed program name, or None."""
    if "/" in prog:
        absprog = os.path.abspath(prog)
        if os.path.realpath(os.path.dirname(absprog)) == VENV_BIN_REAL and PYTHON_NAME_RE.match(os.path.basename(absprog)):
            return "python"
        if os.path.dirname(absprog) in TRUSTED_BIN_DIRS and os.path.basename(absprog) in SIMPLE_PROGRAMS:
            return os.path.basename(absprog)
        return None
    return prog if prog in SIMPLE_PROGRAMS else None


def expand_py_alias(cmd: list[str]) -> list[str]:
    """`py <script>` -> the project venv python. Claude Code's permission check denies a Bash
    command that names `.venv/bin/python` a second time (after `--`), so SKILL.md uses `py`."""
    if cmd[:1] == [PY_ALIAS]:
        return [str(PROJECT / ".venv" / "bin" / "python"), *cmd[1:]]
    return cmd


def check_command(cmd: list[str]) -> None:
    """Raise Rejected unless cmd is on the step allowlist (see the module docstring)."""
    if not cmd:
        raise Rejected("empty command")
    name = program(cmd[0])
    if name is None:
        raise Rejected(f"program not allowed: {cmd[0][:80]!r}")
    if name == "true":
        if len(cmd) != 1:
            raise Rejected("'true' takes no arguments")
        return
    if name in ("ffmpeg", "ffprobe"):
        check_args(cmd[1:], ff=True)
        return
    if name == "python":
        if len(cmd) < 2 or cmd[1].startswith("-"):
            raise Rejected("python must run a script file (no -c, -m or interpreter options)")
        script = Path(os.path.realpath(cmd[1]))
        if script.suffix != ".py" or not script.is_file() or not any(_inside(script, d) for d in SCRIPT_DIRS):
            raise Rejected(f"python script not in an allowed folder: {cmd[1][:80]!r}")
        check_args(cmd[2:])
        return
    # caffeinate [-dimsu] <allowed command | edit.sh's claude call>
    i = 1
    while i < len(cmd) and CAFFEINATE_FLAG_RE.match(cmd[i]):
        i += 1
    rest = cmd[i:]
    if not rest:
        raise Rejected("caffeinate must wrap a command")
    if rest[0] == "claude":
        check_claude(rest)
        return
    if program(rest[0]) == "caffeinate":
        raise Rejected("nested caffeinate not allowed")
    check_command(rest)


def parse_max_rss_mb(report: str) -> float | None:
    """macOS `time -l` reports maximum resident set size in bytes."""
    m = RSS_RE.search(report)
    return round(int(m.group(1)) / (1024 * 1024), 1) if m else None


def studio(*args: str) -> None:
    """Best-effort call to the Studio CLI. Never raises, never prints to our stdout."""
    try:
        script = Path(os.environ.get("EDITOR_STUDIO_SCRIPT") or DEFAULT_STUDIO)
        if not script.is_file():
            return
        subprocess.run([sys.executable, str(script), *args], stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=STUDIO_TIMEOUT_S, check=False)
    except Exception:  # noqa: BLE001 - the board must never break a step
        pass


def studio_start(args) -> None:
    if args.lead:
        studio("lead", args.lead)
    if args.stage:
        studio("stage", args.stage, "running")
    if args.studio:
        studio("busy", args.studio, args.task or args.name)


def studio_finish(args, exit_code: int, wall_s: float) -> None:
    ok = exit_code == 0
    if args.studio:
        studio("idle", args.studio, f"{args.name} ok ({wall_s:.0f}s)" if ok else f"{args.name} failed (exit {exit_code})")
    if args.stage:
        studio("stage", args.stage, "done" if ok else "failed")


def append_row(edit_dir: Path, entry: dict) -> None:
    with open(edit_dir / "timings.jsonl", "a") as f:
        f.write(json.dumps(entry) + "\n")


def marker(edit_dir: Path, name: str) -> Path:
    return edit_dir / f".step_{re.sub(r'[^A-Za-z0-9_.-]', '_', name)}.begin"


def main(argv: list[str] | None = None) -> int:
    if (sys.argv[1:] if argv is None else argv) == ["--print-run-settings-hash"]:
        print(RUN_SETTINGS_SHA256)
        actual = run_settings_sha256()
        if actual != RUN_SETTINGS_SHA256:
            print(f"step.py: DRIFT: {RUN_SETTINGS} sha256 is {actual}", file=sys.stderr)
            return 1
        return 0
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--edit-dir", required=True, type=Path)
    ap.add_argument("--name", required=True)
    ap.add_argument("--studio", metavar="CAST_ID", default=None)
    ap.add_argument("--stage", default=None)
    ap.add_argument("--task", default=None)
    ap.add_argument("--lead", default=None, help="Rio's status line, posted as the step starts")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--begin", action="store_true", help="start timing a non-command step")
    mode.add_argument("--end", action="store_true", help="finish a --begin step and log it")
    ap.add_argument("--exit-code", type=int, default=0, help="with --end: the outcome to record")
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    args = ap.parse_args(argv)

    if not args.edit_dir.is_dir():
        print(f"step.py: edit dir not found: {args.edit_dir}", file=sys.stderr)
        return 2

    if args.begin:
        started_at = datetime.now().astimezone().isoformat(timespec="seconds")
        marker(args.edit_dir, args.name).write_text(json.dumps({"t0": time.time(), "started_at": started_at}))
        studio_start(args)
        return 0
    if args.end:
        m = marker(args.edit_dir, args.name)
        try:
            info = json.loads(m.read_text())
        except (OSError, ValueError):
            print(f"step.py: no --begin marker for step '{args.name}'", file=sys.stderr)
            return 2
        m.unlink(missing_ok=True)
        wall_s = round(time.time() - float(info["t0"]), 3)
        append_row(args.edit_dir, {"name": args.name, "started_at": info.get("started_at"), "wall_s": wall_s,
                                   "max_rss_mb": None, "exit_code": args.exit_code})
        studio_finish(args, args.exit_code, wall_s)
        return 0

    cmd = args.cmd[1:] if args.cmd[:1] == ["--"] else args.cmd
    if not cmd:
        print("step.py: no command given after --", file=sys.stderr)
        return 2
    cmd = expand_py_alias(cmd)
    try:
        check_command(cmd)
    except Rejected as ex:
        print(f"step.py: REFUSED (exit {REJECT_EXIT}): {ex}. Only the project venv python on "
              f"{SYSTEM}/helpers/, vendor/video-use/helpers/ or .claude/skills/{SKILL_DIR.name}/scripts/, ffmpeg, ffprobe "
              "true, and edit.sh's exact claude launch may run through step.py (see its docstring).", file=sys.stderr)
        return REJECT_EXIT

    studio_start(args)
    fd, report_path = tempfile.mkstemp(prefix="step_time_", suffix=".txt")
    os.close(fd)
    started_at = datetime.now().astimezone().isoformat(timespec="seconds")
    t0 = time.monotonic()
    try:
        proc = subprocess.run([TIME_BIN, "-l", "-o", report_path, *cmd])
        exit_code = proc.returncode
    except KeyboardInterrupt:
        exit_code = 130
    wall_s = round(time.monotonic() - t0, 3)
    try:
        report = Path(report_path).read_text(errors="replace")
    finally:
        Path(report_path).unlink(missing_ok=True)

    # A negative code means the step was killed by a signal; record it shell-style.
    if exit_code < 0:
        exit_code = 128 - exit_code

    append_row(args.edit_dir, {
        "name": args.name,
        "started_at": started_at,
        "wall_s": wall_s,
        "max_rss_mb": parse_max_rss_mb(report),
        "exit_code": exit_code,
    })
    studio_finish(args, exit_code, wall_s)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
