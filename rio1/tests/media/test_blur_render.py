"""Rio 1 (user, 2026-11-19): one OBS video, already layered. Blurs, loudness steering and a 4K
16:9 HEVC output, ported from Rio 2.0 without the camera compositing."""
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "helpers" / "media" / "render_tutorial.py"
sys.path.insert(0, str(ROOT / "helpers" / "media"))
import blur as C  # noqa: E402
import blur_find as B  # noqa: E402

import checks  # noqa: E402
from probe import probe  # noqa: E402


def ff(*args):
    subprocess.run(["ffmpeg", "-y", "-nostdin", "-hide_banner", "-loglevel", "error", *args], check=True)


def gray_at(path, t, size):
    out = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1",
                          "-f", "rawvideo", "-pix_fmt", "gray", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(out, np.uint8).astype(float).reshape(size[1], size[0])


def make_clip(path, size, seconds=3, video="noise"):
    """A clip with detail everywhere (noise) or plain white, plus a tone (loudnorm needs sound)."""
    w, h = size
    src = (f"color=gray:s={w}x{h}:r=30:d={seconds},noise=alls=90:allf=t" if video == "noise"
           else f"color=white:s={w}x{h}:r=30:d={seconds}")
    ff("-f", "lavfi", "-i", src, "-f", "lavfi", "-i", f"sine=f=440:d={seconds}:r=48000",
       "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path))


def render(edit, clip, ranges, blurs=None):
    edit.mkdir(parents=True, exist_ok=True)
    (edit / "edl.json").write_text(json.dumps({"version": 1, "sources": {"clip": str(clip)},
                                               "ranges": ranges, "grade": "none", "overlays": []}))
    if blurs is not None:
        (edit / "blurs.json").write_text(json.dumps({"version": 1, "blurs": blurs}))
    out = edit / "final.mp4"
    proc = subprocess.run([sys.executable, str(SCRIPT), str(edit / "edl.json"), "-o", str(out)],
                          capture_output=True, text=True)
    return out, proc


# ------------------------------------------------------------ 4K 16:9 output
def test_profile_asks_for_4k_hevc_at_quality_85():
    out = json.loads((ROOT / "profiles" / "tutorial.json").read_text())["render"]["output"]
    assert out["size"] == [3840, 2160] and out["encoder"] == "hevc_videotoolbox" and out["quality"] == 85
    assert out["prio_speed"] is True


def test_blur_settings_come_from_rio1s_profile():
    cfg = B.profile_blur()
    assert cfg["sensitive"] is True and cfg["step_s"] == 1.0 and "Red Zone" in cfg["private_folders"]


def test_encoder_speed_switch_and_quality_flags():
    """prio_speed: 1.86x faster 4K60 encodes on the M3 for the same picture (SSIM 0.9997 vs the
    default, same size; 2026-11-19 bench on a 1080p OBS-style clip)."""
    import render_tutorial as R
    args = R.encoder_args({"size": [3840, 2160], "encoder": "hevc_videotoolbox", "quality": 85, "prio_speed": True})
    assert args[args.index("-q:v") + 1] == "85" and args[args.index("-prio_speed") + 1] == "1"
    assert args[args.index("-tag:v") + 1] == "hvc1" and "-b:v" not in args
    assert "-prio_speed" not in R.encoder_args({"size": [3840, 2160], "encoder": "hevc_videotoolbox", "quality": 85})
    old = R.encoder_args({"size": None})
    assert old[old.index("-c:v") + 1] == "libx264" and old[old.index("-crf") + 1] == "20"


def test_a_1080p_recording_comes_out_4k_hevc_at_the_same_frame_rate(tmp_path):
    clip = tmp_path / "obs.mp4"
    make_clip(clip, (1920, 1080), seconds=2)
    out, proc = render(tmp_path / "edit", clip, [{"source": "clip", "start": 0.2, "end": 1.6}])
    assert proc.returncode == 0, proc.stderr[-800:]
    p = probe(out)
    assert (p["width"], p["height"]) == (3840, 2160) and p["avg_fps"] == "30/1"
    codec = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=codec_name,codec_tag_string", "-of", "csv=p=0", str(out)],
                           capture_output=True, text=True).stdout.strip()
    assert codec == "hevc,hvc1"                     # hvc1: plays in QuickTime and uploads fine
    d = checks.av_durations(out)
    assert abs(d["video_s"] - 1.4) < 0.05 and abs(d["video_s"] - d["audio_s"]) < 0.05


