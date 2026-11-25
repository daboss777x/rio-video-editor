"""Premiere Pro export (Final Cut Pro 7 XML): Rio's cut list as an editable sequence on the raw clips."""
import json
import shutil
import subprocess
import sys
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

import premiere_xml as P

HELPERS = Path(__file__).resolve().parents[2] / "helpers" / "media"

CLIPS = {
    "intro": {"path": "/footage/intro.mp4", "duration_s": 30.0, "channels": 2},
    "main1": {"path": "/footage/main 1.mp4", "duration_s": 60.0, "channels": 2},
}


def video_items(xml: str) -> list[dict]:
    root = ET.fromstring(xml)
    track = root.find("./sequence/media/video/track")
    out = []
    for c in track.findall("clipitem"):
        f = c.find("file")
        out.append({k: int(c.findtext(k)) for k in ("start", "end", "in", "out")} | {"file": f.get("id")})
    return out


def test_each_cut_sits_at_its_place_on_the_raw_clip():
    ranges = [{"source": "intro", "start": 1.0, "end": 3.5},
              {"source": "intro", "start": 10.0, "end": 11.0},
              {"source": "main1", "start": 2.0, "end": 4.25}]
    xml = P.build_xml(ranges, CLIPS, fps="60/1", size=(1920, 1080), name="My video")
    assert video_items(xml) == [
        {"start": 0, "end": 150, "in": 60, "out": 210, "file": "file-intro"},
        {"start": 150, "end": 210, "in": 600, "out": 660, "file": "file-intro"},
        {"start": 210, "end": 345, "in": 120, "out": 255, "file": "file-main1"},
    ]
    root = ET.fromstring(xml)
    assert root.findtext("./sequence/name") == "My video"
    assert root.findtext("./sequence/duration") == "345"
    paths = {f.get("id"): f.findtext("pathurl") for f in root.iter("file") if f.findtext("pathurl")}
    assert paths == {"file-intro": "file://localhost/footage/intro.mp4",
                     "file-main1": "file://localhost/footage/main%201.mp4"}


def test_many_odd_length_cuts_never_drift_from_the_video():
    # 40 cuts of 0.505 s = 20.2 s = 1212 frames at 60 fps; each cut starts where the last one ended
    ranges = [{"source": "main1", "start": 1.0 * k, "end": 1.0 * k + 0.505} for k in range(40)]
    items = video_items(P.build_xml(ranges, CLIPS, fps="60/1", size=(1920, 1080), name="x"))
    assert items[-1]["end"] == 1212
    assert all(b["start"] == a["end"] for a, b in zip(items, items[1:]))
    assert all(i["out"] - i["in"] == i["end"] - i["start"] for i in items)


RANGES = [{"source": "intro", "start": 1.0, "end": 3.5},     # plays 0.0-2.5
          {"source": "intro", "start": 10.0, "end": 11.0},   # plays 2.5-3.5
          {"source": "main1", "start": 2.0, "end": 4.25}]    # plays 3.5-5.75


def test_blur_and_verifier_markers_land_at_output_times():
    problems = [{"rank": 2, "output_tc": "0:04.0", "evidence": "repeat of the same line"}]
    blurs = [{"source": "intro", "start": 10.5, "end": 10.9, "text": "email", "rect": [1, 2, 3, 4]},
             {"source": "intro", "start": 10.5, "end": 10.9, "text": "email", "rect": [9, 9, 9, 9]},  # same moment
             {"source": "intro", "start": 5.0, "end": 10.2, "text": "secret"},   # starts in a cut-out gap
             {"source": "intro", "start": 20.0, "end": 25.0, "text": "known"},   # never plays: no marker
             {"source": "main1", "start": 3.0, "end": 3.5, "text": "key_value"}]
    got = [(round(m["in"], 3), round(m["out"], 3), m["name"]) for m in P.markers(RANGES, problems, blurs)]
    assert got == [(2.5, 2.7, "Blur: secret"),
                   (3.0, 3.4, "Blur: email"),
                   (4.0, 4.0, "Ines #2"),
                   (4.5, 5.0, "Blur: key_value")]


def test_sequence_carries_markers_rios_audio_and_the_raw_audio_switched_off():
    marks = [{"in": 2.5, "out": 2.7, "name": "Blur: secret", "comment": "c"},
             {"in": 4.0, "out": 4.0, "name": "Ines #2", "comment": "repeat"}]
    xml = P.build_xml(RANGES, CLIPS, fps="60/1", size=(1920, 1080), name="x", markers=marks,
                      audio={"path": "/out/My video_edited_audio.wav", "duration_s": 5.75})
    seq = ET.fromstring(xml).find("./sequence")
    assert [(m.findtext("name"), m.findtext("in"), m.findtext("out")) for m in seq.findall("marker")] == [
        ("Blur: secret", "150", "162"), ("Ines #2", "240", "-1")]
    tracks = seq.findall("./media/audio/track")
    assert len(tracks) == 4
    rio1, rio2 = (t.find("clipitem") for t in tracks[:2])
    for n, c in ((1, rio1), (2, rio2)):
        assert (c.findtext("start"), c.findtext("end"), c.findtext("enabled")) == ("0", "345", "TRUE")
        assert c.findtext("sourcetrack/trackindex") == str(n)
    assert rio1.find("file").findtext("pathurl") == "file://localhost/out/My%20video_edited_audio.wav"
    for n, t in ((1, tracks[2]), (2, tracks[3])):
        raw = t.findall("clipitem")
        assert [(c.findtext("start"), c.findtext("end"), c.findtext("in"), c.findtext("enabled")) for c in raw] == [
            ("0", "150", "60", "FALSE"), ("150", "210", "600", "FALSE"), ("210", "345", "120", "FALSE")]
        assert {c.findtext("sourcetrack/trackindex") for c in raw} == {str(n)}


