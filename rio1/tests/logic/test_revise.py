"""Revise loop: notes parsing, windows, splice/heal/checks, versions, report (synthetic data only)."""
import json

import pytest

from helpers import revise as rv
from conftest import make_edit

SENT = [(10.0 * i, ["alpha", "beta", "gamma"]) for i in range(8)]  # words 10i .. 10i+1.1


def R(src, s, e, **kw):
    return {"source": src, "start": s, "end": e, "beat": "B", **kw}


def sent_range(i, src="clip"):
    return R(src, max(0.0, 10.0 * i - 0.05), 10.0 * i + 1.2)


def ranges8():
    return [sent_range(i) for i in range(8)]


def write_edl(E, ranges, sources=("clip",)):
    edl = {"version": 1, "sources": {s: f"../{s}.mp4" for s in sources}, "ranges": ranges,
           "grade": "none", "overlays": [], "total_duration_s": round(sum(r["end"] - r["start"] for r in ranges), 3)}
    (E / "edl.json").write_text(json.dumps(edl, indent=2))
    return edl


@pytest.fixture
def run(tmp_path):
    E, _ = make_edit(tmp_path, SENT)
    write_edl(E, [sent_range(i) for i in range(8) if i != 4])  # sentence 4 (src 40) is CUT
    return E


def notes_file(tmp_path, text):
    p = tmp_path / "notes.txt"
    p.write_text(text)
    return p


def write_window(E, n, wid, ranges):
    (E / "revisions" / f"v{n}" / f"window_{wid:02d}.edl.json").write_text(json.dumps(ranges))


def meta_of(E, n):
    return json.loads((E / "revisions" / f"v{n}" / "meta.json").read_text())["of"]


# ------------------------------------------------------------ notes
@pytest.mark.parametrize("line,secs,text", [
    ("1:05 - tighten this", 65.0, "tighten this"),
    ("01:05 – tighten this", 65.0, "tighten this"),
    ("1:05 — tighten this", 65.0, "tighten this"),
    ("1:05: tighten this", 65.0, "tighten this"),
    ("1:05 tighten this", 65.0, "tighten this"),
    ("1:05.5 - x y", 65.5, "x y"),
    ("1:02:03 - long one", 3723.0, "long one"),
    ("1:02:03.25: long one", 3723.25, "long one"),
    ("  0:07-no space", 7.0, "no space"),
])
def test_timecode_and_separators(line, secs, text):
    v, notes = rv.parse_notes(line)
    assert v is None and notes == [{"tc_s": secs, "text": text}]


def test_version_line_comments_blank_lines():
    v, notes = rv.parse_notes("version: 2\n\n# a comment\n0:10 - one\n   \n0:20: two\n")
    assert v == 2 and [n["text"] for n in notes] == ["one", "two"]


@pytest.mark.parametrize("bad", ["hello there", "1:05", "1:05 - ", "0:10 - a\nversion: 2"])
def test_bad_lines_raise(bad):
    with pytest.raises(ValueError):
        rv.parse_notes(bad)


def test_fmt_tc():
    assert rv.fmt_tc(65) == "1:05" and rv.fmt_tc(3723) == "1:02:03"


# ------------------------------------------------------------ timeline + windows
def test_out_spans_use_frame_counts():
    sp = rv.out_spans([R("c", 0, 1.0), R("c", 5, 5.5)], 30)
    assert sp[0] == (0.0, 1.0) and abs(sp[1][1] - 1.5) < 1e-9
    sp = rv.out_spans([R("c", 0, 0.05)], 30)  # 1.5 frames -> 2
    assert abs(sp[0][1] - 2 / 30) < 1e-9


def test_window_covers_overlapping_ranges_only():
    rs = ranges8()
    sp = rv.out_spans(rs, 30)
    ws = rv.build_windows(rs, [{"tc_s": sp[3][0] + 0.5, "text": "x"}], 30, window_s=1.0)
    assert len(ws) == 1
    w = ws[0]
    assert (w["range_first"], w["range_last"]) == (2, 4)  # tc +- 1 s reaches the neighbours only
    assert w["source"] == "clip" and w["out_start"] == round(sp[2][0], 3) and w["out_end"] == round(sp[4][1], 3)
    assert w["src_start"] == max(0.0, round(rs[2]["start"] - 20, 3))
    assert w["src_end"] == round(rs[4]["end"] + 20, 3)


