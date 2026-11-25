"""Cut edges placed on the loudness envelope (user, 2026-10-29: no clipped word endings, ever).

Synthetic 10 ms envelopes: -75 dB room, -40 dB speech; threshold = floor + 12 = -63 dB."""
from helpers import chunk_edl as ce

HOP = 0.01
SET = {"policy": "cut_all", "air": {"intro": 0.03, "body": 0.18}, "thr": {"intro": 0.15, "body": 0.35},
       "tail": {"intro": 0.06, "body": 0.2}, "lead": {"intro": 0.04, "body": 0.08},
       "max_tail": 0.4, "max_lead": 0.3, "intro_stems": [], "silences": {}}


def env(sound, dur=10.0, level=-40, soft=None):
    """sound: [(a, b)] at speech level; soft: [(a, b)] at -55 dB (a fading 'ch' or 'st')."""
    db = [-75] * int(round(dur / HOP))
    for spans, lv in ((sound, level), (soft or [], -55)):
        for a, b in spans:
            for i in range(int(round(a / HOP)), int(round(b / HOP))):
                db[i] = lv
    return ce.Envelope(db, HOP, -75, 12, 0.1)


def W(*spans):
    return [{"text": f"w{i}", "start": a, "end": b} for i, (a, b) in enumerate(spans)]


def run(ranges, words, e, sil=None, **kw):
    s = {**SET, "silences": {"clip": sil or []}, "envelopes": {"clip": e}, **kw}
    return ce.compact_ranges(ranges, {"clip": words}, s)


def spans(rs):
    return [(r["start"], r["end"]) for r in rs]


def test_offset_follows_soft_ending_past_transcript_end():
    e = env([(1.0, 1.5)], soft=[(1.5, 1.7)])
    assert abs(e.offset(1.5, 3.0) - 1.7) < 0.011


def test_offset_walks_back_when_transcript_end_is_late():
    e = env([(1.0, 1.5)])
    assert abs(e.offset(1.75, 3.0) - 1.5) < 0.011


def test_onset_walks_back_to_early_sound():
    e = env([(1.0, 1.5)])
    assert abs(e.onset(1.05, 0.0) - 1.0) < 0.011


def test_short_closure_does_not_end_the_word():
    # 'token': 50 ms stop closure, then the 'n' - one word, not an ending
    e = env([(1.0, 1.3), (1.35, 1.5)])
    assert abs(e.offset(1.25, 3.0) - 1.5) < 0.011


def test_range_end_keeps_the_whole_fading_word_plus_tail():
    # transcript says the word ends at 1.5, the soft ending sounds until 1.7; editor ended at 1.52
    e = env([(1.0, 1.5)], soft=[(1.5, 1.7)])
    out, _ = run([{"source": "clip", "start": 0.95, "end": 1.52}], W((1.0, 1.5), (4.0, 4.5)), e)
    (s, t), = spans(out)
    assert abs(t - 1.9) < 0.011 and abs(s - 0.92) < 0.011


def test_edge_never_reaches_into_a_dropped_neighbour():
    # sound runs straight into the next (dropped) word at 1.6: the end stops 40 ms before it
    e = env([(1.0, 2.0)])
    out, _ = run([{"source": "clip", "start": 0.95, "end": 1.55}], W((1.0, 1.5), (1.6, 2.0)), e)
    assert abs(spans(out)[0][1] - 1.56) < 0.011


def test_pause_cut_to_tail_plus_lead():
    e = env([(1.0, 1.5), (3.0, 3.5)])
    out, _ = run([{"source": "clip", "start": 0.9, "end": 3.6}], W((1.0, 1.5), (3.0, 3.5)), e)
    assert spans(out) == [(0.92, 1.7), (2.92, 3.7)]


def test_short_real_pause_is_left_alone():
    # 0.25 s of real quiet in the body (threshold 0.35): not a pause worth a cut
    e = env([(1.0, 1.5), (1.75, 2.2)])
    out, _ = run([{"source": "clip", "start": 0.9, "end": 2.3}], W((1.0, 1.5), (1.75, 2.2)), e)
    assert len(out) == 1


