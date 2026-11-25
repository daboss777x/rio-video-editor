"""Faster blur scan (2026-11-21): a frame whose screen did not change shares the last reading
(blur_find.changed_frames), and the text reading runs in parallel parts (vision.run_vision jobs)."""
import sys
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "helpers" / "media"))
import blur_find as B  # noqa: E402

FW, FH = 1512, 850


def screen(tmp_path, name, draw=None, seed=0):
    """A grey 'code editor' with lines of dark text-like marks; draw(arr) adds changes."""
    a = np.full((FH, FW), 235, np.uint8)
    rng = np.random.default_rng(seed)
    for y in range(80, 800, 24):
        for x in range(60, 1100, 14):
            if rng.random() < 0.7:
                a[y:y + 11, x:x + 8] = 40
    if draw:
        draw(a)
    p = tmp_path / f"{name}.jpg"
    Image.fromarray(a).save(p, quality=90)
    return p


def test_unchanged_frames_share_the_last_reading_and_new_text_is_read(tmp_path):
    def key(a):              # one short new word typed at the end of a line
        a[800:811, 1200:1260] = 30
    fs = [screen(tmp_path, "a"), screen(tmp_path, "b"), screen(tmp_path, "c", key), screen(tmp_path, "d", key)]
    refs, _ = B.changed_frames(fs)
    assert refs == [0, 0, 2, 2]


def bubble(k):
    def draw(a):             # the OBS camera bubble: a moving face, different every frame
        a[600:840, 1300:1500] = np.random.default_rng(100 + k).integers(0, 255, (240, 200), dtype=np.uint8)
    return draw


def test_a_camera_bubble_moving_every_frame_does_not_force_a_reading(tmp_path):
    fs = [screen(tmp_path, f"f{k}", bubble(k)) for k in range(6)]
    refs, live = B.changed_frames(fs)
    assert refs == [0] * 6
    assert live and all(x >= 1200 and y >= 560 for x, y, _, _ in live)      # only the bubble's corner


def test_parallel_text_reading_gives_the_same_words_as_one_reader(tmp_path):
    import shutil

    import pytest
    from PIL import ImageDraw, ImageFont
    if not (shutil.which("swiftc") or shutil.which("swift")):
        pytest.skip("macOS Vision needs swift")
    import vision
    font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 28)
    fs = []
    for k in range(7):
        im = Image.new("L", (900, 200), 245)
        ImageDraw.Draw(im).text((40, 70), f"SUPABASE_URL frame {k} done", fill=20, font=font)
        fs.append(tmp_path / f"t{k}.jpg")
        im.save(fs[-1], quality=92)
    one = vision.run_vision("text", fs, tmp_path, jobs=1)
    three = vision.run_vision("text", fs, tmp_path, jobs=3)
    assert three == one and len(one) == 7
    assert any("SUPABASE_URL" in str(w[4]) for w in one[str(fs[3])]["text"])


ENV = [[100, 300, 520, 16, "OPENAI_API_KEY=sk-proj-Ab3dE5fG7hJ9kL1mN3pQ5rS7tU9vW1xY3z"]]


def _edit(tmp_path, src):
    clip = tmp_path / "main1.mp4"
    import subprocess
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", src, "-pix_fmt", "yuv420p", str(clip)], check=True)
    E = tmp_path / "edit"
    E.mkdir()
    import json
    (E / "edl.json").write_text(json.dumps({"sources": {"main1": "../main1.mp4"},
                                            "ranges": [{"source": "main1", "start": 0.0, "end": 4.0}]}))
    return E


def test_a_still_screen_is_read_once_and_its_blur_covers_the_whole_shot(tmp_path, monkeypatch):
    E = _edit(tmp_path, "color=c=gray:s=1512x982:r=30:d=4")
    read = []
    def reader(mode, images, work):
        read.extend(images)
        return {str(p): {"text": ENV} for p in images}
    monkeypatch.setattr(B, "run_vision", reader)
    blurs = B.find(E, [], step=0.5, sensitive=True)["blurs"]
    assert len(read) == 1
    assert blurs and min(b["start"] for b in blurs) <= 0.0 and max(b["end"] for b in blurs) >= 3.9


def test_an_always_moving_area_with_text_in_it_is_still_read_every_frame(tmp_path, monkeypatch):
    fs = [screen(tmp_path, f"f{k}", bubble(k)) for k in range(6)]     # e.g. a terminal scrolling keys past
    read = []
    def reader(mode, images, work):
        read.extend(images)
        return {str(p): {"text": [[1320, 700, 150, 14, "tr_dev_9fK2mQ7xL4pZ8wR1"]]} for p in images}
    monkeypatch.setattr(B, "run_vision", reader)
    res = B.read_frames([fs], tmp_path)
    assert sorted(read) == sorted(fs) and set(res) == {str(p) for p in fs}


def test_a_key_blur_stays_while_its_spot_is_unchanged_even_when_the_reading_garbles(tmp_path, monkeypatch):
    # Airtable token IDs (WhatsApp v3): read right once, then garbled, so the blur dropped out
    E = _edit(tmp_path, "color=c=gray:s=1512x982:r=30:d=4,"
                        "drawbox=x=50:y=800:w=300:h=100:color=white:t=fill:enable='gte(t,2)'")
    def reader(mode, images, work):
        out = {}
        for p in images:
            first = p.name == "s_0001.jpg"
            out[str(p)] = {"text": [[1072, 492, 97, 13, "pat8jwg68FKuBPKMg" if first else "pat8jwg6BFKu"]]}
        return out
    monkeypatch.setattr(B, "run_vision", reader)
    blurs = [b for b in B.find(E, [], step=1.0, sensitive=True)["blurs"] if b["rect"][1] < 520]
    assert blurs and min(b["start"] for b in blurs) <= 0.0 and max(b["end"] for b in blurs) >= 3.9
