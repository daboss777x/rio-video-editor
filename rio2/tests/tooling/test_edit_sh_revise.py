"""edit.sh --revise: argument parsing, run resolution and the not-applied path.
No Claude call, no render, no transcription (docs/CONTRACTS.md "Revise loop")."""
import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
EDIT = ROOT / "edit.sh"
WRAP = "./edit2.sh" if ROOT.name == "rio2" else "./edit.sh"  # the root wrapper edit.sh names in its usage


@pytest.fixture
def dirs(tmp_path):
    d = {k: tmp_path / k for k in ("input", "runs", "output")}
    for p in d.values():
        p.mkdir()
    notes = tmp_path / "notes.txt"
    notes.write_text("1:23 - the word 'trigger' is cut off\n")
    d["notes"] = notes
    return d


def run(dirs, *args, extra_env=None, timeout=60):
    env = dict(os.environ, EDITOR_INPUT_DIR=str(dirs["input"]), EDITOR_RUNS_DIR=str(dirs["runs"]),
               EDITOR_OUTPUT_DIR=str(dirs["output"]), EDITOR_NO_RECORD="1", EDITOR_SKIP_PREFLIGHT="1",
               EDITOR_STUDIO_SCRIPT=str(dirs["input"].parent / "no-studio.py"))
    env.update(extra_env or {})
    return subprocess.run(["bash", str(EDIT), *map(str, args)], capture_output=True, text=True, env=env,
                          timeout=timeout)


def make_run(dirs, name, files, finished=True, rio_stem=None, plan=None):
    r = dirs["runs"] / name
    (r / "edit").mkdir(parents=True)
    for f in files.values():
        (r / f).write_bytes(b"x")
    if plan:
        (r / plan).write_text("plan")
    (r / "edit" / "sources.json").write_text(json.dumps({"order": list(files), "files": files, "plan": None}))
    if finished:
        (r / "edit" / "edl.json").write_text(json.dumps({"ranges": []}))
    if rio_stem:
        (r / "edit" / "rio.json").write_text(json.dumps({"stem": rio_stem}))
    return r


def resolve(dirs, target):
    p = run(dirs, "--revise", target, dirs["notes"], extra_env={"EDITOR_REVISE_RESOLVE_ONLY": "1"})
    assert p.returncode == 0, p.stdout + p.stderr
    return dict(l.split("=", 1) for l in p.stdout.splitlines() if "=" in l)


SETTER = {"setterintro": "setterintro.mp4", "settermain1": "settermain1.mp4"}


def test_usage_errors(dirs):
    p = run(dirs, "--revise", "x")
    assert p.returncode == 1 and f"usage: {WRAP} --revise <run dir | run name | output name> <notes file>" in p.stderr
    p = run(dirs, "--revise-prepare", "a", "b", "c")
    assert p.returncode == 1 and "usage" in p.stderr
    p = run(dirs, "--revise-finish", "a")
    assert p.returncode == 1 and f"usage: {WRAP} --revise-finish" in p.stderr
    p = run(dirs, "--revise", "x", dirs["notes"].parent / "missing.txt")
    assert p.returncode == 1 and "notes file not found or empty" in p.stderr
    empty = dirs["notes"].parent / "empty.txt"
    empty.write_text("")
    p = run(dirs, "--revise", "x", empty)
    assert p.returncode == 1 and "notes file not found or empty" in p.stderr


def test_help_mentions_revise(dirs):
    p = run(dirs, "--help")
    assert p.returncode == 0 and "--revise TARGET NOTES" in p.stdout and "set -euo" not in p.stdout


def test_resolves_run_dir_run_name_and_edit_dir(dirs):
    r = make_run(dirs, "2026-10-29_1405_setter", SETTER)
    for target in (r, r / "edit", r.name, f"{r}/"):
        got = resolve(dirs, target)
        assert got == {"run": str(r.resolve()), "stem": "setter", "latest": "v1", "next": "v2"}, target


def test_output_names_pick_the_newest_finished_run(dirs):
    make_run(dirs, "2026-10-28_2300_setter", SETTER)
    new = make_run(dirs, "2026-10-29_1405_setter", SETTER)
    make_run(dirs, "2026-10-30_0100_setter", SETTER, finished=False)   # never finished: skipped
    make_run(dirs, "_aborted_2026-10-31_0100_setter", SETTER)           # aborted: skipped
    make_run(dirs, "2026-10-29_1500_other", {"other": "other.mp4"})
    for target in ("setter", "setter_edited.mp4", "output/setter_edited_v2.mp4", "setter_REPORT.md",
                   "setter_REVISION_v3.md", str(dirs["output"] / "setter_edited.mp4")):
        got = resolve(dirs, target)
        assert got["run"] == str(new.resolve()) and got["stem"] == "setter", target
    assert resolve(dirs, "other_edited.mp4")["stem"] == "other"