def test_windows_merge_when_overlapping_and_stay_apart_otherwise():
    rs = ranges8()
    sp = rv.out_spans(rs, 30)
    a, b = {"tc_s": sp[1][0] + 0.1, "text": "a"}, {"tc_s": sp[3][0] + 0.1, "text": "b"}
    ws = rv.build_windows(rs, [a, b], 30, window_s=1.5)
    assert len(ws) == 1 and [n["text"] for n in ws[0]["notes"]] == ["a", "b"]
    far = {"tc_s": sp[7][0] + 0.1, "text": "c"}
    ws = rv.build_windows(rs, [a, far], 30, window_s=1.0)
    assert len(ws) == 2 and [w["id"] for w in ws] == [1, 2]
    assert ws[0]["range_last"] < ws[1]["range_first"]


def test_window_never_spans_two_sources():
    rs = [sent_range(0, "intro"), sent_range(1, "intro"), sent_range(0, "main"), sent_range(1, "main")]
    sp = rv.out_spans(rs, 30)
    notes = [{"tc_s": sp[1][1] - 0.1, "text": "edge"}, {"tc_s": sp[2][0] + 0.1, "text": "next"}]
    ws = rv.build_windows(rs, notes, 30, window_s=60)
    assert [w["source"] for w in ws] == ["intro", "main"]
    assert ws[0]["range_last"] == 1 and ws[1]["range_first"] == 2
    assert [n["text"] for n in ws[0]["notes"]] == ["edge"] and [n["text"] for n in ws[1]["notes"]] == ["next"]


def test_src_end_clamped_to_source_duration():
    ws = rv.build_windows(ranges8(), [{"tc_s": 1.0, "text": "x"}], 30, window_s=60, durs={"clip": 75.0})
    assert ws[0]["src_end"] == 75.0 and ws[0]["src_start"] == 0.0


def test_note_past_end_is_an_error():
    with pytest.raises(ValueError):
        rv.build_windows(ranges8(), [{"tc_s": 500, "text": "x"}], 30)


# ------------------------------------------------------------ start
def start_at_gap(E, tmp_path):
    sp = rv.out_spans(json.loads((E / "edl.json").read_text())["ranges"], 30)
    return rv.start(E, notes_file(tmp_path, f"0:{int(sp[3][0]) + 1:02d} - restore the missing step"), window_s=2)


def fill_gap(E, n):
    """Replacement for window 01 that puts the CUT sentence 4 back among the window's own sentences."""
    w = json.loads((E / "revisions" / f"v{n}" / "windows.json").read_text())[0]
    old = json.loads((E / "edl.json").read_text())["ranges"][w["range_first"]:w["range_last"] + 1]
    ids = sorted({round(r["start"] / 10) for r in old} | {4})
    write_window(E, n, 1, [sent_range(i) for i in ids])
    return ids


def test_start_creates_files_archives_v1_and_marks_kept_cut(run, tmp_path):
    E = run
    v1 = (E / "edl.json").read_text()
    n = start_at_gap(E, tmp_path)
    assert n == 2
    assert (E / "revisions" / "v1" / "edl.json").read_text() == v1
    d = E / "revisions" / "v2"
    assert (d / "notes.txt").exists()
    wins = json.loads((d / "windows.json").read_text())
    assert set(wins[0]) == {"id", "notes", "out_start", "out_end", "range_first", "range_last", "source", "src_start", "src_end"}
    md = (d / "window_01.md").read_text()
    assert "restore the missing step" in md and "KEPT: alpha beta gamma" in md
    assert any(l.startswith("[40.00-") and "CUT: alpha beta gamma" in l for l in md.splitlines())  # sentence 4 dropped
    assert "```json" in md and '"start"' in md


def test_start_cli_prints_windows(run, tmp_path, capsys):
    assert rv.main(["start", "--edit-dir", str(run), "--notes", str(notes_file(tmp_path, "0:02 - hi")),
                    "--window-s", "2"]) == 0
    assert "window_01" in capsys.readouterr().out
    assert rv.main(["start", "--edit-dir", str(run), "--notes", str(notes_file(tmp_path, "garbage"))]) == 1


