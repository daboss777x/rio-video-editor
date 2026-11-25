"""Listen-back diff (helpers/hear_check.py): no network, synthetic word lists."""
import json

from helpers import hear_check as hc


def W(text, t):
    return {"text": text, "t": t}


def test_missing_word_at_a_join_is_reported():
    exp = [W("without", 200.0), W("asking", 200.4), W("for", 200.7), W("approval.", 200.9), W("So", 201.6), W("mine", 201.8)]
    heard = [W("without", 200.0), W("asking", 200.4), W("for", 200.7), W("So", 201.6), W("mine", 201.8)]
    res = hc.compare(exp, heard, joins=[201.5])
    assert [d["kind"] for d in res["at_joins"]] == ["missing"]
    assert res["at_joins"][0]["expected"] == "approval."


def test_stray_sound_at_a_join_is_reported_and_far_noise_is_only_counted():
    exp = [W("downloading.", 45.0), W("and", 47.6), W("I", 47.8), W("can", 47.9), W("see", 48.1),
           W("gonna", 90.0), W("take", 90.3)]
    heard = [W("downloading.", 45.0), W("ah", 47.5), W("and", 47.6), W("I", 47.8), W("can", 47.9), W("see", 48.1),
             W("going", 90.0), W("to", 90.1), W("take", 90.3)]
    res = hc.compare(exp, heard, joins=[47.55])
    assert [(d["kind"], d["heard"]) for d in res["at_joins"]] == [("extra", "ah")]
    assert res["elsewhere_count"] == 1


def test_expected_words_follow_the_edl_in_output_order(tmp_path):
    (tmp_path / "transcripts").mkdir()
    words = [{"text": t, "start": s, "end": s + 0.3, "type": "word"} for t, s in
             [("one", 1.0), ("two", 2.0), ("dropped", 3.0), ("three", 4.0)]]
    (tmp_path / "transcripts" / "clip.json").write_text(json.dumps({"words": words}))
    ranges = [{"source": "clip", "start": 0.9, "end": 2.4}, {"source": "clip", "start": 3.9, "end": 4.4}]
    exp, joins = hc.expected_words(tmp_path, ranges)
    assert [w["text"] for w in exp] == ["one", "two", "three"]
    assert joins == [1.5] and exp[2]["t"] == 1.6


def test_a_squashed_word_at_a_join_is_flagged_short():
    heard = [{"text": "approval.", "t": 214.49, "end": 214.61}, {"text": "So", "t": 214.61, "end": 214.79},
             {"text": "everything", "t": 100.0, "end": 100.05}]   # far from any join: ignored
    got = hc.short_at_joins(heard, joins=[214.6])
    assert [d["expected"] for d in got] == ["approval."]
