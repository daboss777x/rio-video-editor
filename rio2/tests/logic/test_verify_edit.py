import json

import pytest

from helpers import verify_edit as ve
from conftest import PROFILE, make_edit

SENTS = [(1.0, ["Open", "the", "menu", "now"]), (10.0, ["Then", "click", "save", "button"])]
ACTIVE = {"start": 2.5, "end": 10.0, "class": "active", "change_score": 0.2, "typing_score": 0, "must_keep": [3.0, 9.5]}


def setup(tmp_path, monkeypatch, ranges, final_s=None, peak=-1.5, video_s=10.0, audio_s=10.0, duration=100.0):
    E, _ = make_edit(tmp_path, SENTS, [ACTIVE], duration=duration)
    edl = {"version": 1, "sources": {"clip": "../clip.mp4"}, "grade": "none", "overlays": [],
           "ranges": [{"source": "clip", "start": s, "end": e} for s, e in ranges]}
    total = sum(e - s for s, e in ranges)
    (E / "edl.json").write_text(json.dumps(edl))
    monkeypatch.setattr(ve, "probe", lambda p: {"duration_s": total if final_s is None else final_s})
    monkeypatch.setattr(ve, "loudness", lambda p: {"integrated_lufs": -14.0, "true_peak_dbtp": peak})
    monkeypatch.setattr(ve, "av_durations", lambda p: {"video_s": video_s, "audio_s": audio_s})
    return E


@pytest.fixture
def profile(profile):
    # most tests here exercise the original rule (active spans must be kept)
    profile["edit"]["silence_policy"] = "keep_active"
    return profile


@pytest.fixture
def cut_all():
    return json.loads(json.dumps(PROFILE)) | {"edit": {**PROFILE["edit"], "silence_policy": "cut_all"}}


def by_name(res):
    return {c["name"]: c for c in res["checks"]}


def test_all_pass(tmp_path, monkeypatch, profile):
    E = setup(tmp_path, monkeypatch, [(0.9, 11.7)], duration=30.0)
    res = ve.run_checks(E, profile)
    c = by_name(res)
    assert res["overall"] == "PASS", res
    assert set(c) == {"duration", "step_coverage", "word_boundaries", "audio_peak", "av_sync", "cut_ratio", "cam_sync"}
    assert c["cut_ratio"]["data"]["kept_ratio"] > 0.25


def test_broken_duration(tmp_path, monkeypatch, profile):
    E = setup(tmp_path, monkeypatch, [(0.9, 11.7)], final_s=5.0, duration=30.0)
    res = ve.run_checks(E, profile)
    assert by_name(res)["duration"]["status"] == "FAIL" and res["overall"] == "FAIL"


def test_mid_word_cut_and_uncovered_step(tmp_path, monkeypatch, profile):
    E = setup(tmp_path, monkeypatch, [(0.9, 2.0), (9.9, 11.7)], duration=30.0)  # ends inside "menu"; drops active span
    c = by_name(ve.run_checks(E, profile))
    assert c["word_boundaries"]["status"] == "FAIL"
    assert c["word_boundaries"]["data"]["violations"][0]["word"] == "menu"
    assert c["step_coverage"]["status"] == "FAIL" and c["step_coverage"]["data"]["uncovered"] == [[3.0, 9.5]]


def test_peak_and_sync_fail(tmp_path, monkeypatch, profile):
    E = setup(tmp_path, monkeypatch, [(0.9, 11.7)], peak=-0.5, video_s=10.0, audio_s=10.2, duration=30.0)
    c = by_name(ve.run_checks(E, profile))
    assert c["audio_peak"]["status"] == "FAIL" and c["av_sync"]["status"] == "FAIL"


def test_cut_ratio_is_advisory(tmp_path, monkeypatch, profile):
    E = setup(tmp_path, monkeypatch, [(0.9, 11.7)], duration=500.0)  # keeps ~2% -> flagged
    res = ve.run_checks(E, profile)
    assert by_name(res)["cut_ratio"]["status"] == "FAIL"
    assert res["overall"] == "PASS"


def test_media_error_is_a_failure_not_a_crash(tmp_path, monkeypatch, profile):
    E = setup(tmp_path, monkeypatch, [(0.9, 11.7)], duration=30.0)
    monkeypatch.setattr(ve, "probe", lambda p: (_ for _ in ()).throw(RuntimeError("no file")))
    res = ve.run_checks(E, profile)
    assert by_name(res)["duration"]["status"] == "FAIL"