def test_versions_v3_and_version_line_and_default_latest(run, tmp_path):
    E = run
    rv.start(E, notes_file(tmp_path, "0:02 - first"), window_s=2)
    v1 = json.loads((E / "edl.json").read_text())
    v2 = {**v1, "ranges": v1["ranges"][:-1]}  # pretend v2 was applied
    (E / "revisions" / "v2" / "edl.json").write_text(json.dumps(v2))
    (E / "edl.json").write_text(json.dumps(v2))
    assert rv.start(E, notes_file(tmp_path, "version: 1\n0:02 - from the first"), window_s=2) == 3
    assert meta_of(E, 3) == 1
    (E / "revisions" / "v3" / "edl.json").write_text(json.dumps(v2))
    assert rv.start(E, notes_file(tmp_path, "0:02 - default"), window_s=2) == 4
    assert meta_of(E, 4) == 3
    assert rv.start(E, notes_file(tmp_path, "0:02 - flag"), of=2, window_s=2) == 4 and meta_of(E, 4) == 2


def test_v1_archive_is_never_overwritten(run, tmp_path):
    E = run
    v1 = (E / "edl.json").read_text()
    rv.start(E, notes_file(tmp_path, "0:02 - one"), window_s=2)
    (E / "edl.json").write_text("{}")
    (E / "revisions" / "v2" / "edl.json").write_text("{}")
    with pytest.raises(Exception):
        rv.start(E, notes_file(tmp_path, "0:02 - two"), window_s=2)  # base v2 is junk: fails, v1 untouched
    assert (E / "revisions" / "v1" / "edl.json").read_text() == v1


# ------------------------------------------------------------ apply
def test_apply_splices_heals_and_writes_versions(run, tmp_path, profile):
    E = run
    n = start_at_gap(E, tmp_path)
    w = json.loads((E / "revisions" / f"v{n}" / "windows.json").read_text())[0]
    fill_gap(E, n)
    ok, viol, edl, changes = rv.apply(E, n, profile)
    assert ok, viol
    assert [round(r["start"]) for r in edl["ranges"]] == [0, 10, 20, 30, 40, 50, 60, 70]
    assert len(changes) == 1 and changes[0]["window"] == 1
    assert len(changes[0]["before"]) == w["range_last"] - w["range_first"] + 1
    assert 40 in [round(r["start"]) for r in changes[0]["after"]]
    assert changes[0]["new_out_end"] > changes[0]["new_out_start"]
    assert len(json.loads((E / "edl.json").read_text())["ranges"]) == 7  # apply() alone writes nothing
    assert rv.main(["apply", "--edit-dir", str(E), "--version", str(n)]) == 0
    latest = json.loads((E / "edl.json").read_text())
    assert latest == json.loads((E / "revisions" / f"v{n}" / "edl.json").read_text()) and len(latest["ranges"]) == 8
    assert json.loads((E / "revisions" / f"v{n}" / "changes.json").read_text())[0]["window"] == 1
    assert (E / "revisions" / "v1" / "edl.json").exists()


def test_apply_descending_order_keeps_indices_valid(tmp_path, profile):
    E, _ = make_edit(tmp_path, SENT)
    write_edl(E, ranges8())
    sp = rv.out_spans(ranges8(), 30)
    txt = f"0:{int(sp[1][0]) + 1:02d} - A\n0:{int(sp[6][0]) + 1:02d} - B"
    n = rv.start(E, notes_file(tmp_path, txt), window_s=0.5)
    w1, w2 = json.loads((E / "revisions" / f"v{n}" / "windows.json").read_text())
    write_window(E, n, 1, [])                # window 1 removes its ranges
    write_window(E, n, 2, [sent_range(6)])   # window 2 keeps one range
    ok, viol, edl, ch = rv.apply(E, n, profile)
    assert ok, viol
    expect = [i * 10 for i in range(8)]
    for i in range(w1["range_first"], w1["range_last"] + 1):
        expect.remove(i * 10)
    for i in range(w2["range_first"], w2["range_last"] + 1):
        if i != 6:
            expect.remove(i * 10)
    assert [round(r["start"]) for r in edl["ranges"]] == expect
    assert ch[0]["after"] == [] and ch[0]["new_out_start"] == ch[0]["new_out_end"]
    assert len(ch[1]["after"]) == 1


