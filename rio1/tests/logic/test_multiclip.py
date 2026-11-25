import json

import pytest

from helpers import chunk_edl as ce
from helpers import estimate as est
from helpers import verify_edit as ve
from conftest import make_edit, make_multi_edit

LONG = [(10.0 * i, ["word%d" % i, "alpha", "beta"]) for i in range(1, 31)]  # ~320 s
SHORT = [(1.0, ["Open", "the", "menu", "now"]), (3.0, ["Then", "click", "save"])]


def static_gaps(n=29):
    return [{"start": 10.0 * i + 1.1, "end": 10.0 * i + 10, "class": "static", "change_score": 0, "typing_score": 0,
             "must_keep": None} for i in range(1, n + 1)]


def R(src, s, e, **kw):
    return {"source": src, "start": s, "end": e, "beat": "B", "quote": "q", "reason": "r", **kw}


def write_edl(E, ranges, n=1):
    (E / "chunks").mkdir(exist_ok=True)
    (E / "chunks" / f"edl_{n:02d}.json").write_text(json.dumps(ranges))


# ------------------------------------------------------------ loading
def test_load_sources_reads_file_and_falls_back(tmp_path):
    E, _ = make_multi_edit(tmp_path, {"intro": (SHORT, None), "main1": (SHORT, None)})
    s = ce.load_sources(E)
    assert s["order"] == ["intro", "main1"] and s["files"]["main1"] == "main1.mp4" and s["plan"] is None
    (E / "sources.json").unlink()
    s = ce.load_sources(E)  # old run: videos in the run dir
    assert s["order"] == ["intro", "main1"]


def test_load_sources_single_old_run(tmp_path):
    E, _ = make_edit(tmp_path, SHORT)
    assert ce.load_sources(E) == {"order": ["clip"], "files": {"clip": "clip.mp4"}, "plan": None}


def test_load_activity_per_stem_and_fallback(tmp_path):
    g = {"start": 3.0, "end": 9.0, "class": "active", "must_keep": [4.0, 9.0]}
    E, _ = make_multi_edit(tmp_path, {"a": (SHORT, None), "b": (SHORT, None)}, activity={"b": [g]})
    assert ce.load_activity(E, "b")["gaps"] == [g]
    assert ce.load_activity(E, "a") is None and ce.load_activity(E) is None
    E2, _ = make_edit(tmp_path / "old", SHORT, [g])
    assert ce.load_activity(E2, "clip")["gaps"] == [g] and ce.load_activity(E2)["gaps"] == [g]


def test_source_duration_uses_clips(tmp_path):
    E, _ = make_multi_edit(tmp_path, {"a": (SHORT, 50), "b": (SHORT, 70)})
    assert ce.source_duration(E, "a", [], only_source=False) == 50
    assert ce.source_duration(E, "b", [], only_source=False) == 70


# ------------------------------------------------------------ split
def test_split_follows_sources_order_and_never_spans(tmp_path, profile):
    # takes order is b, a; sources.json order is a, b
    E, _ = make_multi_edit(tmp_path, {"b": (LONG, 320), "a": (SHORT, 20)},
                           activity={"b": static_gaps()})
    (E / "sources.json").write_text(json.dumps({"order": ["a", "b"], "files": {"a": "a.mp4", "b": "b.mp4"}, "plan": None}))
    metas = ce.split(E, profile, chunk_minutes=1.5)
    assert metas[0]["source"] == "a.mp4" and metas[0]["start"] == 0.0 and metas[0]["end"] == 20
    assert [m["index"] for m in metas] == list(range(1, len(metas) + 1))
    rest = metas[1:]
    assert len(rest) >= 3 and all(m["source"] == "b.mp4" for m in rest)
    assert rest[0]["start"] == 0.0 and rest[-1]["end"] == 320
    assert (E / "chunks" / "chunk_01.md").read_text().startswith("# chunk 01 | a.mp4 |")
    assert "ACTIVITY" in (E / "chunks" / "chunk_02.md").read_text()
    assert "ACTIVITY" not in (E / "chunks" / "chunk_01.md").read_text()


