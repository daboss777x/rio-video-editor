"""macOS Vision (vision.swift, built into the OS: no installs) for blur_find.py's text reading.

Faster scan (2026-11-21): vision.swift is compiled once with swiftc into cache/vision/ (keyed by its
own hash; ~190 MB per reader instead of ~390 MB for the `swift` interpreter, and no compile on every
start), and the images are read by `jobs` readers at once (default VISION_JOBS = 3: ~2.2x faster on
the M3, ~0.6 GB in all). Falls back to `swift vision.swift` when swiftc is missing or fails.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

SWIFT = Path(__file__).resolve().parent / "vision.swift"
CACHE = Path(__file__).resolve().parents[3] / "cache" / "vision"
VISION_JOBS = 3


def _reader() -> list[str]:
    """The command that runs vision.swift: the cached compiled binary, built on first use."""
    tag = hashlib.sha256(SWIFT.read_bytes()).hexdigest()[:12]
    exe = CACHE / f"vision-{tag}"
    if not exe.exists():
        try:
            CACHE.mkdir(parents=True, exist_ok=True)
            tmp = CACHE / f".vision-{tag}.{os.getpid()}"
            proc = subprocess.run(["swiftc", "-O", str(SWIFT), "-o", str(tmp)], capture_output=True, text=True)
            if proc.returncode != 0:
                return ["swift", str(SWIFT)]
            os.replace(tmp, exe)
        except OSError:
            return ["swift", str(SWIFT)]
    return [str(exe)]


def run_vision(mode: str, images: list[Path], work: Path, jobs: int | None = None) -> dict[str, dict]:
    """{image path: result} from vision.swift, read by `jobs` readers at once (each its own share)."""
    if not images:
        return {}
    jobs = max(1, min(jobs or VISION_JOBS, len(images)))
    cmd = _reader()
    procs = []
    for k in range(jobs):
        lst = work / f"{mode}_list_{k}.txt"
        lst.write_text("\n".join(str(p) for p in images[k::jobs]) + "\n")
        # each reader writes to its own files: a full pipe would stall it while another is being read
        so, se = open(work / f"{mode}_out_{k}.jsonl", "w"), open(work / f"{mode}_err_{k}.txt", "w")
        procs.append((subprocess.Popen([*cmd, mode, str(lst)], stdout=so, stderr=se), so, se, k))
    out: dict[str, dict] = {}
    errors = []
    for p, so, se, k in procs:
        p.wait()
        so.close()
        se.close()
        if p.returncode != 0:
            errors.append((work / f"{mode}_err_{k}.txt").read_text().strip()[-400:])
            continue
        for line in (work / f"{mode}_out_{k}.jsonl").read_text().splitlines():
            line = line.strip()
            if line.startswith("{"):
                d = json.loads(line)
                out[d["p"]] = d
    if errors:
        raise RuntimeError(f"vision.swift {mode} failed: {errors[0]}")
    return out