def test_failure_leaves_edl_untouched(run, tmp_path):
    E = run
    n = start_at_gap(E, tmp_path)
    before = (E / "edl.json").read_text()
    write_window(E, n, 1, [R("clip", 0.0, 500.0)])  # outside the window
    assert rv.main(["apply", "--edit-dir", str(E), "--version", str(n)]) == 1
    assert (E / "edl.json").read_text() == before
    assert not (E / "revisions" / f"v{n}" / "edl.json").exists()
    assert not (E / "revisions" / f"v{n}" / "changes.json").exists()


def test_final_validation_failure_also_leaves_edl_untouched(run, tmp_path):
    E = run
    before = (E / "edl.json").read_text()
    n = rv.start(E, notes_file(tmp_path, "0:02 - remove everything"), window_s=600)
    write_window(E, n, 1, [])
    ok, viol, *_ = rv.apply(E, n)
    assert not ok and any("no ranges" in v for v in viol)
    assert rv.main(["apply", "--edit-dir", str(E), "--version", str(n)]) == 1
    assert (E / "edl.json").read_text() == before


@pytest.mark.parametrize("rep,msg", [
    ([R("clip", 0.0, 500.0)], "outside the window"),
    ([R("other", 30.0, 31.0)], "is not 'clip'"),
    ([{"source": "clip", "start": 30}], "needs source/start/end"),
])
def test_bad_replacements_are_reported_per_window(run, tmp_path, rep, msg):
    E = run
    n = start_at_gap(E, tmp_path)
    write_window(E, n, 1, rep)
    ok, viol, *_ = rv.apply(E, n)
    assert not ok and any(v.startswith("window 01:") and msg in v for v in viol)


def test_missing_window_file_fails(run, tmp_path):
    n = start_at_gap(run, tmp_path)
    ok, viol, *_ = rv.apply(run, n)
    assert not ok and "missing window_01.edl.json" in viol[0]


def test_apply_heals_an_end_that_cuts_audible_speech(tmp_path, profile):
    # the audio has a second burst of speech (to 11.5) that the transcript hides; Rosa's range ends at 11.0
    from tests.logic.test_envelope_edges import env
    E, _ = make_edit(tmp_path, [(10.0, ["has", "been", "registered"]), (30.0, ["next", "one", "here"])])
    write_edl(E, [R("clip", 9.9, 11.3), R("clip", 29.9, 31.3)])
    n = rv.start(E, notes_file(tmp_path, "0:00 - x"), window_s=1)
    e = env([(10.0, 11.5), (30.0, 31.1)], dur=40)
    (E / "silences").mkdir(exist_ok=True)
    (E / "silences" / "clip.envelope.json").write_text(json.dumps({"hop_s": 0.01, "floor_db": -75, "db": e.db}))
    write_window(E, n, 1, [R("clip", 9.9, 11.0)])
    ok, viol, edl, ch = rv.apply(E, n, profile)
    assert ok, viol
    assert edl["ranges"][0]["end"] >= 11.5 and ch[0]["after"][0]["end"] >= 11.5


# ------------------------------------------------------------ report
def applied(run, tmp_path):
    n = start_at_gap(run, tmp_path)
    fill_gap(run, n)
    assert rv.main(["apply", "--edit-dir", str(run), "--version", str(n)]) == 0
    return run, n


def test_report_writes_plain_text_and_appends_lessons(run, tmp_path):
    E, n = applied(run, tmp_path)
    les = tmp_path / "LESSONS.md"
    les.write_text("# Lessons\n\n## Approved\n- keep\n\n## Proposed\n- [old] a line\n\n## Other\nx\n")
    text = rv.report(E, n, les, today="2026-10-30")
    assert "restored 3 words" in text and "alpha beta gamma" in text and "Where" in text
    assert (E / "revisions" / f"v{n}" / "REVISION.md").read_text() == text
    got = les.read_text().split("\n")
    line = '- [2026-10-30 run v2] "restore the missing step" -> restored 3 words "alpha beta gamma"'
    assert line in got
    assert got.index("## Proposed") < got.index(line) < got.index("## Other")
    assert "- [old] a line" in got and "- keep" in got
    rv.report(E, n, les, today="2026-10-30")  # same line again: not duplicated
    assert les.read_text().split("\n").count(line) == 1


