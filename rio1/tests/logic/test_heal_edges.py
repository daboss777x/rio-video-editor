"""Merge/refit and the clipped_words ear check share one definition of 'audible' (heal_edges)."""
from helpers import chunk_edl as ce
from tests.logic.test_envelope_edges import SET, env, W


def S(e, words, **kw):
    return {**SET, "silences": {}, "envelopes": {"clip": e}, **kw}, {"clip": words}


# real-run shape: transcript says "been" ends 3.18 and puts the next word at 5.9, but the audio has
# a second burst of speech 3.4-4.05 (decaying) that the transcript hid inside a silence.
WORDS = [{"text": "has", "start": 2.8, "end": 2.93}, {"text": "been", "start": 3.03, "end": 3.18},
         {"text": "registered.", "start": 5.9, "end": 6.4}]


def decaying():
    e = env([(2.8, 3.2), (3.4, 4.0), (6.5, 7.0)])
    for i, lv in enumerate([-45, -50, -55, -60]):  # fade 4.00-4.04, above threshold (-63) until 4.04
        e.db[400 + i] = lv
    return e


def test_extension_that_added_audio_is_not_undone_by_heal():
    e = decaying()
    s, w = S(e, WORDS)
    rs = [{"source": "clip", "start": 2.7, "end": 3.9}]  # a fix ended it mid-speech
    assert ce.clipped_edges(rs, w, s)
    out, notes = ce.heal_edges(rs, w, s)
    assert out[0]["end"] >= 4.04 and out[0]["start"] == 2.7
    assert ce.clipped_edges(out, w, s) == []


def test_heal_only_extends_never_shrinks():
    e = decaying()
    s, w = S(e, WORDS)
    rs = [{"source": "clip", "start": 2.7, "end": 5.0}]  # long quiet tail already: leave
    out, _ = ce.heal_edges(rs, w, s)
    assert out[0]["end"] == 5.0


def test_compact_end_cannot_fail_clipped_check():
    # max_tail would stop the end short of the second burst; the final guard follows the audio
    e = decaying()
    s, w = S(e, WORDS)
    out, _ = ce.compact_ranges([{"source": "clip", "start": 2.7, "end": 3.9}], w, s)
    assert ce.clipped_edges(out, w, s) == []


def test_start_clipped_is_extended_to_onset():
    e = env([(1.0, 1.5), (2.0, 2.6)])
    words = W((1.0, 1.5), (2.0, 2.6))
    s, w = S(e, words)
    out, _ = ce.heal_edges([{"source": "clip", "start": 2.2, "end": 2.7}], w, s)
    assert out[0]["start"] <= 2.0 - 0.04 + 0.011
    assert ce.clipped_edges(out, w, s) == []


def test_start_a_few_ms_inside_a_dropped_word_moves_out():
    words = W((1.0, 1.5), (1.6, 2.2))
    e = env([(1.0, 1.5), (1.6, 2.2)])
    s, w = S(e, words)
    out, notes = ce.heal_edges([{"source": "clip", "start": 1.495, "end": 2.3}], w, s)
    assert out[0]["start"] == 1.5 and notes


def test_end_a_few_ms_inside_a_dropped_word_moves_out():
    words = W((1.0, 1.5), (1.6, 2.2))
    e = env([(1.0, 1.5), (1.6, 2.2)])
    s, w = S(e, words)
    out, _ = ce.heal_edges([{"source": "clip", "start": 0.9, "end": 1.62}], w, s)
    assert out[0]["end"] == 1.6


def test_edge_deep_inside_a_word_is_left_to_snap():
    words = W((1.0, 2.0))
    out, notes = ce.heal_edges([{"source": "clip", "start": 1.2, "end": 2.5}], {"clip": words},
                               {**SET, "envelopes": {}})
    assert out[0]["start"] == 1.2 and not notes


def test_refit_never_undoes_an_extension_and_heals_a_clipped_edge():
    e = decaying()
    s, w = S(e, WORDS)
    extended = [{"source": "clip", "start": 2.7, "end": 4.5, "beat": "x"}]  # a fix pass already grew it past the fade
    out, _ = ce.refit_ranges(extended, w, s, ["clip"])
    assert out[0]["end"] >= 4.5 and out[0]["start"] == 2.7
    clipped = [{"source": "clip", "start": 2.7, "end": 3.9, "beat": "x"}]
    assert ce.clipped_edges(clipped, w, s)
    out, notes = ce.refit_ranges(clipped, w, s, ["clip"])
    assert out[0]["end"] >= 4.04 and ce.clipped_edges(out, w, s) == [] and notes


def test_refit_joins_ranges_that_heal_into_each_other():
    e = decaying()
    s, w = S(e, WORDS)
    out, _ = ce.refit_ranges([{"source": "clip", "start": 2.7, "end": 3.9}, {"source": "clip", "start": 4.04, "end": 4.5}],
                             w, s, ["clip"])
    assert len(out) == 1 and out[0]["end"] == 4.5
