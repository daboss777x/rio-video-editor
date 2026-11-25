#!/usr/bin/env python3
"""Intake for edit.sh: order the clips, handle the plan doc, write sources.json, use the transcript cache.

Run by edit.sh (not by the headless Claude run). Commands:
  scan  INPUT_DIR [--json OUT]      validate input/, print the order; exit 1 + one-line reason on refusal
  paths INPUT_DIR                   the ordered clip paths, NUL-separated (for estimate.py --source)
  runpaths RUN_DIR                  same, for the clips already moved into a run dir
  get   SCAN_JSON KEY               print one field of a scan json (label, out_stem, slug_src, order_line, n)
  prepare INPUT_DIR RUN_DIR [--cache DIR]
                                    convert the plan, move everything into RUN_DIR, write
                                    RUN_DIR/edit/sources.json, copy cached transcripts to edit/transcripts/
  store RUN_DIR [--cache DIR]       after the run: save new edit/transcripts/<stem>.json into the cache
  key FILE                          the transcript cache key of a video

Rules are in docs/CONTRACTS.md, "Multi-clip runs and the video plan".
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi"}
PLAN_EXTS = {".docx", ".doc", ".rtf", ".txt", ".md"}
GENERIC_PLAN = {"plan", "script", "outline", "video plan", "notes", "brief", "video-plan"}
CHUNK = 64 * 1024 * 1024


class Refuse(Exception):
    pass


def natural_key(s: str):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s.lower())]


# The user names clips <video name><role>: "setterintro", "settermain1", "setter-outro".
# The role may also lead ("intro", "main2"). Keep in sync with helpers/chunk_edl.py ROLE_RE.
ROLE_RE = re.compile(r"^(?P<prefix>.*?)[ _.-]*(?P<role>intro|outro|main)(?P<n>\d*)$", re.I)


def clip_role(stem: str) -> tuple[str | None, str]:
    """(role, video-name prefix): role is intro|main|outro or None; prefix may be ""."""
    m = ROLE_RE.match(stem.strip())
    if m:
        return m.group("role").lower(), m.group("prefix").strip(" _.-")
    s = stem.lower()
    for role in ("intro", "outro", "main"):
        if s.startswith(role):
            return role, ""
    return None, ""


def clip_rank(stem: str) -> int:
    role = clip_role(stem)[0]
    return 0 if role == "intro" else 2 if role == "outro" else 1


def scan(input_dir: Path, output_dir: Path | None = None) -> dict:
    if not input_dir.is_dir():
        raise Refuse(f"input folder not found: {input_dir}")
    videos, docs, pdfs = [], [], []
    for p in sorted(input_dir.iterdir()):
        n = p.name
        if not p.is_file() or n.startswith(".") or n.startswith("~$"):
            continue
        ext = p.suffix.lower()
        if ext in VIDEO_EXTS:
            videos.append(p)
        elif ext in PLAN_EXTS:
            docs.append(p)
        elif ext == ".pdf":
            pdfs.append(p)
    if not videos:
        raise Refuse(f"no video in {input_dir}. Drop your clips (.mp4/.mov/.m4v/.mkv/.webm/.avi) "
                     "there, plus an optional plan doc, and rerun.")
    if pdfs:
        raise Refuse(f"{pdfs[0].name} is a PDF; I can't read PDFs. Save the plan as .docx, .txt or .md and rerun.")
    if len(docs) > 1:
        raise Refuse("found more than one plan doc (" + ", ".join(d.name for d in docs)
                     + "); leave only one in input/ and rerun.")
    seen: dict[str, str] = {}
    for v in videos:
        k = v.stem.lower()
        if k in seen:
            raise Refuse(f"two clips share the name '{v.stem}' ({seen[k]} and {v.name}); rename one and rerun.")
        seen[k] = v.name
    videos.sort(key=lambda p: (clip_rank(p.stem), natural_key(p.stem)))
    order = [v.stem for v in videos]
    plan = docs[0] if docs else None
    mains = [v.stem for v in videos if clip_rank(v.stem) == 1]
    prefixes = {clip_role(v.stem)[1] for v in videos}
    shared = prefixes.pop() if len(prefixes) == 1 and all(clip_role(v.stem)[0] for v in videos) else ""
    title = plan_title(plan) if plan is not None else ""
    if title and title.lower() not in GENERIC_PLAN:
        base = title  # the script's title names the video (user, 2026-11-17)
    elif len(videos) == 1:
        base = videos[0].stem
    elif shared:
        base = shared  # the user's short video name, e.g. "setter"
    elif plan is not None and plan.stem.lower() not in GENERIC_PLAN:
        base = plan.stem
    else:
        base = (mains or order)[0]
    if output_dir is not None:
        base = free_stem(base, output_dir)
    if len(videos) == 1:
        label = videos[0].name
    else:
        has_intro = clip_rank(order[0]) == 0
        label = f"intro + {len(videos) - 1} clips" if has_intro else f"{len(videos)} clips"
        if plan is not None:
            label += " + plan"
    return {"order": order, "files": {v.stem: v.name for v in videos}, "plan": plan.name if plan else None,
            "n": len(videos), "label": label, "out_stem": base,
            "slug_src": base, "order_line": " -> ".join(v.name for v in videos),
            "paths": [str(v) for v in videos]}


def free_stem(base: str, output_dir: Path) -> str:
    """`base`, or base-2, base-3, ... so a new run never replaces an older run's video in output/
    (<stem>_edited.mp4 or any <stem>_edited_vN.mp4)."""
    def taken(stem: str) -> bool:
        return (output_dir / f"{stem}_edited.mp4").exists() or any(output_dir.glob(f"{glob.escape(stem)}_edited_v*.mp4"))
    stem, k = base, 1
    while taken(stem):
        k += 1
        stem = f"{base}-{k}"
    return stem


def plan_title(plan: Path) -> str:
    """The plan's first non-empty line, made file-name safe (≤ 60 chars), or "" if unreadable.
    Multi-clip runs are named after it: "intro"/"main1" make poor video names."""
    try:
        if plan.suffix.lower() in (".md", ".txt"):
            text = plan.read_text(errors="replace")
        else:
            r = subprocess.run(["textutil", "-convert", "txt", "-stdout", str(plan)],
                               capture_output=True, text=True, timeout=30)
            text = r.stdout if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""
    for line in text.splitlines():
        line = re.sub(r"^[#\s*-]+", "", line).strip()
        if line:
            safe = re.sub(r"[^A-Za-z0-9 _-]+", "", line)
            return re.sub(r"\s+", "-", safe.strip())[:60].strip("-_")
    return ""


def cache_key(path: Path) -> str:
    size = path.stat().st_size
    h = hashlib.sha256(str(size).encode())
    with open(path, "rb") as f:
        h.update(f.read(CHUNK))
        if size > CHUNK:
            f.seek(size - CHUNK)
            h.update(f.read(CHUNK))
    return h.hexdigest()


def convert_plan(src: Path, dest: Path) -> None:
    if src.suffix.lower() in (".md", ".txt"):
        shutil.copyfile(src, dest)
        return
    r = subprocess.run(["textutil", "-convert", "txt", "-output", str(dest), str(src)],
                       capture_output=True, text=True)
    if r.returncode != 0 or not dest.exists():
        raise Refuse(f"could not read the plan doc {src.name}: {(r.stderr or 'textutil failed').strip()[:200]}")


def prepare(input_dir: Path, run_dir: Path, cache: Path | None) -> dict:
    s = scan(input_dir)
    e = run_dir / "edit"
    e.mkdir(parents=True, exist_ok=True)
    plan_name = None
    if s["plan"]:
        convert_plan(input_dir / s["plan"], e / "plan.md")  # before anything moves: a failure leaves input/ intact
        plan_name = "plan.md"
    for name in list(s["files"].values()) + ([s["plan"]] if s["plan"] else []):
        shutil.move(str(input_dir / name), str(run_dir / name))
    (e / "sources.json").write_text(json.dumps(
        {"order": s["order"], "files": s["files"], "plan": plan_name}, indent=1) + "\n")
    hits = 0
    if cache is not None:
        (e / "transcripts").mkdir(exist_ok=True)
        for stem, name in s["files"].items():
            c, d = cache / f"{cache_key(run_dir / name)}.json", e / "transcripts" / f"{stem}.json"
            if c.is_file() and not d.exists():
                shutil.copyfile(c, d)
                hits += 1
    s["cache_hits"] = hits
    return s


def store(run_dir: Path, cache: Path) -> int:
    e = run_dir / "edit"
    try:
        files = json.loads((e / "sources.json").read_text())["files"]
    except (OSError, ValueError, KeyError):
        vids = [p for p in run_dir.iterdir() if p.suffix.lower() in VIDEO_EXTS]
        files = {p.stem: p.name for p in vids} if len(vids) == 1 else {}
    n = 0
    for stem, name in files.items():
        t, v = e / "transcripts" / f"{stem}.json", run_dir / name
        if not (t.is_file() and v.is_file()):
            continue
        try:
            if not isinstance(json.loads(t.read_text()), dict):
                continue
            dest = cache / f"{cache_key(v)}.json"
            if not dest.exists():
                cache.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(t, dest)
                n += 1
        except (OSError, ValueError):
            continue
    return n


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("scan"); p.add_argument("input", type=Path); p.add_argument("--json", type=Path)
    p.add_argument("--output-dir", type=Path, help="pick an output name not already used there")
    p = sub.add_parser("paths"); p.add_argument("input", type=Path)
    p = sub.add_parser("runpaths"); p.add_argument("run", type=Path)
    p = sub.add_parser("get"); p.add_argument("file", type=Path); p.add_argument("key")
    p = sub.add_parser("prepare"); p.add_argument("input", type=Path); p.add_argument("run", type=Path)
    p.add_argument("--cache", type=Path)
    p = sub.add_parser("store"); p.add_argument("run", type=Path); p.add_argument("--cache", type=Path, required=True)
    p = sub.add_parser("key"); p.add_argument("file", type=Path)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "scan":
            s = scan(a.input, a.output_dir)
            if a.json:
                a.json.write_text(json.dumps(s))
            print("order: " + s["order_line"] + (f"  | plan: {s['plan']}" if s["plan"] else "  | no plan doc"))
        elif a.cmd == "paths":
            sys.stdout.write("".join(x + "\0" for x in scan(a.input)["paths"]))
        elif a.cmd == "runpaths":
            src = json.loads((a.run / "edit" / "sources.json").read_text())
            sys.stdout.write("".join(str(a.run / src["files"][k]) + "\0" for k in src["order"]))
        elif a.cmd == "get":
            print(json.loads(a.file.read_text())[a.key])
        elif a.cmd == "prepare":
            s = prepare(a.input, a.run, a.cache)
            print(f"intake: {s['n']} clip(s), plan {s['plan'] or 'none'}, transcript cache hits {s['cache_hits']}")
        elif a.cmd == "store":
            print(f"transcript cache: stored {store(a.run, a.cache)} new transcript(s)")
        elif a.cmd == "key":
            print(cache_key(a.file))
    except Refuse as ex:
        print(f"edit.sh: {ex}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
