"""Composite mode of render_tutorial.py + layout_preview.py (CONTRACTS "Rio 2.0: two-camera")."""
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RENDER = ROOT / "helpers" / "media" / "render_tutorial.py"
PREVIEW = ROOT / "helpers" / "media" / "layout_preview.py"

import composite  # noqa: E402
import render_tutorial as rt  # noqa: E402
import sync_cam  # noqa: E402

LAYOUT = {"canvas": [640, 360], "fps": None, "background": None, "background_color": "#2050a0",
          "screen_rect": [200, 20, 420, 262], "screen_radius": 8,
          "cam_crop": [400, 0, 400, 720], "cam_rect": [10, 100, 120, 180], "cam_radius": 16,
          "encoder": "hevc_videotoolbox", "bitrate": "4M", "max_sync_residual_ms": 20}
# cam fixture covers screen [0, 45]; the last range is outside it
RANGES = [{"source": "screen", "start": 2.0, "end": 3.5},
          {"source": "screen", "start": 10.0, "end": 11.0},
          {"source": "screen", "start": 50.0, "end": 51.2}]


def _profile(tmp: Path, **over) -> Path:
    p = tmp / "profile.json"
    p.write_text(json.dumps({"render": {"voice_filter": ""}, "layout": {**LAYOUT, **over}}))
    return p


def _run_dir(tmp: Path, cam_paths, sync_ok: bool = True) -> Path:
    run = tmp / "run dir"
    edit = run / "edit"
    edit.mkdir(parents=True)
    (run / "screen.mp4").symlink_to(cam_paths["screen"])
    (run / "deskcam.mov").symlink_to(cam_paths["cam"])
    (edit / "sources.json").write_text(json.dumps(
        {"order": ["screen"], "files": {"screen": "screen.mp4"}, "cams": {"deskcam": "deskcam.mov"}}))
    res = sync_cam.sync([run / "deskcam.mov"], [("screen", run / "screen.mp4")])
    assert res["ok"]
    if not sync_ok:
        res["ok"] = False
    (edit / "cam_sync.json").write_text(json.dumps(res))
    (edit / "edl.json").write_text(json.dumps({"version": 1, "sources": {"screen": str(run / "screen.mp4")},
                                               "ranges": RANGES, "grade": "none", "overlays": []}))
    return edit


def _render(edit: Path, profile: Path, *extra) -> subprocess.CompletedProcess:
    p = subprocess.run([sys.executable, str(RENDER), str(edit / "edl.json"), "-o", str(edit / "final.mp4"),
                        "--threads", "2", "--profile", str(profile), *extra], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr[-800:]
    return p


def _probe(path: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets",
                          "-show_entries", "stream=width,height,nb_read_packets,codec_name",
                          "-of", "json", str(path)], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)["streams"][0]


def _pixel(path: Path, t: float, x: int, y: int) -> tuple[int, int, int]:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", str(path), "-vf",
                          f"crop=2:2:{x}:{y},format=rgb24", "-frames:v", "1", "-f", "rawvideo", "-"],
                         capture_output=True, check=True).stdout
    return raw[0], raw[1], raw[2]


@pytest.fixture(scope="module")
def rendered(cam_paths, tmp_path_factory):
    tmp = tmp_path_factory.mktemp("composite")
    edit = _run_dir(tmp, cam_paths)
    prof = _profile(tmp)
    p = _render(edit, prof)
    return edit, prof, p


def test_composite_size_frames_cfr(rendered):
    edit, _, p = rendered
    assert "COMPOSITE: 1 camera(s)" in p.stdout
    st = _probe(edit / "final.mp4")
    assert (st["width"], st["height"], st["codec_name"]) == (640, 360, "hevc")
    n = sum(rt.frame_count(r["end"] - r["start"], 30.0) for r in RANGES)
    assert int(st["nb_read_packets"]) == n
    durs = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                           "packet=duration", "-of", "csv=p=0", str(edit / "final.mp4")],
                          capture_output=True, text=True, check=True).stdout.split()
    assert len(set(durs)) == 1, set(durs)            # constant frame rate
    assert not (edit / "_composite").exists()        # per-render assets cleaned up


def test_camera_present_then_gap_noted(rendered):
    edit, _, _ = rendered
    # cam fixture is solid red with a moving white box at x 40..160 (cropped away by cam_crop)
    r, g, b = _pixel(edit / "final.mp4", 0.7, 70, 190)
    assert r > 150 and g < 90 and b < 90, (r, g, b)
    # third range (out time 2.5 s + 0.6) is outside covers: background colour there
    r, g, b = _pixel(edit / "final.mp4", 3.1, 70, 190)
    assert b > 120 and r < 80, (r, g, b)
    # rounded corner: the very corner pixel of the cam rect is background, not red
    r, g, b = _pixel(edit / "final.mp4", 0.7, 10, 100)
    assert b > 100 and r < 100, (r, g, b)
    notes = json.loads((edit / "composite_notes.json").read_text())
    assert notes["composite"] is True and len(notes["no_camera"]) == 1
    gap = notes["no_camera"][0]
    assert gap["index"] == 2 and gap["start"] == 50.0 and "covers" in gap["reason"]
    assert abs(gap["out_start"] - 2.5) < 0.01


