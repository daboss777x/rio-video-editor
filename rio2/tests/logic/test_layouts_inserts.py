"""Rio 2.0 layouts (split/face) and insert clips (testimonials): intake, split, merge, verify."""
import importlib.util
import json
from pathlib import Path

from helpers import chunk_edl as ce
from helpers import verify_edit as ve
from conftest import make_multi_edit

ROOT = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location(
    "intake2", ROOT / ".claude" / "skills" / "tutorial-edit-2" / "scripts" / "intake.py")
intake = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(intake)

MAIN = [(10.0 * i, ["word%d" % i, "alpha", "beta", "gamma"]) for i in range(1, 11)]
# testimonial: two sentences with a 2 s pause between them
TEST = [(1.0, ["I", "had", "a", "pleasant", "experience"]), (5.0, ["very", "purposeful"])]


def R(src, s, e, **kw):
    return {"source": src, "start": s, "end": e, "beat": "B", "quote": "q", "reason": "r", **kw}


def edit_with_insert(tmp_path):
    E, words = make_multi_edit(tmp_path, {"screenmain1": (MAIN, 120.0), "testimonial_hmj": (TEST, 60.0)})
    src = json.loads((E / "sources.json").read_text())
    src["order"] = ["screenmain1"]
    src["files"] = {"screenmain1": "screenmain1.mp4"}
    src["inserts"] = {"testimonial_hmj": "testimonial_hmj.mp4"}
    (E / "sources.json").write_text(json.dumps(src))
    return E, words


def write_edl(E, ranges, n=1):
    (E / "chunks").mkdir(exist_ok=True)
    (E / "chunks" / f"edl_{n:02d}.json").write_text(json.dumps(ranges))


# ------------------------------------------------------------ intake
def test_intake_classifies_inserts_before_cameras(tmp_path):
    for n in ("screenmain1.mp4", "maincam1.MOV", "testimonial_cam.mp4", "insert-broll.mp4"):
        (tmp_path / n).write_bytes(b"x")
    s = intake.scan(tmp_path)
    assert s["order"] == ["screenmain1"]
    assert list(s["cams"]) == ["maincam1"]
    assert set(s["inserts"]) == {"testimonial_cam", "insert-broll"}
    assert "2 inserts" in s["label"]


def test_intake_prepare_writes_inserts_only_when_present(tmp_path):
    inp, run = tmp_path / "in", tmp_path / "run"
    inp.mkdir()
    (inp / "screenmain1.mp4").write_bytes(b"x")
    (inp / "testimonial_hmj.mp4").write_bytes(b"y")
    intake.prepare(inp, run, None)
    src = json.loads((run / "edit" / "sources.json").read_text())
    assert src["inserts"] == {"testimonial_hmj": "testimonial_hmj.mp4"}
    assert (run / "testimonial_hmj.mp4").exists() and src["order"] == ["screenmain1"]


# ------------------------------------------------------------ split
def test_split_keeps_inserts_out_of_chunks_and_packs_them(tmp_path, profile):
    E, _ = edit_with_insert(tmp_path)
    (E / "slides").mkdir()
    (E / "slides" / "screenmain1.json").write_text(json.dumps(
        {"slides": [{"t": 15.0, "still": "slides/screenmain1/s_0001.jpg"}]}))
    (E / "cam_sync.json").write_text(json.dumps(
        {"ok": True, "cams": [{"cam": "c1", "screen": "screenmain1", "offset_s": 0, "covers": [0, 50]},
                              {"cam": "c2", "screen": "screenmain1", "offset_s": 0, "covers": [80, 120]}]}))
    metas = ce.split(E, profile)
    assert all(m["source"] == "screenmain1.mp4" for m in metas)
    text = "".join((E / "chunks" / f"chunk_{m['index']:02d}.md").read_text() for m in metas)
    assert "testimonial" not in text and "pleasant" not in text
    assert "SLIDE 15.00 still=slides/screenmain1/s_0001.jpg" in text
    assert "NO CAMERA 50.00-80.00" in text
    assert "pleasant experience" in (E / "inserts_packed.md").read_text()


def test_uncovered():
    assert ce.uncovered([[0, 50], [80, 120]], 0, 120) == [(50, 80)]
    assert ce.uncovered([[10, 20]], 0, 30) == [(0, 10), (20, 30)]
    assert ce.uncovered([[0, 100]], 0, 100) == []


# ------------------------------------------------------------ merge
def test_merge_places_insert_where_the_editor_put_it(tmp_path, profile):
    E, _ = edit_with_insert(tmp_path)
    write_edl(E, [R("screenmain1", 10.0, 11.6), R("testimonial_hmj", 0.9, 6.0),
                  R("screenmain1", 20.0, 21.6), R("screenmain1", 30.0, 31.6)])
    edl, viol, notes = ce.merge(E, profile)
    assert viol == []
    srcs = [r["source"] for r in edl["ranges"]]
    first_ins = srcs.index("testimonial_hmj")
    assert all(s == "screenmain1" for s in srcs[:first_ins]) and first_ins > 0
    ins = [r for r in edl["ranges"] if r["source"] == "testimonial_hmj"]
    assert all(r.get("insert") for r in ins)
    assert max(r["end"] for r in edl["ranges"][:first_ins]) < 15     # the main range before it
    after = edl["ranges"][first_ins + len(ins):]
    assert after and all(r["source"] == "screenmain1" and r["start"] >= 19 for r in after)
    assert len(ins) == 2      # the 2.3 s pause inside the testimonial is cut (light touch)
    assert edl["inserts"] == ["testimonial_hmj"]
    assert list(edl["sources"])[0] == "screenmain1"   # the render's output format = the screen


