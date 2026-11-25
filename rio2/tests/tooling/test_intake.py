"""intake.py: clip ordering, plan doc conversion, sources.json, transcript cache."""
import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = ROOT.parent / ".claude" / "skills" / ("tutorial-edit-2" if ROOT.name == "rio2" else "tutorial-edit")  # ROOT = rio1/ or rio2/
sys.path.insert(0, str(SKILL_DIR / "scripts"))
import intake  # noqa: E402


def touch(d, *names, data=b"x"):
    for n in names:
        (d / n).write_bytes(data + n.encode())


def test_order_intro_natural_outro(tmp_path):
    touch(tmp_path, "Outro.mp4", "main10.mp4", "main2.mov", "extra.mp4", "Intro.mp4", "main1.mp4", ".DS_Store")
    s = intake.scan(tmp_path)
    assert s["order"] == ["Intro", "extra", "main1", "main2", "main10", "Outro"]
    assert s["label"] == "intro + 5 clips" and s["plan"] is None


def test_single_clip_keeps_name_label(tmp_path):
    touch(tmp_path, "My Clip.mp4")
    s = intake.scan(tmp_path)
    assert s["label"] == "My Clip.mp4" and s["out_stem"] == "My Clip"


def test_refusals(tmp_path):
    with pytest.raises(intake.Refuse, match="no video"):
        intake.scan(tmp_path)
    touch(tmp_path, "a.mp4", "plan.pdf")
    with pytest.raises(intake.Refuse, match="PDF"):
        intake.scan(tmp_path)
    (tmp_path / "plan.pdf").unlink()
    touch(tmp_path, "p.docx", "q.md")
    with pytest.raises(intake.Refuse, match="more than one plan"):
        intake.scan(tmp_path)
    (tmp_path / "p.docx").unlink(); (tmp_path / "q.md").unlink()
    touch(tmp_path, "A.mov")
    with pytest.raises(intake.Refuse, match="share the name"):
        intake.scan(tmp_path)


@pytest.mark.skipif(not shutil.which("textutil"), reason="macOS textutil")
def test_plan_docx_like_rtf_converts_and_sources_written(tmp_path):
    inp, run = tmp_path / "in", tmp_path / "run"
    inp.mkdir()
    touch(inp, "main1.mp4", "intro.mp4")
    (inp / "My Plan.rtf").write_text(r"{\rtf1\ansi My Video Title\par Step one: open the app.\par}")
    s = intake.prepare(inp, run, None)
    e = run / "edit"
    assert "Step one: open the app." in (e / "plan.md").read_text()
    d = json.loads((e / "sources.json").read_text())
    assert d == {"order": ["intro", "main1"], "files": {"intro": "intro.mp4", "main1": "main1.mp4"},
                 "plan": "plan.md", "cams": {}}   # contract: "cams" is always present
    assert list(inp.iterdir()) == [] and (run / "My Plan.rtf").exists()
    assert s["out_stem"] == "My-Video-Title"   # the script's title names the video


def test_txt_plan_copied_and_no_plan_is_null(tmp_path):
    inp, run = tmp_path / "in", tmp_path / "run"
    inp.mkdir()
    touch(inp, "a.mp4")
    (inp / "notes.txt").write_text("hello")
    intake.prepare(inp, run, None)
    assert (run / "edit" / "plan.md").read_text() == "hello"
    inp2, run2 = tmp_path / "in2", tmp_path / "run2"
    inp2.mkdir(); touch(inp2, "a.mp4")
    intake.prepare(inp2, run2, None)
    assert json.loads((run2 / "edit" / "sources.json").read_text())["plan"] is None


def test_failed_plan_conversion_moves_nothing(tmp_path):
    inp, run = tmp_path / "in", tmp_path / "run"
    inp.mkdir()
    touch(inp, "a.mp4")
    (inp / "bad.docx").write_bytes(b"not a docx")
    if not shutil.which("textutil"):
        pytest.skip("macOS textutil")
    try:
        intake.prepare(inp, run, None)
    except intake.Refuse:
        assert (inp / "a.mp4").exists() and (inp / "bad.docx").exists()
    else:  # textutil happily read it as text: fine, nothing to assert
        pass