def test_cli_writes_checks_json(tmp_path, monkeypatch):
    E = setup(tmp_path, monkeypatch, [(0.9, 11.7)], duration=30.0)
    assert ve.main(["--edit-dir", str(E)]) == 0
    assert json.loads((E / "verify" / "checks.json").read_text())["overall"] in ("PASS", "FAIL")


def test_cut_all_passes_compacted_ranges(tmp_path, monkeypatch, cut_all):
    E = setup(tmp_path, monkeypatch, [(0.94, 2.56), (9.94, 11.56)], duration=30.0)
    res = ve.run_checks(E, cut_all)
    c = by_name(res)
    assert set(c) == {"duration", "no_long_silence", "word_boundaries", "audio_peak", "av_sync", "cut_ratio", "cam_sync"}
    assert c["no_long_silence"]["status"] == "PASS", c["no_long_silence"]


def test_cut_all_flags_pause_left_in_a_range(tmp_path, monkeypatch, cut_all):
    E = setup(tmp_path, monkeypatch, [(0.9, 11.7)], duration=30.0)  # keeps the 2.5-10.0 pause
    c = by_name(ve.run_checks(E, cut_all))
    assert c["no_long_silence"]["status"] == "FAIL"
    assert {"range_index": 0, "start": 2.5, "end": 10.0} in c["no_long_silence"]["data"]["pauses"]


# ---- every flagged edge gets Ines's call (2026-11-21: v1 had 61 cut_in_sound edges nobody reviewed)
CHECKS = {"checks": [
    {"name": "cut_in_sound", "data": {"edges": [{"range_index": 3, "edge": "end", "time": 10.2},
                                                {"range_index": 7, "edge": "start", "time": 40.1}]}},
    {"name": "clipped_words", "data": {"edges": [{"range_index": 9, "edge": "end", "time": 55.0, "word": "project"}]}},
]}


def test_coverage_lists_flagged_edges_the_verdict_skips():
    verdict = {"verdict": "FAIL", "problems": [{"rank": 1, "fix": {"op": "extend", "range_index": 3}}],
               "edges": [{"id": "r3.end", "call": "clipped"}, {"id": "r9.end", "call": "filler"}]}
    assert ve.verdict_coverage(CHECKS, verdict) == ["r7.start"]


def test_coverage_needs_a_fix_for_every_edge_called_clipped():
    verdict = {"verdict": "FAIL", "problems": [{"rank": 1, "fix": {"op": "extend", "range_index": 3}}],
               "edges": [{"id": "r3.end", "call": "clipped"}, {"id": "r7.start", "call": "clipped"},
                         {"id": "r9.end", "call": "filler"}]}
    assert ve.verdict_coverage(CHECKS, verdict) == ["r7.start"]     # called clipped, but no fix on range 7


def test_coverage_cli_fails_and_names_skipped_edges(tmp_path, capsys):
    V = tmp_path / "verify"
    V.mkdir()
    (V / "checks.json").write_text(json.dumps(CHECKS))
    (V / "verdict.json").write_text(json.dumps({"verdict": "PASS", "problems": [],
                                                "edges": [{"id": "r9.end", "call": "filler"}]}))
    assert ve.main(["--edit-dir", str(tmp_path), "--coverage"]) == 1
    out = capsys.readouterr().out
    assert "r3.end" in out and "r7.start" in out and "r9.end" not in out
    (V / "verdict.json").write_text(json.dumps({"verdict": "PASS", "problems": [], "edges": [
        {"id": i, "call": "filler"} for i in ("r3.end", "r7.start", "r9.end")]}))
    assert ve.main(["--edit-dir", str(tmp_path), "--coverage"]) == 0


def test_flagged_edges_carry_the_id_ines_answers_with(tmp_path, monkeypatch, cut_all):
    E = setup(tmp_path, monkeypatch, [(0.9, 1.55)], duration=30.0)     # ends while "the" is still sounding
    (E / "silences").mkdir(exist_ok=True)
    loud = [-75.0] * 90 + [-20.0] * 300 + [-75.0] * 2610            # speech 0.9-3.9 s, 10 ms frames
    (E / "silences" / "clip.envelope.json").write_text(json.dumps({"hop_s": 0.01, "floor_db": -75, "db": loud}))
    res = by_name(ve.run_checks(E, cut_all))
    edges = res["cut_in_sound"]["data"]["edges"]
    assert edges and {e["id"] for e in edges} == {"r0.end"}
    assert "r0.end" in res["cut_in_sound"]["detail"]
