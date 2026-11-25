#!/usr/bin/env python3
"""Find the run a revise is for (edit.sh --revise; docs/CONTRACTS.md "Revise loop").

    revise_target.py TARGET --runs-dir R

TARGET is any of:
  - a run dir (absolute, or relative to the cwd): `runs/2026-10-29_1918_setter`, or its `edit/`
  - a run name inside R: `2026-10-29_1918_setter`
  - an output name: `setter_edited.mp4`, `setter_edited_v2.mp4`, `output/setter_REPORT.md`,
    `setter_REVISION_v3.md` or just the stem `setter` (the newest finished run of that video wins)

Prints three lines on success (exit 0):
    <absolute run dir>
    <output stem>         the base of output/<stem>_edited[_vN].mp4
    <latest version>      1 before any revision
Exits 1 with "edit.sh: <reason>" on stderr when nothing (finished) matches.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import intake  # noqa: E402  (same out_stem rule as the run that made the output)

RUN_NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{4}_(?P<slug>.+?)(?:-(?P<n>\d+))?$")
OUTPUT_SUFFIX_RE = re.compile(r"_(?:edited(?:_v\d+)?|REPORT(?:_v\d+)?|REVISION_v\d+)(?:_[^/]*)?$")


class Fail(Exception):
    pass


def slug(stem: str) -> str:
    """Same as edit.sh: lower case, non-alphanumerics to '-', trimmed, 40 chars."""
    s = re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")[:40]
    return s or "video"


def _json(p: Path) -> dict:
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}


def run_stem(run: Path) -> str | None:
    """The output stem of a run: rio.json (interactive runs), else recomputed from its clips the way
    intake.py scan did (the run dir holds the moved clips and the original plan doc)."""
    E = run / "edit"
    stem = _json(E / "rio.json").get("stem")
    if stem:
        return stem
    files = _json(E / "sources.json").get("files") or {}
    if len(files) == 1:
        return next(iter(files))
    vids = [run / n for n in files.values() if (run / n).is_file()]
    if not vids:
        return None
    docs = [p for p in run.iterdir() if p.is_file() and p.suffix.lower() in intake.PLAN_EXTS
            and p.name.lower() != "feedback.md" and not p.name.startswith((".", "~$"))]
    with tempfile.TemporaryDirectory() as td:
        for v in vids + (docs if len(docs) == 1 else []):
            (Path(td) / v.name).symlink_to(v.resolve())
        try:
            return intake.scan(Path(td))["out_stem"]
        except Exception:  # noqa: BLE001  (an odd run dir just doesn't match by stem)
            return None


def latest_version(E: Path) -> int:
    rev = E / "revisions"
    ns = [int(m.group(1)) for d in (rev.iterdir() if rev.is_dir() else [])
          if (m := re.fullmatch(r"v(\d+)", d.name)) and (d / "edl.json").is_file()]
    return max(ns + [1])


def _finished(run: Path) -> bool:
    return (run / "edit" / "edl.json").is_file()


def resolve(target: str, runs_dir: Path) -> tuple[Path, str]:
    t = target.strip()
    if not t:
        raise Fail("--revise needs a run dir, run name or output name")
    p = Path(t).expanduser()
    for c in (p, runs_dir / t):
        if c.is_dir():
            run = c.parent if c.name == "edit" and (c / "edl.json").exists() else c
            if not _finished(run):
                raise Fail(f"{run} has no edit/edl.json (that run never finished an edit; nothing to revise)")
            run = run.resolve()
            m = RUN_NAME_RE.match(run.name)
            return run, run_stem(run) or (m.group("slug") if m else run.name)
    # An output name (or a bare stem): newest finished run of that video.
    name = p.name
    for ext in (".mp4", ".md", ".mov"):
        if name.lower().endswith(ext):
            name = name[: -len(ext)]
    stem = OUTPUT_SUFFIX_RE.sub("", name) or name
    runs = sorted(d for d in (runs_dir.iterdir() if runs_dir.is_dir() else [])
                  if d.is_dir() and not d.name.startswith(("_", ".")) and _finished(d))
    hits = [d for d in runs if run_stem(d) == stem]
    if not hits:  # stem unknown (odd run dir): fall back to the run name's slug
        hits = [d for d in runs if (m := RUN_NAME_RE.match(d.name)) and m.group("slug") == slug(stem)]
    if not hits:
        raise Fail(f"no finished run found for '{target}' in {runs_dir} (give the run dir, e.g. runs/<id>)")
    if len(hits) > 1:
        print(f"edit.sh: {len(hits)} runs match '{stem}'; using the newest, {hits[-1].name}", file=sys.stderr)
    return hits[-1].resolve(), stem


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("target")
    ap.add_argument("--runs-dir", required=True, type=Path)
    a = ap.parse_args(argv)
    try:
        run, stem = resolve(a.target, a.runs_dir)
    except Fail as e:
        print(f"edit.sh: {e}", file=sys.stderr)
        return 1
    print(run)
    print(stem)
    print(latest_version(run / "edit"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
