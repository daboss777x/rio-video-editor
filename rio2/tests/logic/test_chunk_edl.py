import json

import pytest

from helpers import chunk_edl as ce
from conftest import make_edit

LONG = [(10.0 * i, ["word%d" % i, "alpha", "beta"]) for i in range(1, 31)]  # sentence every 10s, ~1.1s long


def test_split_breaks_at_gaps_near_target(tmp_path, profile):
    gaps = [{"start": 10.0 * i + 1.1, "end": 10.0 * i + 10, "class": "static", "change_score": 0, "typing_score": 0, "must_keep": None}
            for i in range(1, 30)]
    E, words = make_edit(tmp_path, LONG, gaps, duration=320)
    metas = ce.split(E, profile, chunk_minutes=1.5)  # target 90 s
    assert len(metas) >= 3
    assert metas[0]["start"] == 0.0 and metas[-1]["end"] == 320
    for a, b in zip(metas, metas[1:]):
        assert a["end"] == b["start"]
        # boundary is inside no word
        assert not any(w["start"] < a["end"] < w["end"] for w in words)
        assert abs(a["end"] - a["start"] - 90) < 15
    assert all(m["source"] == "clip.mp4" for m in metas)
    md = (E / "chunks" / "chunk_01.md").read_text()
    assert "ACTIVITY" in md and "static" in md
    assert json.loads((E / "chunks" / "chunk_02.json").read_text())["index"] == 2


def test_split_single_chunk_for_short_video(tmp_path, profile):
    E, _ = make_edit(tmp_path, LONG[:5])
    assert len(ce.split(E, profile)) == 1


def test_words_lines_only_for_fillers_and_repeats(tmp_path, profile):
    sents = [(1.0, ["Open", "the", "settings", "menu"]),
             (6.0, ["So", "basically", "um", "we", "click", "save"]),
             (12.0, ["Then", "we", "we", "go", "home"]),
             (18.0, ["Clean", "sentence", "here", "today"])]
    E, _ = make_edit(tmp_path, sents)
    ce.split(E, profile)
    md = (E / "chunks" / "chunk_01.md").read_text()
    words_lines = [l for l in md.splitlines() if "WORDS" in l]
    assert len(words_lines) == 2
    assert any("um@" in l for l in words_lines) and any("we@" in l for l in words_lines)


def test_activity_lines_carry_class_and_keep(tmp_path, profile):
    gap = {"start": 3.0, "end": 9.0, "class": "active", "change_score": 0.1, "typing_score": 0, "must_keep": [4.0, 9.0]}
    E, _ = make_edit(tmp_path, [(1.0, ["a", "b"]), (10.0, ["c", "d"])], [gap])
    ce.split(E, profile)
    assert "ACTIVITY 3.00-9.00 active keep=4.00-9.00" in (E / "chunks" / "chunk_01.md").read_text()


# ------------------------------------------------------------ merge
SENTS = [(1.0, ["Open", "the", "menu", "now"]), (10.0, ["Then", "click", "save", "button"]), (20.0, ["All", "done", "here"])]
# words: Open 1.0-1.3, the 1.4-1.7, menu 1.8-2.1, now 2.2-2.5 | Then 10.0-10.3 click 10.4-10.7 save 10.8-11.1 button 11.2-11.5
ACTIVE = {"start": 2.5, "end": 10.0, "class": "active", "change_score": 0.2, "typing_score": 0, "must_keep": [3.0, 9.5]}


def write_edl(E, ranges, n=1):
    (E / "chunks").mkdir(exist_ok=True)
    (E / "chunks" / f"edl_{n:02d}.json").write_text(json.dumps(ranges))


def R(s, e, **kw):
    return {"source": "clip", "start": s, "end": e, "beat": "B", "quote": "q", "reason": "r", **kw}


def keep_active(profile):
    profile["edit"]["silence_policy"] = "keep_active"
    return profile


def test_merge_rejects_dropped_active_span(tmp_path, profile):
    keep_active(profile)
    E, _ = make_edit(tmp_path, SENTS, [ACTIVE])
    write_edl(E, [R(0.9, 2.6), R(9.9, 11.6)])  # active 3.0-9.5 dropped
    edl, viol, _ = ce.merge(E, profile)
    assert any("active step 3.00-9.50" in v for v in viol)