def test_a_recording_that_is_not_16x9_gets_bars_never_stretched(tmp_path):
    clip = tmp_path / "square.mp4"
    make_clip(clip, (640, 480), seconds=2, video="white")
    out, proc = render(tmp_path / "edit", clip, [{"source": "clip", "start": 0.2, "end": 1.2}])
    assert proc.returncode == 0, proc.stderr[-800:]
    g = gray_at(out, 0.5, (3840, 2160))
    assert g[:, :200].mean() < 30                    # left bar: black
    assert g[:, -200:].mean() < 30                   # right bar: black
    assert g[:, 600:3240].mean() > 220               # the picture: 4:3 = 2880 px wide, centred


# ------------------------------------------------------------ blurs in the render
def test_a_blur_box_blurs_that_area_for_its_time_only(tmp_path):
    clip = tmp_path / "obs.mp4"
    make_clip(clip, (640, 360), seconds=3)
    blurs = [{"source": "clip", "start": 1.0, "end": 2.0, "rect": [100, 100, 200, 100], "text": "x@y.com"}]
    out, proc = render(tmp_path / "edit", clip, [{"source": "clip", "start": 0.0, "end": 3.0}], blurs)
    assert proc.returncode == 0, proc.stderr[-800:]
    assert "blur x1" in proc.stdout
    inside = (slice(700, 1100), slice(700, 1700))     # the box x6 (640 -> 3840), minus its edge
    outside = (slice(1500, 2000), slice(2400, 3600))
    during, before = gray_at(out, 1.5, (3840, 2160)), gray_at(out, 0.4, (3840, 2160))
    assert during[inside].std() < 0.35 * before[inside].std()          # unreadable while blurred
    assert during[outside].std() > 0.8 * before[outside].std()         # the rest stays sharp


def test_a_file_name_as_blur_source_still_blurs(tmp_path):
    clip = tmp_path / "obs.mp4"
    make_clip(clip, (640, 360), seconds=2)
    blurs = [{"source": str(clip.name), "start": 0.0, "end": 2.0, "rect": [100, 100, 200, 100]}]
    out, proc = render(tmp_path / "edit", clip, [{"source": "clip", "start": 0.0, "end": 2.0}], blurs)
    assert proc.returncode == 0, proc.stderr[-800:]
    assert "blur x1" in proc.stdout


def test_an_unknown_blur_source_stops_the_render(tmp_path):
    clip = tmp_path / "obs.mp4"
    make_clip(clip, (640, 360), seconds=2)
    blurs = [{"source": "someotherclip", "start": 0.0, "end": 1.0, "rect": [0, 0, 50, 50]}]
    out, proc = render(tmp_path / "edit", clip, [{"source": "clip", "start": 0.0, "end": 2.0}], blurs)
    assert proc.returncode != 0 and "someotherclip" in proc.stderr and not out.exists()


def test_blurs_for_windows():
    bl = [{"source": "s", "start": 10, "end": 20, "rect": [1, 2, 3, 4]},
          {"source": "o", "start": 10, "end": 20, "rect": [1, 2, 3, 4]}]
    assert C.blurs_for(bl, "s", 15, 30) == [{"a": 0.0, "b": 5.0, "rect": [1, 2, 3, 4]}]
    assert C.blurs_for(bl, "s", 20, 30) == []


