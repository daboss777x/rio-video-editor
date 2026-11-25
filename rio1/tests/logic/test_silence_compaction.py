import json

import pytest

from helpers import chunk_edl as ce
from helpers import verify_edit as ve
from conftest import PROFILE, make_edit

SET = {"policy": "cut_all", "air": {"intro": 0.03, "body": 0.18}, "thr": {"intro": 0.15, "body": 0.35},
       "intro_stems": [], "silences": {}}


def W(*spans):
    return [{"text": f"w{i}", "start": a, "end": b} for i, (a, b) in enumerate(spans)]


def sets(sil, **kw):
    return {**SET, "silences": {"clip": sil}, **kw}


# stretched: "two" is reported 2.0-3.5 but the real speech is 3.2-3.5; pause 2.0-3.2
STRETCHED = W((1.0, 2.0), (2.0, 3.5), (3.5, 4.0))


def run(ranges, words, settings):
    return ce.compact_ranges(ranges, {"clip": words}, settings)[0]


def spans(rs):
    return [(r["start"], r["end"]) for r in rs]


def test_touching_words_alone_not_cut():
    assert spans(run([{"source": "clip", "start": 0.9, "end": 4.1}], STRETCHED, sets([]))) == [(0.91, 4.09)]


def test_stretched_pause_cut_to_air_body():
    out = run([{"source": "clip", "start": 0.9, "end": 4.1}], STRETCHED, sets([[2.0, 3.2]]))
    assert spans(out) == [(0.91, 2.09), (3.11, 4.09)]  # 0.09 + 0.09 = 0.18 air


def test_intro_uses_intro_air_and_threshold():
    r = [{"source": "clip", "start": 0.9, "end": 4.1, "beat": "INTRO hook"}]
    out = run(r, STRETCHED, sets([[2.2, 3.0]]))
    assert spans(out) == [(0.985, 2.215), (2.985, 4.015)]
    # 0.2 s silence: over the intro threshold (0.15) but under the body one (0.35)
    short = [[2.5, 2.7]]
    assert len(run(r, STRETCHED, sets(short))) == 2
    assert len(run([{"source": "clip", "start": 0.9, "end": 4.1}], STRETCHED, sets(short))) == 1


def test_no_silences_matches_old_behaviour():
    ws = W((1.0, 1.3), (2.0, 2.3))
    r = [{"source": "clip", "start": 0.9, "end": 2.4}]
    assert run(r, ws, SET) == run(r, ws, sets([]))
    assert spans(run(r, ws, SET)) == [(0.91, 1.39), (1.91, 2.39)]


def test_silence_overlapping_word_gap_cut_merges():
    ws = W((1.0, 1.3), (2.5, 2.8))
    out = run([{"source": "clip", "start": 0.9, "end": 2.9}], ws, sets([[1.35, 2.45]]))
    assert spans(out) == [(0.91, 1.39), (2.41, 2.89)]
    assert all(a < b for a, b in spans(out))


def test_speech_guard_keeps_word_start():
    ws = W((1.0, 2.0), (2.5, 3.0))
    # silence starts 30 ms into "w0": cut may not start before w0.start + 0.12
    out = run([{"source": "clip", "start": 0.9, "end": 3.1}], ws, sets([[1.03, 1.9]]))
    assert spans(out)[0] == (0.91, 1.12)


def test_silence_at_range_edge_pulls_edge_in():
    ws = W((1.0, 1.4), (3.0, 3.4))
    out = run([{"source": "clip", "start": 0.0, "end": 3.5}], ws, sets([[0.0, 0.95], [3.45, 3.5]]))
    assert spans(out) == [(0.91, 1.49), (2.91, 3.49)] or spans(out)[0][0] == pytest.approx(0.91)


def test_slivers_dropped_and_keep_silence_untouched():
    ws = W((1.0, 1.05), (2.0, 2.4))
    out = run([{"source": "clip", "start": 0.9, "end": 2.5}], ws, sets([[1.1, 1.95]]))
    assert all(b - a >= 0.08 for a, b in spans(out))
    ks = {"source": "clip", "start": 1.0, "end": 3.0, "keep_silence": True}
    assert run([ks], STRETCHED, sets([[2.0, 3.2]])) == [ks]


def test_long_silences_reports_leftover_detected_silence():
    sil = sets([[2.0, 3.2]])
    left = ce.long_silences([{"source": "clip", "start": 0.9, "end": 4.1}], {"clip": STRETCHED}, sil)
    assert left == [{"range_index": 0, "start": 2.0, "end": 3.2}]
    done = run([{"source": "clip", "start": 0.9, "end": 4.1}], STRETCHED, sil)
    assert ce.long_silences(done, {"clip": STRETCHED}, sil) == []