def test_merge_cli_exits_nonzero_and_lists(tmp_path, profile, capsys):
    E, _ = make_edit(tmp_path, SENTS, [ACTIVE])
    write_edl(E, [R(0.9, 2.6), R(9.9, 11.6)])
    prof = tmp_path / "keep_active.json"
    prof.write_text(json.dumps(keep_active(profile)))
    assert ce.main(["merge", "--edit-dir", str(E), "--profile", str(prof)]) == 1
    assert "active step" in capsys.readouterr().err
    assert not (E / "edl.json").exists()


def test_merge_accepts_and_writes_video_use_format(tmp_path, profile):
    E, _ = make_edit(tmp_path, SENTS, [ACTIVE])
    write_edl(E, [R(0.9, 11.6)])
    assert ce.main(["merge", "--edit-dir", str(E)]) == 0
    edl = json.loads((E / "edl.json").read_text())
    assert edl["grade"] == "none" and edl["overlays"] == [] and edl["version"] == 1
    assert edl["sources"] == {"clip": "../clip.mp4"}
    assert edl["total_duration_s"] == pytest.approx(sum(r["end"] - r["start"] for r in edl["ranges"]))
    assert ce.main(["validate", "--edit-dir", str(E)]) == 0


def test_merge_snaps_mid_word_boundary(tmp_path, profile):
    E, words = make_edit(tmp_path, SENTS, [ACTIVE])
    # start 1.85 is inside "menu" (1.8-2.1, nearer its start); end 11.0 is inside "save"... use 11.4 inside "button" (11.2-11.5)
    write_edl(E, [R(1.85, 2.6), R(9.9, 11.4)])
    edl, viol, notes = ce.merge(E, profile)
    r0, r1 = edl["ranges"][0], edl["ranges"][-1]
    assert r0["start"] < 1.8 and r0["start"] >= 1.8 - 0.2 - 1e-9          # pad before "menu", within pad_ms
    assert r0["start"] >= 1.7 - 1e-9                                       # never into "the" (ends 1.7)
    assert r1["end"] > 11.5 and r1["end"] <= 11.5 + 0.2 + 1e-9             # "button" kept whole
    for r in edl["ranges"]:
        for w in words:
            assert not (w["start"] < r["start"] < w["end"]) and not (w["start"] < r["end"] < w["end"])
    assert notes


def test_merge_never_crosses_neighbour_word(tmp_path, profile):
    E, words = make_edit(tmp_path, SENTS, [])
    write_edl(E, [R(1.75, 2.15)])  # tiny gaps between words (0.1s): pad must stay inside the gap
    edl, _, _ = ce.merge(E, profile)
    r = edl["ranges"][0]
    assert r["start"] >= 1.7 and r["start"] <= 1.8      # between "the" end and "menu" start
    assert r["end"] >= 2.1 and r["end"] <= 2.2


def test_merge_sorts_and_coalesces_overlaps(tmp_path, profile):
    E, _ = make_edit(tmp_path, SENTS, [])
    write_edl(E, [R(9.9, 11.6), R(0.9, 2.6)], 1)
    write_edl(E, [R(2.0, 3.0)], 2)  # overlaps the first-by-time range
    edl, viol, _ = ce.merge(E, profile)
    assert not viol
    assert [r["start"] for r in edl["ranges"]] == sorted(r["start"] for r in edl["ranges"])
    assert len(edl["ranges"]) == 2 and edl["ranges"][0]["end"] >= 2.5


def test_validate_flags_overlap_order_and_duration(profile):
    edl = {"sources": {"clip": "../clip.mp4"}, "ranges": [
        {"source": "clip", "start": 5, "end": 10}, {"source": "clip", "start": 8, "end": 12},
        {"source": "clip", "start": 1, "end": 2}, {"source": "clip", "start": 50, "end": 130}]}
    v = ce.validate_edl(edl, {"clip": 100.0}, None)
    text = "\n".join(v)
    assert "overlaps" in text and "out of order" in text and "after source duration" in text


