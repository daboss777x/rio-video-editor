import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PROFILE = json.loads((ROOT / "profiles" / "tutorial.json").read_text())


def _pack():
    spec = importlib.util.spec_from_file_location("pack_transcripts", ROOT.parent / "vendor/video-use/helpers/pack_transcripts.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def make_edit(tmp_path, sentences, gaps=(), duration=None, name="clip"):
    """sentences: list of (start, [words]) where each word lasts 0.3s with 0.1s between.
    gaps: activity gap dicts. Returns the edit dir (tmp/run/edit) with transcripts, takes_packed.md, activity.json, probe.json."""
    run = tmp_path / "run"
    E = run / "edit"
    (E / "transcripts").mkdir(parents=True)
    (run / f"{name}.mp4").write_bytes(b"")
    words = []
    for start, toks in sentences:
        t = start
        for tok in toks:
            words.append({"text": tok, "start": round(t, 3), "end": round(t + 0.3, 3), "type": "word", "speaker_id": "speaker_0"})
            t += 0.4
    (E / "transcripts" / f"{name}.json").write_text(json.dumps({"words": words}))
    pk = _pack()
    phrases = pk.group_into_phrases(words, 0.5)
    (E / "takes_packed.md").write_text(pk.render_markdown([(name, 0, phrases)], 0.5))
    dur = duration or (words[-1]["end"] + 1)
    (E / "activity.json").write_text(json.dumps({"source": f"{name}.mp4", "profile": "tutorial", "params": {}, "gaps": list(gaps)}))
    (E / "probe.json").write_text(json.dumps({"path": str(run / f"{name}.mp4"), "duration_s": dur, "width": 1920,
                                              "height": 1080, "avg_fps": "30/1", "r_fps": "30/1", "vfr": False}))
    return E, words


@pytest.fixture
def profile():
    return json.loads(json.dumps(PROFILE))


def make_multi_edit(tmp_path, clips, activity=None):
    """clips: ordered {stem: (sentences, duration_or_None)} (same sentence format as make_edit).
    Writes sources.json, per-clip transcripts, takes_packed.md (in the order given), a multi-clip
    probe.json and, for stems in `activity` ({stem: [gaps]}), E/activity/<stem>.json."""
    run = tmp_path / "run"
    E = run / "edit"
    (E / "transcripts").mkdir(parents=True)
    (E / "activity").mkdir()
    pk = _pack()
    entries, probes, all_words, total = [], {}, {}, 0.0
    for stem, (sentences, duration) in clips.items():
        (run / f"{stem}.mp4").write_bytes(b"")
        words = []
        for start, toks in sentences:
            t = start
            for tok in toks:
                words.append({"text": tok, "start": round(t, 3), "end": round(t + 0.3, 3), "type": "word", "speaker_id": "speaker_0"})
                t += 0.4
        (E / "transcripts" / f"{stem}.json").write_text(json.dumps({"words": words}))
        entries.append((stem, 0, pk.group_into_phrases(words, 0.5)))
        dur = duration or (words[-1]["end"] + 1)
        total += dur
        probes[stem] = {"path": str(run / f"{stem}.mp4"), "duration_s": dur, "width": 1920, "height": 1080,
                        "avg_fps": "30/1", "r_fps": "30/1", "vfr": False}
        all_words[stem] = words
        if activity and stem in activity:
            (E / "activity" / f"{stem}.json").write_text(json.dumps(
                {"source": f"{stem}.mp4", "profile": "tutorial", "params": {}, "gaps": list(activity[stem])}))
    (E / "takes_packed.md").write_text(pk.render_markdown(entries, 0.5))
    order = list(clips)
    (E / "sources.json").write_text(json.dumps({"order": order, "files": {s: f"{s}.mp4" for s in order}, "plan": None}))
    first = probes[order[0]]
    (E / "probe.json").write_text(json.dumps({**first, "duration_s": total, "clips": probes}))
    return E, all_words