def test_single_clip_stem_with_spaces_and_rio_stem(dirs):
    r = make_run(dirs, "2026-10-29_0900_my-test-clip", {"My Test Clip": "My Test Clip.mp4"})
    got = resolve(dirs, "My Test Clip_edited.mp4")
    assert got["run"] == str(r.resolve()) and got["stem"] == "My Test Clip"
    r2 = make_run(dirs, "2026-10-29_1000_whatever", {"a": "a.mp4", "b": "b.mp4"}, rio_stem="Launch Video")
    assert resolve(dirs, r2)["stem"] == "Launch Video"
    assert resolve(dirs, "Launch Video_edited_v4.mp4")["run"] == str(r2.resolve())


def test_multi_clip_stem_uses_the_plan_doc_but_not_feedback(dirs):
    r = make_run(dirs, "2026-10-29_1100_seo-basics", {"intro": "intro.mp4", "main1": "main1.mp4"},
                 plan="SEO Basics.docx")
    (r / "feedback.md").write_text("x")
    assert resolve(dirs, r)["stem"] == "SEO Basics"
    assert resolve(dirs, "SEO Basics_edited.mp4")["run"] == str(r.resolve())


def test_latest_version_counts_applied_revisions_only(dirs):
    r = make_run(dirs, "2026-10-29_1405_setter", SETTER)
    for n, applied in ((1, True), (2, True), (3, False)):
        v = r / "edit" / "revisions" / f"v{n}"
        v.mkdir(parents=True)
        if applied:
            (v / "edl.json").write_text("{}")
    got = resolve(dirs, r)
    assert got["latest"] == "v2" and got["next"] == "v3"


def test_unknown_and_unfinished_targets_fail_with_a_reason(dirs):
    p = run(dirs, "--revise", "nothing_edited.mp4", dirs["notes"])
    assert p.returncode == 1 and "no finished run found for 'nothing_edited.mp4'" in p.stderr
    r = make_run(dirs, "2026-10-29_1405_setter", SETTER, finished=False)
    p = run(dirs, "--revise", r, dirs["notes"])
    assert p.returncode == 1 and "never finished an edit" in p.stderr
    assert not (r / "edit" / "revisions").exists()   # refused before anything was written


def test_revise_finish_refuses_a_revision_not_prepared(dirs):
    r = make_run(dirs, "2026-10-29_1405_setter", SETTER)
    p = run(dirs, "--revise-finish", r, "2")
    assert p.returncode == 1 and "rev.json missing" in p.stderr
    p = run(dirs, "--revise-finish", r, "two")
    assert p.returncode == 1 and "version must be a number" in p.stderr


def test_revise_finish_without_an_applied_edl_reports_not_applied(dirs):
    """Rosa wrote nothing: the final apply fails, edl.json stays as it was, and the user gets a
    REVISION report saying nothing changed (and why). No render is attempted."""
    r = make_run(dirs, "2026-10-29_1405_setter", SETTER)
    edl_before = (r / "edit" / "edl.json").read_text()
    v = r / "edit" / "revisions" / "v2"
    v.mkdir(parents=True)
    (v / "rev.json").write_text(json.dumps({"stem": "setter", "n": 2, "of": 1, "t_start": 0, "run": str(r)}))
    (v / "notes.txt").write_text("# my notes\n1:23 - the word 'trigger' is cut off\n")
    p = run(dirs, "--revise-finish", r, "2", timeout=120)
    out = p.stdout + p.stderr
    assert p.returncode == 1, out
    assert "revision v2 not applied" in out
    md = (dirs["output"] / "setter_REVISION_v2.md").read_text()
    assert md.startswith("# Revision v2 (from v1): not applied")
    assert "- 1:23 - the word 'trigger' is cut off" in md and "# my notes" not in md
    assert (r / "edit" / "edl.json").read_text() == edl_before
    assert not (dirs["output"] / "setter_edited_v2.mp4").exists()
    assert (v / "revise.log").exists() and (v / "apply_errors.txt").exists()
    assert not (v / "edl.json").exists()