def test_stretched_word_over_a_pause_is_cut_on_the_audio():
    # AssemblyAI stretched w1 over 1.5-3.0; the audio is quiet there
    e = env([(1.0, 1.5), (3.0, 3.5)])
    out, _ = run([{"source": "clip", "start": 0.9, "end": 3.6}], W((1.0, 1.5), (1.5, 3.5)), e, sil=[[1.5, 3.0]])
    assert spans(out) == [(0.92, 1.7), (2.92, 3.7)]


def test_scrap_without_a_whole_word_is_dropped():
    # an 'oop': 60 ms of sound, then a pause, then the real word (the transcript stretched it over both)
    e = env([(1.0, 1.06), (2.0, 2.4)])
    out, notes = run([{"source": "clip", "start": 0.9, "end": 2.5}], W((1.0, 2.4)), e, sil=[[1.06, 2.0]])
    assert spans(out) == [(1.92, 2.6)]
    assert any("scrap" in n for n in notes)


def test_squashed_words_follow_the_audio_to_the_next_silence():
    words = W((1.0, 1.3), (1.30, 1.32), (1.32, 1.34), (1.34, 1.36), (5.0, 5.3))
    e = env([(1.0, 3.2), (5.0, 5.3)])
    out, notes = run([{"source": "clip", "start": 0.95, "end": 1.4}], words, e, sil=[[3.2, 5.0]])
    assert abs(spans(out)[0][1] - 3.4) < 0.011
    assert any("squashed" in n for n in notes)


def test_compaction_is_idempotent():
    e = env([(1.0, 1.5), (3.0, 3.5), (6.0, 6.4)], soft=[(1.5, 1.65)])
    words = W((1.0, 1.5), (3.0, 3.5), (6.0, 6.4))
    once, _ = run([{"source": "clip", "start": 0.9, "end": 6.5}], words, e, sil=[[1.6, 3.0], [3.5, 6.0]])
    twice, _ = run(once, words, e, sil=[[1.6, 3.0], [3.5, 6.0]])
    assert spans(once) == spans(twice)
    s = {**SET, "silences": {"clip": [[1.6, 3.0], [3.5, 6.0]]}, "envelopes": {"clip": e}}
    assert ce.long_silences(once, {"clip": words}, s) == []


def test_long_silences_reports_an_uncut_pause():
    e = env([(1.0, 1.5), (3.0, 3.5)])
    s = {**SET, "envelopes": {"clip": e}}
    left = ce.long_silences([{"source": "clip", "start": 0.9, "end": 3.6}], {"clip": W((1.0, 1.5), (3.0, 3.5))}, s)
    assert len(left) == 1


def test_clipped_edges_flags_a_cut_before_the_fade():
    e = env([(1.0, 1.7)])
    s = {**SET, "envelopes": {"clip": e}}
    words = {"clip": W((1.0, 1.5), (3.0, 3.5))}
    bad = ce.clipped_edges([{"source": "clip", "start": 0.95, "end": 1.55}], words, s)
    assert [b["edge"] for b in bad] == ["end"]
    assert ce.clipped_edges([{"source": "clip", "start": 0.95, "end": 1.9}], words, s) == []


def test_clipped_edges_ignores_a_filler_cut_in_continuous_speech():
    # "so uh the": sound never fades between the kept word and the dropped filler
    e = env([(1.0, 2.0)])
    s = {**SET, "envelopes": {"clip": e}}
    words = {"clip": W((1.0, 1.4), (1.45, 1.6), (1.6, 2.0))}
    assert ce.clipped_edges([{"source": "clip", "start": 0.95, "end": 1.42}], words, s) == []


def test_without_envelope_the_old_path_runs():
    s = {**SET, "silences": {"clip": []}}
    out, _ = ce.compact_ranges([{"source": "clip", "start": 0.9, "end": 1.6}], {"clip": W((1.0, 1.5))}, s)
    assert spans(out) == [(0.91, 1.59)]


