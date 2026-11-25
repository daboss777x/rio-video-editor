import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "helpers" / "media" / "detect_screen_activity.py"


@pytest.fixture(scope="module")
def activity(fixture_paths, tmp_path_factory):
    out = tmp_path_factory.mktemp("detect") / "activity.json"
    subprocess.run([sys.executable, str(SCRIPT), "--video", str(fixture_paths["video"]),
                    "--transcript", str(fixture_paths["transcript"]), "--out", str(out),
                    "--profile", str(ROOT / "profiles" / "tutorial.json")], check=True)
    return json.loads(out.read_text())


def _match(gaps, seg):
    return [g for g in gaps if abs(g["start"] - seg["start"]) < 0.05 and abs(g["end"] - seg["end"]) < 0.05]


def test_one_gap_per_silent_segment(activity, truth):
    silent = [s for s in truth["segments"] if s["expected_class"]]
    assert len(activity["gaps"]) == len(silent)
    for s in silent:
        assert len(_match(activity["gaps"], s)) == 1, s


@pytest.mark.parametrize("label,expected", [("static", "static"), ("click", "active"),
                                            ("typing", "typing"), ("cursor", "static"),
                                            ("lead_in", "static"), ("tail", "static")])
def test_labels(activity, truth, label, expected):
    segs = [s for s in truth["segments"] if s["label"] == label]
    assert segs
    for s in segs:
        (g,) = _match(activity["gaps"], s)
        assert g["class"] == expected, (label, g)


def test_must_keep_contract(activity, truth):
    for g in activity["gaps"]:
        if g["class"] == "static":
            assert g["must_keep"] is None
        elif g["class"] == "typing":
            assert g["must_keep"] == [round(g["end"] - 1.0, 3), g["end"]]
        else:
            a, b = g["must_keep"]
            assert g["start"] <= a < b <= g["end"]
    # click: panel at +3 s and page switch at +6 s must both be inside must_keep
    for s in truth["segments"]:
        if s["label"] == "click":
            (g,) = _match(activity["gaps"], s)
            a, b = g["must_keep"]
            assert a <= s["start"] + 3.0 and b >= s["start"] + 6.0


def test_output_shape(activity):
    assert activity["source"] == "tutorial_fixture.mp4"
    assert activity["profile"] == "tutorial"
    assert activity["params"]["scene_T"] == 0.003
    for g in activity["gaps"]:
        assert {"start", "end", "class", "change_score", "typing_score", "must_keep"} <= set(g)


def test_cli_override(fixture_paths, tmp_path):
    out = tmp_path / "a.json"
    subprocess.run([sys.executable, str(SCRIPT), "--video", str(fixture_paths["video"]),
                    "--transcript", str(fixture_paths["transcript"]), "--out", str(out),
                    "--profile", str(ROOT / "profiles" / "tutorial.json"),
                    "--scene-T", "0.9", "--typing-tail-s", "2"], check=True)
    a = json.loads(out.read_text())
    assert a["params"]["scene_T"] == 0.9 and a["params"]["typing_tail_s"] == 2.0
    # with scene_T absurdly high the click gaps are no longer 'scene' active
    assert all(g["features"]["reason"] != "scene" for g in a["gaps"])


def test_find_gaps_edges():
    sys.path.insert(0, str(ROOT / "helpers" / "media"))
    from detect_screen_activity import find_gaps
    assert find_gaps([], 5.0, 1.0) == [(0.0, 5.0)]
    assert find_gaps([(2.0, 3.0), (3.5, 4.0), (6.0, 7.0)], 7.5, 1.0) == [(0.0, 2.0), (4.0, 6.0)]
