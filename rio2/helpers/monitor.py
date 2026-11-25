#!/usr/bin/env python3
"""System RAM / swap sampler for a whole run.

Usage:
    .venv/bin/python rio2/helpers/monitor.py start   --edit-dir E [--interval 5]
    .venv/bin/python rio2/helpers/monitor.py stop    --edit-dir E
    .venv/bin/python rio2/helpers/monitor.py summary --edit-dir E

`start` launches a detached sampler (pidfile E/monitor.pid) that appends
{"t", "used_mb", "swap_used_mb"} to E/monitor.jsonl every interval seconds.
used_mb = (active + wired + compressor-occupied pages) x page size, from vm_stat.
swap_used_mb comes from `sysctl vm.swapusage`. `summary` writes
E/monitor_summary.json; the baseline is the first sample.
The sampler only forks two tiny commands per tick and holds no history in memory.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

PIDFILE = "monitor.pid"
SAMPLES = "monitor.jsonl"
SUMMARY = "monitor_summary.json"
DEFAULT_INTERVAL = float(os.environ.get("MONITOR_INTERVAL_S", "5"))


def parse_vm_stat(text: str) -> float:
    m = re.search(r"page size of (\d+) bytes", text)
    page = int(m.group(1)) if m else 16384

    def pages(label: str) -> int:
        mm = re.search(rf"^{re.escape(label)}:\s+(\d+)\.", text, re.M)
        return int(mm.group(1)) if mm else 0

    used = pages("Pages active") + pages("Pages wired down") + pages("Pages occupied by compressor")
    return round(used * page / (1024 * 1024), 1)


def parse_swapusage(text: str) -> float:
    m = re.search(r"used = ([\d.]+)([KMG])", text)
    if not m:
        return 0.0
    val, unit = float(m.group(1)), m.group(2)
    return round(val * {"K": 1 / 1024, "M": 1, "G": 1024}[unit], 1)


def sample() -> dict:
    vm = subprocess.run(["vm_stat"], capture_output=True, text=True).stdout
    sw = subprocess.run(["sysctl", "vm.swapusage"], capture_output=True, text=True).stdout
    return {"t": round(time.time(), 1), "used_mb": parse_vm_stat(vm), "swap_used_mb": parse_swapusage(sw)}


def _append(edit_dir: Path) -> None:
    with open(edit_dir / SAMPLES, "a") as f:
        f.write(json.dumps(sample()) + "\n")


def run_loop(edit_dir: Path, interval: float) -> int:
    stop = False

    def _term(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)
    while not stop:
        _append(edit_dir)
        deadline = time.monotonic() + interval
        while not stop and time.monotonic() < deadline:
            time.sleep(min(0.25, interval))
    _append(edit_dir)  # final sample at stop
    return 0


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def start(edit_dir: Path, interval: float) -> int:
    pidfile = edit_dir / PIDFILE
    if pidfile.exists():
        try:
            if _alive(int(pidfile.read_text().strip())):
                print(f"monitor.py: already running (pidfile {pidfile})", file=sys.stderr)
                return 1
        except ValueError:
            pass
    proc = subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "_run", "--edit-dir", str(edit_dir), "--interval", str(interval)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True, close_fds=True,
    )
    pidfile.write_text(str(proc.pid))
    # Wait briefly for the baseline sample so summary always has one.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not (edit_dir / SAMPLES).exists():
        time.sleep(0.05)
    print(f"monitor started (pid {proc.pid}, every {interval:g}s)")
    return 0


def stop(edit_dir: Path) -> int:
    pidfile = edit_dir / PIDFILE
    if not pidfile.exists():
        print("monitor.py: not running (no pidfile)", file=sys.stderr)
        return 1
    try:
        pid = int(pidfile.read_text().strip())
    except ValueError:
        pidfile.unlink(missing_ok=True)
        print("monitor.py: corrupt pidfile removed", file=sys.stderr)
        return 1
    if _alive(pid):
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 5
        while _alive(pid) and time.monotonic() < deadline:
            try:  # reap if it happens to be our child
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass
            time.sleep(0.05)
        if _alive(pid):
            os.kill(pid, signal.SIGKILL)
    pidfile.unlink(missing_ok=True)
    print("monitor stopped")
    return 0


def summarize(edit_dir: Path) -> int:
    path = edit_dir / SAMPLES
    rows = []
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    if not rows:
        print(f"monitor.py: no samples in {path}", file=sys.stderr)
        return 1
    base, base_swap = rows[0]["used_mb"], rows[0]["swap_used_mb"]
    peak = max(r["used_mb"] for r in rows)
    peak_swap = max(r["swap_used_mb"] for r in rows)
    out = {
        "baseline_used_mb": base,
        "peak_used_mb": peak,
        "peak_extra_mb": round(peak - base, 1),
        "peak_swap_mb": peak_swap,
        "swap_growth_mb": round(peak_swap - base_swap, 1),
        "samples": len(rows),
    }
    (edit_dir / SUMMARY).write_text(json.dumps(out, indent=2) + "\n")
    print(json.dumps(out))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="System RAM/swap sampler")
    ap.add_argument("action", choices=["start", "stop", "summary", "_run"])
    ap.add_argument("--edit-dir", required=True, type=Path)
    ap.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)
    a = ap.parse_args(argv)
    if not a.edit_dir.is_dir():
        print(f"monitor.py: edit dir not found: {a.edit_dir}", file=sys.stderr)
        return 2
    if a.action == "_run":
        return run_loop(a.edit_dir, a.interval)
    if a.action == "start":
        return start(a.edit_dir, a.interval)
    if a.action == "stop":
        return stop(a.edit_dir)
    return summarize(a.edit_dir)


if __name__ == "__main__":
    sys.exit(main())