def test_cache_restore_then_store(tmp_path):
    inp, run, cache = tmp_path / "in", tmp_path / "run", tmp_path / "cache"
    inp.mkdir()
    touch(inp, "intro.mp4", "main1.mp4")
    k = intake.cache_key(inp / "intro.mp4")
    assert k != intake.cache_key(inp / "main1.mp4")
    cache.mkdir()
    (cache / f"{k}.json").write_text('{"words": [1]}')
    s = intake.prepare(inp, run, cache)
    assert s["cache_hits"] == 1
    e = run / "edit"
    assert (e / "transcripts" / "intro.json").read_text() == '{"words": [1]}'
    assert not (e / "transcripts" / "main1.json").exists()
    (e / "transcripts" / "main1.json").write_text('{"words": [2]}')   # "Scribe" made a new one
    assert intake.store(run, cache) == 1
    assert (cache / f"{intake.cache_key(run / 'main1.mp4')}.json").read_text() == '{"words": [2]}'
    assert intake.store(run, cache) == 0                                # already cached


def test_cache_key_uses_size_and_ends(tmp_path):
    a, b = tmp_path / "a.mp4", tmp_path / "b.mp4"
    a.write_bytes(b"abc"); b.write_bytes(b"abd")
    assert intake.cache_key(a) != intake.cache_key(b)


def test_suffix_roles_order_and_name(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("intake2", SKILL_DIR / "scripts" / "intake.py")
    ik = importlib.util.module_from_spec(spec); spec.loader.exec_module(ik)
    d = tmp_path / "in"; d.mkdir()
    for n in ("settermain10", "setteroutro", "settermain2", "setterintro"):
        (d / f"{n}.mp4").write_bytes(b"")
    (d / "AI Appointment Setter Script.docx").write_bytes(b"")
    s = ik.scan(d)
    assert s["order"] == ["setterintro", "settermain2", "settermain10", "setteroutro"]
    assert s["out_stem"] == "setter"
    assert ik.clip_role("intro") == ("intro", "") and ik.clip_role("main3")[0] == "main"


def test_output_named_after_script_title_even_for_one_clip(tmp_path):
    # user, 2026-11-17: "mainscreen1_edited.mp4" is a poor name; use the script's title.
    touch(tmp_path, "mainscreen1.mp4")
    (tmp_path / "Drive_Organizer_Agent_Script.md").write_text(
        "How to Build a Claude Agent to Organise Your Google Drive\nWorking title · IvyFlow\nHOOK:\n")
    s = intake.scan(tmp_path)
    assert s["out_stem"] == "How-to-Build-a-Claude-Agent-to-Organise-Your-Google-Drive"


def test_new_run_never_reuses_an_output_name_already_in_output(tmp_path):
    # 2026-11-17: two recordings both called mainscreen1 -> the newer run replaced the older videos.
    inp, out = tmp_path / "in", tmp_path / "output"
    inp.mkdir(); out.mkdir()
    touch(inp, "mainscreen1.mp4")
    assert intake.scan(inp, output_dir=out)["out_stem"] == "mainscreen1"      # nothing there yet
    (out / "mainscreen1_edited.mp4").write_bytes(b"old run")
    assert intake.scan(inp, output_dir=out)["out_stem"] == "mainscreen1-2"
    (out / "mainscreen1-2_edited_v2.mp4").write_bytes(b"older revision")     # a revision counts too
    assert intake.scan(inp, output_dir=out)["out_stem"] == "mainscreen1-3"


def test_generic_or_empty_script_title_falls_back_to_the_clip_name(tmp_path):
    touch(tmp_path, "mainscreen1.mp4")
    (tmp_path / "script.md").write_text("Script\n\nHOOK: ...\n")
    assert intake.scan(tmp_path)["out_stem"] == "mainscreen1"
    (tmp_path / "script.md").write_text("### !!! ###\n")
    assert intake.scan(tmp_path)["out_stem"] == "mainscreen1"
