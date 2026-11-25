"""Revise: a start the editor moved earlier for a clipped word onset is kept (2026-11-10, "Today")."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "helpers"))
import chunk_edl as ce  # noqa: E402
import revise  # noqa: E402

WORDS = [{"text": "Today", "start": 50.638, "end": 50.799}, {"text": "I'm", "start": 50.816, "end": 50.977},
         {"text": "gonna", "start": 50.977, "end": 51.139}]


def test_pre_roll_keeps_an_earlier_start_and_holds_it():
    r = {"source": "m", "start": 50.15, "end": 51.2}
    out = revise.pre_roll(r, 50.61, WORDS)
    assert out["start"] == 50.15 and out["hold_edges"] is True


def test_pre_roll_is_capped_at_half_a_second():
    out = revise.pre_roll({"source": "m", "start": 49.0, "end": 51.2}, 50.61, WORDS)
    assert out["start"] == round(50.638 - revise.PRE_ROLL_MAX_S, 3)


def test_pre_roll_ignores_a_start_that_was_not_moved_earlier():
    r = {"source": "m", "start": 50.6, "end": 51.2}
    assert revise.pre_roll(r, 50.61, WORDS) is r


def test_held_range_keeps_lead_and_tail_but_cuts_inner_pauses():
    r = {"source": "m", "start": 50.15, "end": 52.0, "hold_edges": True}
    cuts = [[50.2, 50.55], [50.85, 51.0], [51.3, 51.9]]
    assert ce._held(r, WORDS, cuts) == [[50.85, 51.0]]
    assert ce._held({**r, "hold_edges": False}, WORDS, cuts) == cuts