# ------------------------------------------------------------ merge
def test_merge_orders_by_sources_even_if_chunks_out_of_order(tmp_path, profile):
    E, _ = make_multi_edit(tmp_path, {"intro": (SHORT, None), "main1": (SHORT, None), "main2": (SHORT, None)})
    write_edl(E, [R("main2", 0.9, 4.0)], n=1)
    write_edl(E, [R("intro", 0.9, 4.0)], n=2)
    write_edl(E, [R("main1", 2.9, 4.0), R("main1", 0.9, 2.5)], n=3)
    edl, viol, _ = ce.merge(E, profile)
    assert not viol
    seq = [r["source"] for r in edl["ranges"]]  # compaction may split a range at a long pause
    assert list(dict.fromkeys(seq)) == ["intro", "main1", "main2"]
    assert seq == sorted(seq, key=["intro", "main1", "main2"].index)
    m1 = [r["start"] for r in edl["ranges"] if r["source"] == "main1"]
    assert m1 == sorted(m1)
    assert list(edl["sources"]) == ["intro", "main1", "main2"]
    assert edl["sources"]["main2"] == "../main2.mp4"


def test_merge_sources_lists_every_clip(tmp_path, profile):
    E, _ = make_multi_edit(tmp_path, {"a": (SHORT, None), "b": (SHORT, None)})
    write_edl(E, [R("b", 0.9, 4.0)])
    edl, viol, _ = ce.merge(E, profile)
    assert not viol and list(edl["sources"]) == ["a", "b"]


def test_intro_pacing_only_for_the_hook(tmp_path, profile):
    gapsent = [(1.0, ["Open", "the"]), (4.0, ["menu", "now"])]  # word gap ~2.6 s inside the range
    E, _ = make_multi_edit(tmp_path, {"Intro": (gapsent, None), "main": (gapsent, None)})
    write_edl(E, [R("Intro", 0.9, 4.9, beat="INTRO"), R("main", 0.9, 4.9, beat="INTRO")])  # INTRO in main: ignored
    edl, viol, _ = ce.merge(E, profile)
    assert not viol
    intro = [r for r in edl["ranges"] if r["source"] == "Intro"]
    body = [r for r in edl["ranges"] if r["source"] == "main"]
    assert len(intro) == 2 and len(body) == 2
    def air(rs):  # silence left at the join: tail after "the" (ends 1.7) + lead before "menu" (starts 4.0)
        return (rs[0]["end"] - 1.7) + (4.0 - rs[1]["start"])
    assert air(intro) == pytest.approx(0.030, abs=0.002)
    assert air(body) == pytest.approx(0.090, abs=0.002)    # body air halved 2026-11-03


def test_section_rules():
    # only the editor's hook labels are punchy; the intro clip's name alone is not (it can run 20+ min)
    assert ce.section({"source": "intro2", "beat": "X"}) == "body"
    assert ce.section({"source": "INTRO", "beat": "INTRO"}) == "intro"
    assert ce.section({"source": "main1", "beat": "INTRO hook"}) == "intro"   # single-clip runs: label wins
    assert ce.section({"source": "main1", "beat": "Setup"}) == "body"
    assert ce.section({"source": "outro", "beat": "x"}) == "body"


def test_merge_keep_active_checks_each_clips_activity(tmp_path, profile):
    profile["edit"]["silence_policy"] = "keep_active"
    g = {"start": 2.5, "end": 9.0, "class": "active", "must_keep": [3.0, 8.5]}
    E, _ = make_multi_edit(tmp_path, {"a": (SHORT, 20), "b": (SHORT, 20)}, activity={"b": [g]})
    write_edl(E, [R("a", 0.9, 4.0), R("b", 0.9, 4.0)])
    _, viol, _ = ce.merge(E, profile)
    assert any("active step 3.00-8.50" in v for v in viol)


