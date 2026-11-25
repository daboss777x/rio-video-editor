"""Rio 2.0 face / full (insert) segments and slide stills, on tiny synthetic media."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "helpers" / "media"))
import composite as C  # noqa: E402
import slides as S  # noqa: E402

LAYOUT = {**C.DEFAULT_LAYOUT, "canvas": [640, 360], "screen_rect": [0, 0, 640, 360],
          "cam_crop": [100, 50, 200, 300], "cam_rect": [10, 10, 120, 180],
          "encoder": "libx264", "background_color": "#000000"}


def ff(*args):
    subprocess.run(["ffmpeg", "-y", "-nostdin", "-hide_banner", "-loglevel", "error", *args], check=True)


def probe(p):
    out = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height,nb_read_frames", "-of", "json", str(p)],
                         capture_output=True, text=True, check=True)
    st = json.loads(out.stdout)["streams"][0]
    return int(st["width"]), int(st["height"]), int(st["nb_read_frames"])


@pytest.fixture
def media(tmp_path):
    cam = tmp_path / "cam.mp4"
    ff("-f", "lavfi", "-i", "testsrc2=s=800x450:r=30:d=3", "-pix_fmt", "yuv420p", str(cam))
    ins = tmp_path / "ins.mp4"
    ff("-f", "lavfi", "-i", "testsrc=s=320x240:r=25:d=3", "-pix_fmt", "yuv420p", str(ins))
    return tmp_path, cam, ins


def test_face_crop_aspect_zoom_and_clamp():
    lay = {**LAYOUT, "face_zoom": 1.3, "face_center": None}
    x, y, w, h = C.face_crop(lay, (3840, 2160))
    assert abs(w / h - 16 / 9) < 0.01 and abs(w - 3840 / 1.3) < 4
    assert 0 <= x <= 3840 - w and 0 <= y <= 2160 - h
    x, y, w, h = C.face_crop({**lay, "face_center": [3800, 2100]}, (3840, 2160))
    assert x + w == 3840 and y + h == 2160          # clamped inside the frame


@pytest.mark.parametrize("mode", ["face", "full"])
def test_full_canvas_segment_exact_frames(media, mode):
    tmp, cam, ins = media
    assets = C.prepare(LAYOUT, tmp / "work", set())
    src, size = (cam, (800, 450)) if mode == "face" else (ins, (320, 240))
    out = tmp / f"{mode}.mp4"
    C.run(C.build_full_cmd(LAYOUT, assets, "30", src, size, 0.5, 45, out, mode), mode)
    assert probe(out) == (640, 360, 45)


def test_full_still(media):
    tmp, cam, ins = media
    assets = C.prepare(LAYOUT, tmp / "work", set())
    out = tmp / "still.png"
    C.run(C.build_full_cmd(LAYOUT, assets, "30", ins, (320, 240), 1.0, 1, out, "full", still=True), "still")
    assert out.is_file()


def test_key_extra_differs_by_mode():
    cam = None
    assert C.key_extra(LAYOUT, cam, "split") != C.key_extra(LAYOUT, cam, "full")
    assert C.key_extra(LAYOUT, cam) == C.key_extra(LAYOUT, cam, "split")


def test_slides_finds_changes(tmp_path):
    v = tmp_path / "deck.mp4"
    ff("-f", "lavfi", "-i", "color=red:s=320x200:r=30:d=4", "-f", "lavfi", "-i", "color=blue:s=320x200:r=30:d=4",
       "-f", "lavfi", "-i", "color=white:s=320x200:r=30:d=4",
       "-filter_complex", "[0][1][2]concat=n=3:v=1:a=0,format=yuv420p", str(v))
    got = S.stills(v, tmp_path / "slides" / "deck", threshold=0.06, min_gap=3.0)
    ts = [g["t"] for g in got]
    assert len(got) == 3 and ts[0] == 0 and abs(ts[1] - 4) <= 0.6 and abs(ts[2] - 8) <= 0.6
    assert all((tmp_path / g["still"]).is_file() for g in got)