def test_blur_labels_checked_against_the_clips_before_render():
    sources = {"mainscreen1": "../mainscreen1.mp4"}
    bl = [{"source": "mainscreen1.mp4", "start": 1, "end": 2, "rect": [0, 0, 9, 9]},
          {"source": "mainscreen1", "start": 3, "end": 4, "rect": [0, 0, 9, 9]}]
    out = C.check_blur_sources(bl, sources)
    assert [b["source"] for b in out] == ["mainscreen1", "mainscreen1"]
    with pytest.raises(ValueError, match="maincam1"):
        C.check_blur_sources([{"source": "maincam1", "start": 1, "end": 2, "rect": [0, 0, 9, 9]}], sources)


def test_blur_find_groups_sightings():
    sz = {"s": (3024, 1964)}
    sight = [("s", "barbara", t, [1400, 1440, 200, 50]) for t in (404.0, 405.0, 406.0)]
    sight += [("s", "barbara", 900.0, [100, 100, 200, 50])]
    out = B.group(sight, 1.0, sz)
    assert len(out) == 2
    first = [b for b in out if b["start"] < 500][0]
    assert first["start"] == 402.5 and first["end"] == 407.5
    x, y, w, h = first["rect"]
    assert x < 1400 and y < 1440 and x + w > 1600 and y + h > 1490


def test_blur_find_bridges_an_ocr_miss():
    sz = {"s": (3024, 1964)}
    box = [1400, 1440, 200, 50]
    out = B.group([("s", "barbara", 405.0, box), ("s", "barbara", 410.0, box),
                   ("s", "barbara", 417.0, box), ("s", "barbara", 430.0, box)], 1.0, sz)
    assert len(out) == 2 and out[0]["start"] == 403.5 and out[0]["end"] == 418.5


def test_blur_find_word_match():
    assert B.matches("Barbara,", {"barbara"}) == "barbara"
    assert B.matches("Barbara's", {"barbara"}) == "barbara"
    assert B.matches("Bar", {"barbara"}) is None


# ------------------------------------------------------------ loudness steering
def test_limiter_in_final_encode():
    import render_tutorial as R
    lim = R.peak_limiter(-3.0)
    assert "alimiter" in lim and "latency=1" in lim and "limit=0.7079" in lim


def test_loudness_steered_to_target_with_peaks_under(tmp_path):
    """Rio 2.0 HMJ v3 regression: peak fixes must not drag the loudness down (v3 was -16.7 LUFS)."""
    import render_tutorial as R
    a = tmp_path / "base_a.wav"
    ff("-f", "lavfi", "-i", "anoisesrc=d=12:c=pink:a=0.3:r=48000",
       "-f", "lavfi", "-i", "aevalsrc='if(lt(mod(t,1),0.002),0.99,0)':d=12:s=48000",
       "-filter_complex", "[0][1]amix=inputs=2:normalize=0,aformat=channel_layouts=stereo",
       "-c:a", "pcm_s16le", str(a))
    v = tmp_path / "base_v.mp4"
    ff("-f", "lavfi", "-i", "color=black:s=64x64:r=30:d=12", "-c:v", "libx264", str(v))
    out = tmp_path / "final.mp4"
    R._loudnorm_encode(v, a, out, 12 * R.SR)
    lo = checks.loudness(out)
    assert abs(lo["integrated_lufs"] - (-14.0)) <= 0.6
    assert lo["true_peak_dbtp"] <= -1.0
    assert not list(tmp_path.glob("base_a_try*.m4a")) and not (tmp_path / "base_a_norm.wav").exists()


def test_true_peak_db(tmp_path):
    import render_tutorial as R
    w = tmp_path / "t.wav"
    ff("-f", "lavfi", "-i", "sine=f=440:d=1", "-af", "volume=-6dB", str(w))
    assert abs(R.true_peak_db(w) - (-24.1)) < 0.5