def test_merge_insert_splits_a_main_range_that_runs_past_its_anchor(tmp_path, profile):
    E, _ = edit_with_insert(tmp_path)
    # two touching main ranges would be joined into one; the insert must still sit between them
    write_edl(E, [R("screenmain1", 10.0, 11.6), R("testimonial_hmj", 0.9, 3.2),
                  R("screenmain1", 11.6, 11.7)])
    edl, viol, _ = ce.merge(E, profile)
    assert viol == []
    srcs = [r["source"] for r in edl["ranges"]]
    assert srcs.index("testimonial_hmj") > 0


def test_join_touching_never_moves_inserts_or_joins_layouts():
    rs = [{"source": "a", "start": 0, "end": 1}, {"source": "t", "start": 5, "end": 6, "insert": True},
          {"source": "a", "start": 3, "end": 4}, {"source": "a", "start": 4, "end": 5, "layout": "face"}]
    out = ce._join_touching(rs, {"a": 0, "t": 1})
    assert [r["source"] for r in out] == ["a", "t", "a", "a"]
    assert out[3]["layout"] == "face"


def test_smooth_layouts():
    rs = [{"source": "a", "start": 0, "end": 10, "layout": "face"},
          {"source": "a", "start": 10, "end": 11},                     # 1 s split blip
          {"source": "a", "start": 11, "end": 20, "layout": "face"},
          {"source": "a", "start": 20, "end": 30},
          {"source": "a", "start": 30, "end": 33, "layout": "face"}]  # 3 s face: too short
    notes = ce.smooth_layouts(rs, 6.0, 2.0)
    assert [ce._layout(r) for r in rs] == ["face", "face", "face", "split", "split"]
    assert len(notes) == 2


def test_validate_rejects_bad_layout():
    edl = {"sources": {"a": "a.mp4"}, "ranges": [{"source": "a", "start": 0, "end": 1, "layout": "pip"}]}
    assert any("layout" in v for v in ce.validate_edl(edl, {"a": 10}, None))


def test_long_silences_skip_inserts(tmp_path, profile):
    E, words = edit_with_insert(tmp_path)
    sil = ce.silence_settings(profile, E)
    rs = [{"source": "testimonial_hmj", "start": 0.9, "end": 6.0, "insert": True}]
    assert ce.long_silences(rs, words, sil) == []


def test_layouts_check():
    rs = [{"source": "a", "start": 0, "end": 10, "layout": "face"},
          {"source": "t", "start": 0, "end": 5, "insert": True},
          {"source": "a", "start": 20, "end": 22, "layout": "face"},
          {"source": "a", "start": 30, "end": 60}]
    c = ve._layouts(rs)
    assert c["data"]["seconds"] == {"split": 30.0, "face": 12.0, "full": 5.0}
    assert c["data"]["switches"] == 3 and c["status"] != "PASS"   # the 2 s face run is flagged


def test_validate_allows_inserts_out_of_their_own_order():
    edl = {"sources": {"a": "a.mp4", "t": "t.mp4"}, "inserts": ["t"],
           "ranges": [{"source": "a", "start": 0, "end": 1}, {"source": "t", "start": 50, "end": 52, "insert": True},
                      {"source": "a", "start": 2, "end": 3}, {"source": "t", "start": 10, "end": 12, "insert": True}]}
    assert ce.validate_edl(edl, {"a": 10, "t": 60}, None) == []


def test_place_inserts_keeps_a_word_tail_with_its_range():
    words = {"a": [{"text": "he", "start": 1.0, "end": 1.2}, {"text": "said", "start": 1.3, "end": 1.6},
                   {"text": "next", "start": 5.0, "end": 5.3}]}
    merged = [{"source": "a", "start": 0.9, "end": 1.85}, {"source": "a", "start": 4.9, "end": 5.5}]
    raw = [{"source": "a", "start": 0.9, "end": 1.65}]
    ins = [(0, {"source": "t", "start": 0, "end": 1, "insert": True})]
    out, _ = ce.place_inserts(merged, ins, raw, {0: 1.65}, {**words, "t": [{"text": "x", "start": 0.1, "end": 0.5}]},
                              {}, {"envelopes": {}, "silences": {}})
    assert [r["source"] for r in out] == ["a", "t", "a"]
    assert out[0]["end"] == 1.85 and out[2]["start"] == 4.9      # no stray 1.65-1.85 blip after the insert