def test_old_single_clip_run_still_merges(tmp_path, profile):
    E, _ = make_edit(tmp_path, SHORT)
    write_edl(E, [R("clip", 0.9, 4.0)])
    edl, viol, _ = ce.merge(E, profile)
    assert not viol and edl["sources"] == {"clip": "../clip.mp4"}


# ------------------------------------------------------------ verify
def test_verify_multiclip_totals(tmp_path, profile, monkeypatch):
    E, _ = make_multi_edit(tmp_path, {"intro": (SHORT, 10), "main1": (SHORT, 30)})
    write_edl(E, [R("intro", 0.9, 4.0), R("main1", 0.9, 4.0)])
    edl, viol, _ = ce.merge(E, profile)
    assert not viol
    (E / "edl.json").write_text(json.dumps(edl))
    kept = edl["total_duration_s"]
    monkeypatch.setattr(ve, "probe", lambda p: {"duration_s": kept})
    monkeypatch.setattr(ve, "loudness", lambda p: {"true_peak_dbtp": -2.0, "integrated_lufs": -14})
    monkeypatch.setattr(ve, "av_durations", lambda p: {"video_s": kept, "audio_s": kept})
    res = ve.run_checks(E, profile, final=E / "final.mp4")
    by = {c["name"]: c for c in res["checks"]}
    assert by["duration"]["status"] == "PASS" and set(by["duration"]["data"]["per_clip"]) == {"intro", "main1"}
    assert by["cut_ratio"]["data"]["source_s"] == 40
    assert by["cut_ratio"]["data"]["kept_ratio"] == pytest.approx(kept / 40, abs=1e-3)
    assert by["no_long_silence"]["status"] == "PASS"


# ------------------------------------------------------------ estimate
def test_estimate_sums_two_sources(profile):
    a = {"duration_s": 1200.0, "width": 1920, "height": 1080, "fps": "30/1", "vfr": False}
    b = {"duration_s": 2400.0, "width": 1280, "height": 720, "fps": "60/1", "vfr": True}
    src = est.combine_sources([a, b])
    assert src["duration_s"] == 3600 and src["width"] == 1920 and src["vfr"] is True
    e = est.estimate(profile, src, [])
    assert e["source"]["duration_s"] == 3600
    # 20 min -> 2 chunks, 40 min -> 3 (chunks never span clips; not ceil(60/15)=4)
    assert e["drivers"]["chunks"] == 2 + 3 and e["source"]["clip_count"] == 2
    one = est.estimate(profile, {**a, "duration_s": 2400.0}, [])
    assert one["drivers"]["chunks"] == 3 and e["drivers"]["source_min"] == 60


def test_estimate_cli_from_probe_json_with_clips(tmp_path):
    E, _ = make_multi_edit(tmp_path, {"a": (SHORT, 1200), "b": (SHORT, 1500)})
    out = tmp_path / "est.json"
    rc = est.main(["--probe-json", str(E / "probe.json"), "--out", str(out), "--history", str(tmp_path / "none.jsonl")])
    assert rc in (0, 3)
    d = json.loads(out.read_text())
    assert d["source"]["duration_s"] == 2700 and d["drivers"]["chunks"] == 2 + 2


def test_section_with_intro_clip_ignores_intro_beats_elsewhere():
    stems = ["intro"]
    assert ce.section({"source": "main1", "beat": "INTRO hook"}, stems) == "body"
    assert ce.section({"source": "intro", "beat": "Setup"}, stems) == "body"
    assert ce.section({"source": "intro", "beat": "INTRO"}, stems) == "intro"


def test_suffix_intro_stem_gets_intro_pacing():
    assert ce.is_intro_stem("setterintro") and ce.is_intro_stem("intro2") and ce.is_intro_stem("setter-intro")
    assert not ce.is_intro_stem("settermain1") and not ce.is_intro_stem("introduction-to-n8n-main1")
    assert ce.section({"source": "setterintro", "beat": "INTRO"}, ["setterintro"]) == "intro"
    assert ce.section({"source": "setterintro", "beat": "SETUP"}, ["setterintro"]) == "body"
