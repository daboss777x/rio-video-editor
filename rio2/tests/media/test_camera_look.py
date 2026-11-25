"""Rio 2.0 camera look (user, 2026-11-09): face tracking, camera tone, screen blurs, peak limiter."""
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "helpers" / "media"))
import blur_find as B  # noqa: E402
import composite as C  # noqa: E402

CFG = dict(C.TRACK_DEFAULTS)


def ff(*args):
    subprocess.run(["ffmpeg", "-y", "-nostdin", "-hide_banner", "-loglevel", "error", *args], check=True)


def gray_frame(src_args, n):
    out = subprocess.run(["ffmpeg", "-v", "error", *src_args, "-vf", f"select='eq(n\\,{n})'",
                          "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(out, np.uint8).astype(float)


# ------------------------------------------------------------ face_path
def test_still_face_gets_a_fixed_crop_on_the_face():
    s = [[t, 1500 + (t % 2) * 20, 900, 600] for t in range(10, 16)]
    p = C.face_path(s, 10.0, 15.0, CFG)
    assert len(p) == 1 and abs(p[0][1] - 1510) <= 10
    assert p[0][2] == round(900 + 0.1 * 600)          # forehead room: y_offset x face height


def test_moving_face_gets_a_pan():
    s = [[t, 2000 - 100 * (t - 10), 900, 600] for t in range(10, 16)]
    p = C.face_path(s, 10.0, 15.0, CFG)
    assert len(p) > 1 and p[0][1] > p[-1][1] and p[0][0] == 0.0


def test_turned_head_shifts_target_toward_the_head():
    front = C.face_path([[10, 1500, 900, 600, 560, 0.0]], 10, 11, CFG)
    left = C.face_path([[10, 1500, 900, 600, 560, -0.8]], 10, 11, CFG)
    assert front[0][1] == 1500 and 1600 < left[0][1] < 1700      # +sin(0.8) x 0.4 x 560 = 161


def test_deadband_keeps_the_previous_cut_still():
    prev = [[0.0, 1500, 960]]
    s = [[t, 1540, 920, 600] for t in range(20, 24)]
    assert C.face_path(s, 20.0, 23.0, CFG, prev) == [[0.0, 1500, 960]]
    far = [[t, 1800, 920, 600] for t in range(20, 24)]
    assert C.face_path(far, 20.0, 23.0, CFG, prev)[0][1] == 1800


def test_no_face_nearby_falls_back_to_default():
    s = [[100.0, 1500, 900, 600]]
    assert C.face_path(s, 10.0, 12.0, CFG) is None
    assert C.face_path(s, 98.0, 99.0, CFG) is not None      # within max_gap_s
    assert C.face_path([], 0, 1, CFG) is None


def test_track_cfg_off_by_default():
    assert C.track_cfg(C.DEFAULT_LAYOUT) is None
    assert C.track_cfg({"face_track": {"still_px": 50}})["still_px"] == 50


def test_fixed_tracked_crop_is_clamped():
    assert C.tracked_crop(1000, 1574, (3840, 2160), [[0, 300, 900]], (1500, 240)) == "crop=1000:1574:0:112"
    assert C.tracked_crop(1000, 1574, (3840, 2160), None, (1500, 240)) == "crop=1000:1574:1500:240"


def test_key_changes_with_track_and_blur(tmp_path):
    cam = tmp_path / "c.mp4"
    cam.write_bytes(b"x")
    base = {"path": cam, "start": 1.0, "drift": 0, "hdr": None}
    k0 = C.key_extra(C.DEFAULT_LAYOUT, base)
    k1 = C.key_extra(C.DEFAULT_LAYOUT, {**base, "track": [[0, 1, 2]]})
    k2 = C.key_extra(C.DEFAULT_LAYOUT, base, blurs=[{"a": 0, "b": 1, "rect": [0, 0, 4, 4]}])
    assert len({k0, k1, k2}) == 3


# ------------------------------------------------------------ ffmpeg: pan + blur really happen
def test_pan_and_blur_render(tmp_path):
    out = tmp_path / "t.mp4"
    path = [[0, 200, 150], [1.0, 500, 150], [2.0, 500, 300]]
    g = ["[0:v]" + C.tracked_crop(200, 120, (800, 450), path, (0, 0)) + "[c]"]
    g += C.blur_graph("c", "out", [{"a": 0.5, "b": 1.5, "rect": [10, 10, 80, 60]}])
    ff("-f", "lavfi", "-i", "cellauto=s=800x450:r=30:rule=30:seed=7,trim=duration=3,format=yuv420p", "-filter_complex", ";".join(g),
       "-map", "[out]", "-pix_fmt", "yuv420p", "-crf", "10", str(out))
    src = ["-f", "lavfi", "-i", "cellauto=s=800x450:r=30:rule=30:seed=7,trim=duration=3,format=yuv420p"]
    a = gray_frame(["-i", str(out)], 75)
    b = gray_frame(src[:-1] + [src[-1] + ",crop=200:120:400:240"], 75)
    assert np.abs(a - b).mean() < 4                       # t=2.5: crop centred on (500, 300)
    # blur only inside its window: the box differs from the sharp crop at t=1, not at t=0.1
    sharp = lambda n: gray_frame(src[:-1] + [src[-1] + f",{C.tracked_crop(200, 120, (800, 450), path, (0, 0))}"], n)
    box = lambda f: f.reshape(120, 200)[10:70, 10:90]
    assert np.abs(box(gray_frame(["-i", str(out)], 3)) - box(sharp(3))).mean() < 4
    assert np.abs(box(gray_frame(["-i", str(out)], 30)) - box(sharp(30))).mean() > 4


# ------------------------------------------------------------ blurs
def test_blurs_for_windows():
    bl = [{"source": "s", "start": 10, "end": 20, "rect": [1, 2, 3, 4]},
          {"source": "o", "start": 10, "end": 20, "rect": [1, 2, 3, 4]}]
    assert C.blurs_for(bl, "s", 15, 30) == [{"a": 0.0, "b": 5.0, "rect": [1, 2, 3, 4]}]
    assert C.blurs_for(bl, "s", 20, 30) == []


def test_blur_find_groups_sightings():
    sz = {"s": (3024, 1964)}
    sight = [("s", "barbara", t, [1400, 1440, 200, 50]) for t in (404.0, 405.0, 406.0)]
    sight += [("s", "barbara", 900.0, [100, 100, 200, 50])]          # elsewhere, much later
    out = B.group(sight, 1.0, sz)
    assert len(out) == 2
    first = [b for b in out if b["start"] < 500][0]
    assert first["start"] == 402.5 and first["end"] == 407.5
    x, y, w, h = first["rect"]
    assert x < 1400 and y < 1440 and x + w > 1600 and y + h > 1490      # padded


def test_blur_find_bridges_an_ocr_miss():
    sz = {"s": (3024, 1964)}
    box = [1400, 1440, 200, 50]
    out = B.group([("s", "barbara", 405.0, box), ("s", "barbara", 410.0, box),
                   ("s", "barbara", 417.0, box), ("s", "barbara", 430.0, box)], 1.0, sz)
    assert len(out) == 2 and out[0]["start"] == 403.5 and out[0]["end"] == 418.5   # 13 s gap: separate


def test_blur_find_word_match():
    assert B.matches("Barbara,", {"barbara"}) == "barbara"
    assert B.matches("Barbara's", {"barbara"}) == "barbara"
    assert B.matches("Bar", {"barbara"}) is None


# ------------------------------------------------------------ tone + limiter
def test_cam_eq_in_both_camera_modes(tmp_path):
    lay = {**C.DEFAULT_LAYOUT, "canvas": [640, 360], "screen_rect": [0, 0, 640, 360],
           "cam_crop": [100, 50, 200, 300], "cam_rect": [10, 10, 120, 180], "cam_eq": "contrast=1.1"}
    assets = {"plate": tmp_path / "p.png", "masks": {}, "luts": {}}
    cam = {"path": tmp_path / "c.mp4", "start": 0.0, "drift": 0, "size": (800, 450), "hdr": None}
    split = " ".join(C.build_cmd(lay, assets, "30", tmp_path / "s.mp4", (640, 360), 0, 10,
                                 tmp_path / "o.mp4", cam))
    face = " ".join(C.build_full_cmd(lay, assets, "30", tmp_path / "c.mp4", (800, 450), 0, 10,
                                     tmp_path / "o.mp4", "face"))
    full = " ".join(C.build_full_cmd(lay, assets, "30", tmp_path / "i.mp4", (800, 450), 0, 10,
                                     tmp_path / "o.mp4", "full"))
    assert "eq=contrast=1.1" in split and "eq=contrast=1.1" in face and "eq=" not in full


def test_profile_look():
    prof = json.loads((Path(__file__).resolve().parents[2] / "profiles" / "tutorial.json").read_text())
    lay = prof["layout"]
    assert lay["cam_eq"] and lay["cam_hdr_gain"] < 1.0 and C.track_cfg(lay) is not None


def test_limiter_in_final_encode():
    import render_tutorial as R
    lim = R.peak_limiter(-3.0)
    assert "alimiter" in lim and "latency=1" in lim and "limit=0.7079" in lim


def _loud_track(path, seconds=12):
    """Speech-like test audio: a loud pink-noise bed with sharp clicks (peaky, so the limiter
    and AAC overshoot matter)."""
    ff("-f", "lavfi", "-i", f"anoisesrc=d={seconds}:c=pink:a=0.3:r=48000",
       "-f", "lavfi", "-i", f"aevalsrc='if(lt(mod(t,1),0.002),0.99,0)':d={seconds}:s=48000",
       "-filter_complex", "[0][1]amix=inputs=2:normalize=0,aformat=channel_layouts=stereo",
       "-c:a", "pcm_s16le", str(path))


def test_loudness_steered_to_target_with_peaks_under(tmp_path):
    """v3 regression: peak fixes must not drag the loudness down (v3 was -16.7 LUFS)."""
    import render_tutorial as R
    import checks
    a = tmp_path / "base_a.wav"
    _loud_track(a)
    v = tmp_path / "base_v.mp4"
    ff("-f", "lavfi", "-i", "color=black:s=64x64:r=30:d=12", "-c:v", "libx264", str(v))
    out = tmp_path / "final.mp4"
    n = 12 * R.SR
    R._loudnorm_encode(v, a, out, n)
    lo = checks.loudness(out)
    assert abs(lo["integrated_lufs"] - (-14.0)) <= 0.6
    assert lo["true_peak_dbtp"] <= -1.0
    assert not list(tmp_path.glob("base_a_try*.m4a")) and not (tmp_path / "base_a_norm.wav").exists()


def test_encoder_quality_replaces_bitrate():
    args = C.encoder_args({"encoder": "hevc_videotoolbox", "bitrate": "40M", "quality": 80})
    assert args[args.index("-q:v") + 1] == "80" and "-b:v" not in args
    args = C.encoder_args({"encoder": "hevc_videotoolbox", "bitrate": "40M", "quality": None})
    assert args[args.index("-b:v") + 1] == "40M" and "-q:v" not in args


def test_true_peak_db(tmp_path):
    import render_tutorial as R
    w = tmp_path / "t.wav"
    ff("-f", "lavfi", "-i", "sine=f=440:d=1", "-af", "volume=-6dB", str(w))
    assert abs(R.true_peak_db(w) - (-24.1)) < 0.5      # lavfi sine is -18 dBFS, then -6 dB


def test_blur_labels_checked_against_the_clips_before_render():
    # 2026-11-17: hand-written blurs said "mainscreen1.mp4", the EDL says "mainscreen1": nothing was blurred.
    sources = {"mainscreen1": "../mainscreen1.mp4"}
    bl = [{"source": "mainscreen1.mp4", "start": 1, "end": 2, "rect": [0, 0, 9, 9]},
          {"source": "mainscreen1", "start": 3, "end": 4, "rect": [0, 0, 9, 9]}]
    out = C.check_blur_sources(bl, sources)
    assert [b["source"] for b in out] == ["mainscreen1", "mainscreen1"]   # file name -> clip name
    with pytest.raises(ValueError, match="maincam1"):
        C.check_blur_sources([{"source": "maincam1", "start": 1, "end": 2, "rect": [0, 0, 9, 9]}], sources)