def test_active_span_split_across_two_ranges_is_violation():
    edl = {"sources": {"clip": "x"}, "ranges": [{"source": "clip", "start": 0, "end": 5}, {"source": "clip", "start": 5.5, "end": 10}]}
    act = {"source": "clip.mp4", "gaps": [{"start": 1, "end": 9, "class": "active", "must_keep": [4, 6]}]}
    assert ce.validate_edl(edl, {"clip": 20.0}, act)


def test_active_gap_start_preserved_by_snap(tmp_path, profile):
    # range starts inside the long active gap (before pad_max from next word): must_keep must survive
    E, _ = make_edit(tmp_path, SENTS, [ACTIVE])
    write_edl(E, [R(3.0, 11.6), R(0.9, 2.6)])
    edl, viol, _ = ce.merge(E, profile)
    assert not viol


# ---------- silence policy cut_all (default since 2026-10-29)

def test_cut_all_cuts_pause_even_over_active_screen(tmp_path, profile):
    assert profile["edit"]["silence_policy"] == "cut_all"
    E, _ = make_edit(tmp_path, SENTS, [ACTIVE])
    write_edl(E, [R(0.9, 11.6)])
    edl, viol, _ = ce.merge(E, profile)
    assert not viol
    got = [(r["start"], r["end"]) for r in edl["ranges"]]
    assert got == [(0.955, 2.545), (9.955, 11.545)]      # 45 ms either side of the words: 90 ms body air (halved 2026-11-03)


def test_cut_all_intro_gets_30ms_air(tmp_path, profile):
    E, _ = make_edit(tmp_path, SENTS, [])
    write_edl(E, [R(0.9, 11.6, beat="INTRO")])
    edl, _, _ = ce.merge(E, profile)
    assert [(r["start"], r["end"]) for r in edl["ranges"]] == [(0.985, 2.515), (9.985, 11.515)]


def test_cut_all_keeps_announced_silent_demo(tmp_path, profile):
    E, _ = make_edit(tmp_path, SENTS, [ACTIVE])
    write_edl(E, [R(0.9, 11.6, keep_silence=True)])
    edl, viol, _ = ce.merge(E, profile)
    assert not viol and len(edl["ranges"]) == 1 and edl["ranges"][0]["end"] - edl["ranges"][0]["start"] > 10


def test_wordless_silent_demo_touching_speech_keeps_its_silence(tmp_path, profile):
    # "now watch" ... silent clicking 2.6-9.9 ... "Then click save": the demo touches both spoken ranges
    E, _ = make_edit(tmp_path, SENTS, [ACTIVE])
    write_edl(E, [R(0.9, 2.6), R(2.6, 9.9, keep_silence=True), R(9.9, 11.6)])
    edl, viol, _ = ce.merge(E, profile)
    demo = [r for r in edl["ranges"] if r.get("keep_silence")]
    assert not viol and len(demo) == 1
    assert demo[0]["start"] <= 2.6 and demo[0]["end"] >= 9.9     # the whole 7.3 s demo plays
    assert edl["total_duration_s"] > 7.3


def test_silent_demo_overlapping_speech_stays_whole_and_valid(tmp_path, profile):
    E, _ = make_edit(tmp_path, SENTS, [ACTIVE])
    write_edl(E, [R(0.9, 2.6), R(2.3, 9.9, keep_silence=True), R(9.9, 11.6)])
    edl, viol, _ = ce.merge(E, profile)
    demo = [r for r in edl["ranges"] if r.get("keep_silence")]
    assert not viol and len(demo) == 1 and demo[0]["start"] <= 2.6 and demo[0]["end"] >= 9.9
    rs = edl["ranges"]
    assert all(a["end"] <= b["start"] + 1e-6 for a, b in zip(rs, rs[1:]))   # nothing plays twice


def test_cut_all_drops_wordless_range_and_short_gaps_survive(tmp_path, profile):
    E, _ = make_edit(tmp_path, SENTS, [ACTIVE])
    write_edl(E, [R(0.9, 2.6), R(3.5, 9.0), R(9.9, 11.6)])
    edl, viol, notes = ce.merge(E, profile)
    assert not viol and len(edl["ranges"]) == 2               # 0.1 s word gaps stay inside one range
    assert any("wordless" in n for n in notes)
    assert ce.long_silences(edl["ranges"], {"clip": ce.load_words(E, "clip")}, ce.silence_settings(profile)) == []