def test_load_silences_and_settings(tmp_path, profile):
    E, _ = make_edit(tmp_path, [(1.0, ["a", "b"])])
    assert ce.load_silences(E, "clip") == []
    (E / "silences").mkdir()
    (E / "silences" / "clip.json").write_text(json.dumps({"source": "clip.mp4", "noise_db": -35, "min_s": 0.3,
                                                          "silences": [[3.0, 4.0], [1.0, 1.5]]}))
    assert ce.load_silences(E, "clip") == [[1.0, 1.5], [3.0, 4.0]]
    assert ce.silence_settings(profile, E)["silences"]["clip"] == [[1.0, 1.5], [3.0, 4.0]]
    assert ce.silence_settings(profile)["silences"] == {}


def _verify_setup(tmp_path, monkeypatch, ranges, silences):
    E, _ = make_edit(tmp_path, [(1.0, ["Open", "the", "menu"])], duration=30.0)  # words 1.0-1.3, 1.4-1.7, 1.8-2.1
    (E / "edl.json").write_text(json.dumps({"version": 1, "sources": {"clip": "../clip.mp4"}, "grade": "none",
        "overlays": [], "ranges": [{"source": "clip", "start": s, "end": e} for s, e in ranges]}))
    if silences:
        (E / "silences").mkdir()
        (E / "silences" / "clip.json").write_text(json.dumps({"silences": silences}))
    total = sum(e - s for s, e in ranges)
    monkeypatch.setattr(ve, "probe", lambda p: {"duration_s": total})
    monkeypatch.setattr(ve, "loudness", lambda p: {"integrated_lufs": -14.0, "true_peak_dbtp": -1.5})
    monkeypatch.setattr(ve, "av_durations", lambda p: {"video_s": 1.0, "audio_s": 1.0})
    return E


def test_verify_edge_inside_silence_not_flagged_but_real_cut_is(tmp_path, monkeypatch):
    prof = json.loads(json.dumps(PROFILE))
    E = _verify_setup(tmp_path, monkeypatch, [(0.9, 1.55)], [[1.5, 1.6]])  # end 1.55 inside "the" and in silence
    c = {x["name"]: x for x in ve.run_checks(E, prof)["checks"]}
    assert c["word_boundaries"]["status"] == "PASS"
    E2 = _verify_setup(tmp_path / "b", monkeypatch, [(0.9, 1.55)], [])
    c2 = {x["name"]: x for x in ve.run_checks(E2, prof)["checks"]}
    assert c2["word_boundaries"]["status"] == "FAIL"


def test_verify_no_long_silence_reports_detected_leftover(tmp_path, monkeypatch):
    prof = json.loads(json.dumps(PROFILE))
    prof["edit"]["silence_policy"] = "cut_all"
    E = _verify_setup(tmp_path, monkeypatch, [(0.9, 2.2)], [[1.35, 1.95]])
    c = {x["name"]: x for x in ve.run_checks(E, prof)["checks"]}
    assert c["no_long_silence"]["status"] == "FAIL"
    assert c["no_long_silence"]["data"]["pauses"][0]["start"] == 1.35


def test_validate_refit_recuts_pause_added_by_a_fix(tmp_path, profile):
    E, _ = make_edit(tmp_path, [(1.0, ["Open", "the", "menu", "now"]), (10.0, ["Then", "click", "save", "button"])], [])
    edl = {"version": 1, "sources": {"clip": "../clip.mp4"}, "grade": "none", "overlays": [],
           "ranges": [{"source": "clip", "start": 0.9, "end": 11.6}], "total_duration_s": 10.7}  # a fix spanning the pause
    (E / "edl.json").write_text(json.dumps(edl))
    assert ce.main(["validate", "--edit-dir", str(E), "--refit"]) == 0
    out = json.loads((E / "edl.json").read_text())
    assert len(out["ranges"]) == 2 and out["total_duration_s"] < 4


def test_refit_leaves_already_compacted_ranges_alone(tmp_path, profile):
    E, _ = make_edit(tmp_path, [(1.0, ["Open", "the", "menu", "now"]), (10.0, ["Then", "click", "save", "button"])], [])
    write = lambda rs: (E / "edl.json").write_text(json.dumps({"version": 1, "sources": {"clip": "../clip.mp4"},
                                                                "ranges": rs, "total_duration_s": 0}))
    clean = [{"source": "clip", "start": 0.91, "end": 2.59}, {"source": "clip", "start": 9.91, "end": 11.59}]
    write(clean)
    assert ce.main(["validate", "--edit-dir", str(E), "--refit"]) == 0
    got = json.loads((E / "edl.json").read_text())["ranges"]
    assert [(r["start"], r["end"]) for r in got] == [(0.91, 2.59), (9.91, 11.59)]   # untouched