def test_range_covering_untranscribed_speech_is_not_shrunk():
    # the transcript lost a word at 2.0-2.3 ("map"); a fix extended the range to 2.35
    e = env([(1.0, 1.5), (2.0, 2.3)])
    out, _ = run([{"source": "clip", "start": 0.95, "end": 2.35}], W((1.0, 1.5), (5.0, 5.5)), e)
    assert spans(out)[-1][1] >= 2.5 - 0.011


def test_edge_margin_keeps_a_soft_final_s():
    # 2026-11-03 "websites": the final "s" sits 16 dB over the room. With the pause line at
    # floor + 20 it reads as quiet; the gentler edge line (floor + 13) keeps it.
    db = [-75] * 1000
    for i in range(100, 130):
        db[i] = -40          # "website" 1.00-1.30
    for i in range(130, 145):
        db[i] = -59          # soft "s" 1.30-1.45
    e = ce.Envelope(db, HOP, -75, 20, 0.1, edge_margin=13)
    assert abs(e.offset(1.25, 3.0) - 1.30) < 0.011           # pause line: "s" is quiet
    assert abs(e.offset(1.25, 3.0, edge=True) - 1.45) < 0.011  # edge line: "s" is sound
    words = [{"text": "websites.", "start": 1.0, "end": 1.3}]
    got = run([{"source": "clip", "start": 0.95, "end": 1.4, "beat": "STEP"}], words, e)[0]
    assert got[0]["end"] >= 1.45


def test_soft_ending_air_is_not_reported_as_a_pause():
    db = [-75] * 1000
    for i in range(100, 130):
        db[i] = -40
    for i in range(130, 145):
        db[i] = -59
    e = ce.Envelope(db, HOP, -75, 20, 0.1, edge_margin=13)
    words = {"clip": [{"text": "websites.", "start": 1.0, "end": 1.3}]}
    s = {**SET, "silences": {"clip": [[1.3, 3.0]]}, "envelopes": {"clip": e}}
    r = {"source": "clip", "start": 0.95, "end": 1.6, "beat": "STEP"}   # ends 0.15 s after the "s"
    assert ce.long_silences([r], words, s) == []


def test_cuts_in_sound_flags_an_end_inside_running_sound_only():
    e = env([(1.0, 1.8)])
    s = {**SET, "envelopes": {"clip": e}}
    clipped = {"source": "clip", "start": 0.95, "end": 1.5}   # sound runs on to 1.8
    clean = {"source": "clip", "start": 0.95, "end": 1.9}     # ends after the fade
    assert ce.cuts_in_sound([clipped], s) == [{"range_index": 0, "edge": "end", "time": 1.5}]
    assert ce.cuts_in_sound([clean], s) == []
    late_start = {"source": "clip", "start": 1.3, "end": 1.9}  # sound began at 1.0
    assert ce.cuts_in_sound([late_start], s) == [{"range_index": 0, "edge": "start", "time": 1.3}]


def test_word_whose_sound_is_inside_a_piece_is_not_a_scrap():
    # Real case (testscreen1, 'system'): the transcript stretched the word back to 1.2 so it touches
    # the previous kept range, but its real sound is 1.6-1.75 (under 0.25 s). A verifier fix starts
    # the next range at 1.5; the edge can't walk back into the previous range, so the first piece is
    # not "whole" by transcript times. It holds all the word's sound, so it is the word, not a scrap.
    e = env([(0.5, 1.1), (1.6, 1.75), (3.0, 3.5)])
    words = W((0.5, 1.1), (1.2, 1.9), (3.0, 3.5))
    out, notes = run([{"source": "clip", "start": 0.4, "end": 1.2}, {"source": "clip", "start": 1.5, "end": 3.6}],
                     words, e, sil=[[1.1, 1.6], [1.75, 3.0]])
    kept = [sp for sp in spans(out) if sp[0] < 1.75 and sp[1] > 1.6]
    assert kept and kept[0][0] <= 1.6 and kept[0][1] >= 1.75, spans(out)