def test_cache_reuse_and_key_changes_with_layout(rendered, tmp_path):
    edit, prof, _ = rendered
    p = _render(edit, prof)
    assert "cache: 3 reused, 0 extracted" in p.stdout
    prof2 = _profile(tmp_path, cam_radius=24)
    p = _render(edit, prof2)
    assert "cache: 0 reused, 3 extracted" in p.stdout


def test_key_extra_changes_with_layout_and_sync(cam_paths):
    cam = {"path": Path(cam_paths["cam"]), "start": 5.2, "drift": 5e-5, "hdr": None}
    lay = composite.load_layout(None) or LAYOUT
    base = composite.key_extra(lay, cam)
    assert composite.key_extra({**lay, "cam_radius": 2}, cam) != base
    assert composite.key_extra(lay, {**cam, "start": 5.3}) != base
    assert composite.key_extra(lay, None) != base


def test_screen_only_key_unchanged(fixture_paths):
    src = Path(fixture_paths["video"])
    st = src.stat()
    parts = [str(src.resolve()), str(st.st_size), str(st.st_mtime_ns), "1.000", "30", "30",
             "1280x720", "", rt.CRF, rt.PRESET, repr(rt.FADE_S), str(rt.SR), str(rt.CACHE_VERSION)]
    old = hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:20]
    assert rt.segment_key(src, 1.0, 30, "30", (1280, 720), "") == old


def test_not_ok_sync_renders_screen_only_unchanged(cam_paths, tmp_path):
    a = _run_dir(tmp_path / "a", cam_paths, sync_ok=False)
    b = _run_dir(tmp_path / "b", cam_paths)
    (b / "cam_sync.json").unlink()
    prof = _profile(tmp_path)
    pa, pb = _render(a, prof), _render(b, prof)
    assert "COMPOSITE" not in pa.stdout and "COMPOSITE" not in pb.stdout
    st = _probe(a / "final.mp4")
    assert (st["width"], st["height"], st["codec_name"]) == (640, 400, "h264")
    keys = lambda e: sorted(json.loads((e / "segments" / "manifest.json").read_text())["renders"][-1]["keys"])
    assert keys(a) == keys(b)
    md5 = lambda e: subprocess.run(["ffmpeg", "-v", "error", "-i", str(e / "final.mp4"), "-map", "0:v",
                                    "-f", "md5", "-"], capture_output=True, text=True).stdout
    assert md5(a) == md5(b)
    assert not (a / "composite_notes.json").exists()


def test_layout_preview_writes_pngs(cam_paths, tmp_path):
    edit = _run_dir(tmp_path, cam_paths)
    prof = _profile(tmp_path)
    out = edit / "layout"
    p = subprocess.run([sys.executable, str(PREVIEW), "--edit-dir", str(edit), "--at", "5", "--at", "20",
                        "-o", str(out), "--profile", str(prof)], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    pngs = sorted(out.glob("layout_*.png"))
    assert len(pngs) == 2 and not (out / "_assets").exists()
    st = _probe(pngs[0])
    assert (st["width"], st["height"]) == (640, 360)
    r, g, b = _pixel(pngs[0], 0, 70, 190)
    assert r > 150 and g < 90, (r, g, b)              # the camera is in the still


def test_hdr_lut_is_monotonic_and_maps_reference_white(tmp_path):
    import numpy as np
    p = composite.make_hdr_lut("hlg", tmp_path / "h.cube", size=9)
    rows = [l for l in p.read_text().splitlines() if l and l[0].isdigit()]
    vals = np.array([[float(x) for x in r.split()] for r in rows]).reshape(9, 9, 9, 3)
    grey = np.array([vals[i, i, i, 0] for i in range(9)])
    assert np.all(np.diff(grey) >= 0) and grey[0] == 0.0 and grey[-1] <= 1.0
    assert 0.85 < float(np.interp(0.75, np.linspace(0, 1, 9), grey)) < 1.0   # HLG 75% ~ SDR white


# ------------------------------------------------------------ Rio 2.0 layouts + inserts
def test_render_face_split_and_insert(cam_paths, tmp_path):
    edit = _run_dir(tmp_path, cam_paths)
    run = edit.parent
    ins = run / "testimonial_x.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=s=320x240:r=25:d=4",
                    "-f", "lavfi", "-i", "sine=f=440:d=4", "-pix_fmt", "yuv420p", "-shortest", str(ins)],
                   check=True)
    ranges = [{"source": "screen", "start": 2.0, "end": 3.5, "layout": "face"},
              {"source": "testimonial_x", "start": 1.0, "end": 2.0, "insert": True},
              {"source": "screen", "start": 10.0, "end": 11.0},
              {"source": "screen", "start": 50.0, "end": 51.2, "layout": "face"}]   # no camera -> split fallback
    (edit / "edl.json").write_text(json.dumps(
        {"version": 1, "sources": {"screen": str(run / "screen.mp4"), "testimonial_x": str(ins)},
         "inserts": ["testimonial_x"], "ranges": ranges, "grade": "none", "overlays": []}))
    p = _render(edit, _profile(tmp_path))
    assert "(face)" in p.stdout and "(insert, full)" in p.stdout
    st = _probe(edit / "final.mp4")
    assert (st["width"], st["height"]) == (640, 360)
    notes = json.loads((edit / "composite_notes.json").read_text())
    gaps = notes["no_camera"]
    assert len(gaps) == 1 and gaps[0]["start"] == 50.0 and gaps[0]["reason"].startswith("face:")
    assert (edit / "insert_gain.json").exists()