def test_report_says_removed_words_and_creates_lessons(tmp_path):
    E, _ = make_edit(tmp_path, SENT)
    write_edl(E, ranges8())
    n = rv.start(E, notes_file(tmp_path, "0:07 - drop this"), window_s=1)
    w = json.loads((E / "revisions" / f"v{n}" / "windows.json").read_text())[0]
    write_window(E, n, 1, [sent_range(i) for i in range(w["range_first"], w["range_last"] + 1) if i != 5])
    assert rv.main(["apply", "--edit-dir", str(E), "--version", str(n)]) == 0
    les = tmp_path / "L.md"
    assert "removed 3 words" in rv.report(E, n, les, today="2026-10-30")
    assert "## Proposed" in les.read_text() and "drop this" in les.read_text()


def test_trust_audio_edges_keeps_a_stretched_last_word():
    """HMJ v3: the editor ended at 3373.10 inside 'one.' (transcript 3372.61-3374.84, sound gone by
    3373.0); snap dropped the word, and the end must come back out to the real fade + tail."""
    from helpers import revise as rv

    class Env:
        duration = 4000.0

        def offset(self, t, limit, edge=False):
            return min(3373.0, limit)

        def voiced(self, a, b, edge=False):
            return 1.0

    words = [{"text": "next", "start": 3372.29, "end": 3372.611},
             {"text": "one.", "start": 3372.611, "end": 3374.843}]
    sil = {"envelopes": {"s": Env()}, "intro_stems": None, "tail": {"body": 0.1, "intro": 0.06},
           "lead": {"body": 0.04, "intro": 0.04}}
    r = {"source": "s", "start": 3367.98, "end": 3373.10, "beat": "OUTRO"}
    ns, ne = rv.trust_audio_edges(3367.98, 3373.10, 3367.98, 3372.611, words, sil, r)
    assert abs(ne - 3373.10) < 1e-6          # fade 3373.0 + 0.1 tail, capped at her end


@pytest.mark.parametrize("line,start,end,text", [
    ("20:30 - 21:55 Cut out this entire TDD section.", 1230.0, 1315.0, "Cut out this entire TDD section."),
    ("1:46 - 2:26 blur the email part", 106.0, 146.0, "blur the email part"),
    ("0:10–0:20: tighten", 10.0, 20.0, "tighten"),
    ("1:02:03 - 1:02:30 - long one", 3723.0, 3750.0, "long one"),
])
def test_span_notes_keep_their_end_time(line, start, end, text):
    # 2026-11-17: "20:30 - 21:55 cut this" was read as 20:30 only; the window stopped at 21:30.
    v, notes = rv.parse_notes(line)
    assert notes == [{"tc_s": start, "tc_end_s": end, "text": text}]


def test_span_note_window_reaches_past_its_end_time():
    rs = ranges8()
    sp = rv.out_spans(rs, 30)
    note = {"tc_s": sp[1][0] + 0.5, "tc_end_s": sp[5][0] + 0.5, "text": "cut all of this"}
    ws = rv.build_windows(rs, [note], 30, window_s=1.0)
    assert len(ws) == 1
    assert (ws[0]["range_first"], ws[0]["range_last"]) == (0, 6)   # start-1 s .. end+1 s
    assert ws[0]["notes"] == [note]


def test_window_file_shows_the_whole_span_to_the_editor(run, tmp_path):
    n = rv.start(run, notes_file(tmp_path, "0:05 - 0:09 cut this whole bit\n"))
    md = (run / "revisions" / f"v{n}" / "window_01.md").read_text()
    assert "[0:05 - 0:09] cut this whole bit" in md


def test_a_backwards_span_is_an_error_naming_the_line():
    with pytest.raises(ValueError, match="line 1.*21:55.*before.*20:30|line 1.*ends before"):
        rv.parse_notes("21:55 - 20:30 cut this")
