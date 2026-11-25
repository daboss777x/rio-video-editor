"""Moved sections (user, 2026-11-10): a section recorded late is moved to where it belongs with
`move_after: T` on its ranges; merge places it, validate accepts it, re-joins keep it in place."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "helpers"))
import chunk_edl as ce  # noqa: E402


def R(s, e, **kw):
    return {"source": "main", "start": s, "end": e, "beat": "STEP", **kw}


def test_place_moved_goes_after_target():
    ranges = [R(0, 10), R(10.5, 20), R(30, 40), R(50, 60, move_after=12), R(61, 70, move_after=12), R(80, 90)]
    out, notes = ce.place_moved(ranges)
    assert [(r["start"]) for r in out] == [0, 10.5, 50, 61, 30, 80]
    assert notes and "moved 2 range(s)" in notes[0]


def test_place_moved_noop_without_marks():
    ranges = [R(0, 10), R(20, 30)]
    out, notes = ce.place_moved(ranges)
    assert out == ranges and notes == []


def test_join_touching_keeps_moved_block_in_place():
    ranges = [R(0, 10), R(50, 55, move_after=10), R(55, 60, move_after=10), R(10, 20)]
    out = ce._join_touching(ranges, {"main": 0})
    # the moved block is joined within itself but never sorted against, or joined to, the rest
    assert [(r["start"], r["end"], r.get("move_after")) for r in out] == [
        (0, 10, None), (50, 60, 10), (10, 20, None)]


def test_validate_accepts_moved_and_catches_overlap():
    edl = {"sources": {"main": "../main.mp4"},
           "ranges": [R(0, 10), R(50, 60, move_after=10), R(20, 30), R(70, 80)]}
    assert ce.validate_edl(edl, {"main": 100.0}, None) == []
    edl["ranges"][1] = R(25, 60, move_after=10)
    v = ce.validate_edl(edl, {"main": 100.0}, None)
    assert any("moved range overlaps" in x for x in v)


def test_validate_still_catches_out_of_order_normal_ranges():
    edl = {"sources": {"main": "../main.mp4"}, "ranges": [R(20, 30), R(0, 10)]}
    assert any("out of order" in x for x in ce.validate_edl(edl, {"main": 100.0}, None))
