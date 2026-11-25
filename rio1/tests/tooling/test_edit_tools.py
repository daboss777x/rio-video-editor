"""edit_tools.py: the compact views the tutorial-edit agents read instead of raw JSON."""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROJECT = ROOT.parent  # ROOT = this system's code (rio1/ or rio2/); commands run from PROJECT
TOOL = PROJECT / ".claude" / "skills" / ("tutorial-edit-2" if ROOT.name == "rio2" else "tutorial-edit") / "scripts" / "edit_tools.py"


def tool(*args):
    return subprocess.run([sys.executable, str(TOOL), *map(str, args)], capture_output=True, text=True,
                          cwd=PROJECT, timeout=60)


def make_edit(tmp_path):
    E = tmp_path / "edit"
    (E / "transcripts").mkdir(parents=True)
    words = []
    t = 0.5
    for i, w in enumerate("open the menu then open the menu and click save now done".split()):
        words.append({"text": w, "start": t, "end": t + 0.3, "type": "word"})
        t += 0.5 if i != 3 else 5.0
    (E / "transcripts" / "clip.json").write_text(json.dumps({"words": words}))
    (E / "probe.json").write_text(json.dumps({"path": "clip.mov", "duration_s": 20.0}))
    (E / "activity.json").write_text(json.dumps({"gaps": [
        {"start": 2.3, "end": 6.8, "class": "active", "must_keep": [5.0, 6.8]},
        {"start": 0.0, "end": 0.5, "class": "static", "must_keep": None}]}))
    (E / "edl.json").write_text(json.dumps({"version": 1, "sources": {"clip": "../clip.mov"}, "ranges": [
        {"source": "clip", "start": 0.4, "end": 2.4, "beat": "INTRO"},
        {"source": "clip", "start": 4.9, "end": 9.0, "beat": "STEP"},
        {"source": "clip", "start": 9.4, "end": 12.0, "beat": "OUTRO"}], "total_duration_s": 8.7}))
    return E


def test_kept_in_output_order(tmp_path):
    E = make_edit(tmp_path)
    p = tool("kept", "--edit-dir", E)
    assert p.returncode == 0, p.stderr
    lines = p.stdout.splitlines()
    assert lines[0].startswith("#0 out 00:00.0-00:02.0") and "open the menu then" in lines[0]
    assert lines[1].startswith("#1 out 00:02.0-00:06.1")


def test_joins_flags_active_edges_and_prints_view_commands(tmp_path):
    E = make_edit(tmp_path)
    p = tool("joins", "--edit-dir", E)
    assert p.returncode == 0, p.stderr
    assert "2 joins; checking 2" in p.stdout
    assert "active edge" in p.stdout and "timeline_view.py" in p.stdout


def test_activity_summary_and_window(tmp_path):
    E = make_edit(tmp_path)
    p = tool("activity", "--edit-dir", E)
    assert "2 gaps" in p.stdout and "active" in p.stdout
    w = tool("activity", "--edit-dir", E, "--from", 3, "--to", 4)
    assert w.stdout.strip() == "[2.30-6.80] active must_keep=[5.0, 6.8]"


def test_out2src_and_stats(tmp_path):
    E = make_edit(tmp_path)
    assert "range #1 source clip t=5.900" in tool("out2src", "--edit-dir", E, 3.0).stdout
    s = tool("stats", "--edit-dir", E)
    assert s.returncode == 0, s.stderr
    assert "| Ranges kept | 3 |" in s.stdout and "| Cuts | 4 |" in s.stdout and "Scribe cost" in s.stdout


def test_missing_input_fails_with_reason(tmp_path):
    p = tool("kept", "--edit-dir", tmp_path)
    assert p.returncode == 1 and "missing" in p.stderr


def test_lessons_prints_only_approved():
    p = tool("lessons")
    assert p.returncode == 0 and "## Proposed" not in p.stdout


def test_multi_clip_activity_per_stem(tmp_path):
    E = tmp_path / "edit"
    (E / "activity").mkdir(parents=True)
    (E / "sources.json").write_text(json.dumps({"order": ["intro", "main1"],
                                                "files": {"intro": "intro.mp4", "main1": "main1.mp4"}, "plan": None}))
    (E / "activity" / "intro.json").write_text(json.dumps({"gaps": [{"start": 1, "end": 2, "class": "static"}]}))
    (E / "activity" / "main1.json").write_text(json.dumps({"gaps": [
        {"start": 3, "end": 5, "class": "active", "must_keep": [4.0]}, {"start": 8, "end": 9, "class": "static"}]}))
    assert "3 gaps" in tool("activity", "--edit-dir", E).stdout
    w = tool("activity", "--edit-dir", E, "--stem", "main1", "--from", 2, "--to", 6)
    assert w.stdout.strip() == "[3.00-5.00] active must_keep=[4.0]"
    assert tool("activity", "--edit-dir", E, "--from", 2, "--to", 6).returncode == 1


def test_loud_shows_where_the_sound_is(tmp_path):
    E = make_edit(tmp_path)
    # 1 s quiet, 0.5 s speech, 0.5 s soft, 1 s quiet (10 ms frames); "then" sits at 2.0-2.3 in the quiet.
    db = [-70] * 100 + [-20] * 50 + [-50] * 50 + [-70] * 100
    (E / "silences").mkdir()
    (E / "silences" / "clip.envelope.json").write_text(json.dumps({"source": "clip", "hop_s": 0.01,
                                                                    "floor_db": -70, "db": db}))
    (E / "silences" / "clip.json").write_text(json.dumps({"silences": [[2.0, 3.0]]}))
    p = tool("loud", "--edit-dir", E, "--stem", "clip", "--from", 0, "--to", 3)
    assert p.returncode == 0, p.stderr
    rows = [l.split("|")[1] for l in p.stdout.splitlines() if l.startswith("  ") and "|" in l]
    strip = "".join(rows)
    assert strip == "." * 10 + "#" * 5 + "+" * 5 + "." * 10
    assert "then@2.00-2.30" in p.stdout and "detected silences: 2.00-3.00" in p.stdout
    p = tool("loud", "--edit-dir", E, "--stem", "nope", "--from", 0, "--to", 3)
    assert p.returncode == 1 and "envelope" in p.stderr


def test_edges_lists_every_flagged_edge_with_its_sound_around_the_cut(tmp_path):
    E = make_edit(tmp_path)
    (E / "silences").mkdir()
    db = [-75.0] * 2000
    for k in range(480, 560):        # "then" still sounding 4.8-5.6 s, past range 1's start at 4.9
        db[k] = -20.0
    (E / "silences" / "clip.envelope.json").write_text(json.dumps({"hop_s": 0.01, "floor_db": -75, "db": db}))
    (E / "verify").mkdir()
    edges = [{"range_index": i, "edge": "start", "time": 4.9 + i, "id": f"r{i}.start", "output_s": 2.0} for i in range(1, 2)]
    (E / "verify" / "checks.json").write_text(json.dumps({"checks": [
        {"name": "cut_in_sound", "data": {"edges": edges}},
        {"name": "clipped_words", "data": {"edges": []}}]}))
    p = tool("edges", "--edit-dir", E)
    assert p.returncode == 0, p.stderr
    line = next(l for l in p.stdout.splitlines() if l.startswith("r1.start"))
    assert "00:02.0" in line and "cut_in_sound" in line and "|" in line
    assert "1 flagged edge" in p.stdout