def test_ntsc_rates_are_marked_ntsc():
    xml = P.build_xml([{"source": "intro", "start": 0.0, "end": 1.0}], CLIPS, fps="30000/1001",
                      size=(1920, 1080), name="x")
    rate = ET.fromstring(xml).find("./sequence/rate")
    assert (rate.findtext("timebase"), rate.findtext("ntsc")) == ("30", "TRUE")
    assert video_items(xml)[0]["end"] == 30   # 1 s at 29.97 fps rounds to 30 frames


def _duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                          str(path)], capture_output=True, text=True, check=True).stdout
    return float(out)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_export_command_writes_an_xml_on_the_real_clip_and_rios_audio(fixture_paths, tmp_path):
    run = tmp_path / "runs" / "2026-11-25_0900_my-clip"
    E = run / "edit"
    (E / "verify").mkdir(parents=True)
    clip = run / "my clip.mp4"
    shutil.copy(fixture_paths["video"], clip)
    ranges = [{"source": "my clip", "start": 0.5, "end": 2.0}, {"source": "my clip", "start": 4.0, "end": 5.0}]
    (E / "edl.json").write_text(json.dumps({"version": 1, "sources": {"my clip": "../my clip.mp4"},
                                            "ranges": ranges, "total_duration_s": 2.5}))
    subprocess.run([sys.executable, str(HELPERS / "probe.py"), str(clip), "-o", str(E / "probe.json")], check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(clip), "-t", "2.5", "-c:v", "libx264", "-c:a", "aac",
                    str(E / "final.mp4")], check=True)
    (E / "blurs.json").write_text(json.dumps({"blurs": [{"source": "my clip", "start": 4.2, "end": 4.6,
                                                         "text": "email", "rect": [0, 0, 10, 10]}]}))
    (E / "verify" / "verdict.json").write_text(json.dumps({"verdict": "PASS", "problems": [
        {"rank": 1, "output_tc": "0:01.0", "evidence": "check this join"}]}))
    out_dir = tmp_path / "output" / "not made yet"   # the export makes its own folder
    xml_path = out_dir / "My clip_edited.xml"

    p = subprocess.run([sys.executable, str(HELPERS / "premiere_xml.py"), "--edit-dir", str(E), "-o", str(xml_path)],
                       capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    seq = ET.parse(xml_path).getroot().find("./sequence")
    assert seq.findtext("name") == "My clip_edited"
    pathurls = {urllib.parse.unquote(f.findtext("pathurl")).removeprefix("file://localhost")
                for f in seq.iter("file") if f.findtext("pathurl")}
    wav = out_dir / "My clip_edited_audio.wav"
    assert pathurls == {str(clip.resolve()), str(wav.resolve())}
    assert abs(_duration(wav) - _duration(E / "final.mp4")) < 0.05
    assert [m.findtext("name") for m in seq.findall("marker")] == ["Ines #1", "Blur: email"]


def test_names_with_symbols_still_make_valid_xml():
    clips = {"Q&A <live>": {"path": "/f/Q&A <live>.mp4", "duration_s": 10.0, "channels": 2}}
    xml = P.build_xml([{"source": "Q&A <live>", "start": 0.0, "end": 1.0}], clips, fps="30/1",
                      size=(1280, 720), name="Tips & tricks", markers=[{"in": 0.5, "out": 0.5, "name": "Ines #1",
                                                                        "comment": "a < b & c"}])
    seq = ET.fromstring(xml).find("./sequence")
    assert seq.findtext("name") == "Tips & tricks"
    assert seq.find("marker").findtext("comment") == "a < b & c"
    assert seq.find(".//file").findtext("pathurl") == "file://localhost/f/Q%26A%20%3Clive%3E.mp4"


def test_without_rios_audio_the_raw_audio_stays_on():
    xml = P.build_xml(RANGES, CLIPS, fps="60/1", size=(1920, 1080), name="x")
    tracks = ET.fromstring(xml).findall("./sequence/media/audio/track")
    assert len(tracks) == 2
    assert {c.findtext("enabled") for t in tracks for c in t.findall("clipitem")} == {"TRUE"}
