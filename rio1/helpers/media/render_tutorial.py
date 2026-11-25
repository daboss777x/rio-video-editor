"""Render a tutorial EDL (see docs/CONTRACTS.md).

Usage:
    .venv/bin/python rio1/helpers/media/render_tutorial.py E/edl.json -o E/final.mp4
        [--threads N] [--keep-intermediates] [--no-cache] [--profile P]

Output (user, 2026-11-19): the profile's render.output, by default always 16:9 4K
(3840x2160) HEVC (hevc_videotoolbox, constant quality 85, tagged hvc1) at the FIRST source's
(in edl["sources"] order) avg fps. A smaller recording (e.g. 1080p from OBS) is scaled up
(lanczos); one that isn't 16:9 is fitted with black bars, never stretched. render.output.size
null = the old behaviour: the first source's size, libx264 CRF 20. Audio is always 48 kHz stereo.

Blurs: E/blurs.json (blur_find.py or hand-written; docs/CONTRACTS.md "Blurs") rects are blurred
for their time windows at the source's own size, before the scale. An unknown `source` stops the
render (blur.check_blur_sources).

Pipeline (segments are extracted one at a time to keep RAM bounded). Drift-free by
construction: every range is snapped to n = round(dur*fps) frames, dur_exact = n/fps,
and its audio is cut to exactly round(dur_exact*48000) samples.
  1. Per range, video-only: `-ss` before `-i`, blurs, scale to the output size, the output encoder,
     CFR at the first source's avg fps, exactly n frames  ->  E/clips_tutorial/v_NNN.mp4
  2. Per range, audio only: PCM s16le 48 kHz stereo (mono upmixed), sample-exact,
     10 ms fade in/out (joins keep as little as 15 ms of air)  ->  E/clips_tutorial/a_NNN.wav
  3. concat demuxer `-c copy` for video and for PCM at once -> E/base_v.mp4 + E/base_a.wav (no AAC yet,
     so no per-segment priming/rounding can accumulate)
  4. voice filter, then two-pass loudnorm -14 LUFS (video-use measure_loudness + constants) + a
     peak limiter, padded/trimmed to the exact sample count (loudnorm adds ~60 ms of tail), AAC
     encoded alone and MEASURED: up to 4 tries steer the gain to -14 +/- 0.5 LUFS and the limiter's
     ceiling until the AAC true peak is under -1.3 dBTP; then the video is muxed once, +faststart

Segment cache (docs/CONTRACTS.md "Revise loop"): steps 1-2 write each range to
E/segments/<key>.mp4 + <key>.wav, where key hashes everything that decides the segment's
bytes (source path/size/mtime, start, n frames, fps, output size, filters, CRF/PRESET/FADE_S,
CACHE_VERSION). A later render reuses valid pairs and extracts only what's missing; concat and
loudnorm always run over the whole edit. E/segments/manifest.json keeps the last 2 renders'
keys; every other file in E/segments/ is deleted. --no-cache = old behaviour (E/clips_tutorial/,
deleted afterwards).

measure_loudness, LOUDNORM_*, probe_source_fps, resolve_path and the
HDR helpers are imported from vendor/video-use/helpers/render.py, not copied.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from fractions import Fraction
from datetime import datetime, timezone
from pathlib import Path

CODE = Path(__file__).resolve().parents[2]  # this system's code root (rio1/ or rio2/): profiles/
PROJECT = CODE.parent  # shared project root: vendor/
VU_HELPERS = PROJECT / "vendor" / "video-use" / "helpers"
sys.path.insert(0, str(VU_HELPERS))
import render as _vu  # noqa: E402  (video-use)
sys.path.insert(0, str(Path(__file__).resolve().parent))
import blur as _blur  # noqa: E402
from render import (  # noqa: E402
    TONEMAP_CHAIN,
    is_hdr_source,
    probe_source_fps,
    resolve_path,
)

FADE_S = 0.010          # joins may leave only ~15 ms of air; 30 ms would eat word tails
SR = 48000
# loudnorm target ceiling. video-use's -1.0 overshoots by ~0.1-0.2 dB after AAC encoding;
# -1.5 leaves margin so the final measures <= -1.0 dBTP. Do not edit vendor/.
LOUDNORM_TP = -1.5
CRF, PRESET = "20", "fast"
# Bump whenever extract_video/extract_audio change what they write, so old cached
# segments are never reused with new code.
CACHE_VERSION = 1
# render.output when the profile has none: always 16:9 4K HEVC (user, 2026-11-19)
OUTPUT_DEFAULT = {"size": [3840, 2160], "encoder": "hevc_videotoolbox", "quality": 85, "prio_speed": True}
KEEP_RENDERS = 2        # manifest keeps this many renders' keys (current + previous version)
MANIFEST = "manifest.json"


def has_audio(path: Path) -> bool:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0",
                          "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True)
    return bool(out.stdout.strip())


def _run(cmd: list[str], what: str) -> None:
    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"{what} failed: {proc.stderr.strip()[-300:]}")


def frame_count(duration: float, fps: float) -> int:
    return max(1, round(duration * fps))


def video_size(path: Path) -> tuple[int, int]:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                          "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True)
    w, h = out.stdout.strip().split(",")[:2]
    return int(w), int(h)


def fit_filter(size: tuple[int, int], target: tuple[int, int]) -> str:
    """Scale to fit inside target and pad (letterbox); keeps aspect, never stretches. Lanczos:
    the sharpest plain scaler for text when a 1080p recording goes up to 4K."""
    tw, th = target
    return (f"scale={tw}:{th}:force_original_aspect_ratio=decrease:force_divisible_by=2:flags=lanczos,"
            f"pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2,setsar=1")


def output_settings(profile: Path | None = None) -> dict:
    """The profile's render.output ({"size": [w, h] | None, "encoder", "quality"}), else OUTPUT_DEFAULT."""
    profile = profile or CODE / "profiles" / "tutorial.json"
    try:
        out = (json.loads(profile.read_text()).get("render") or {}).get("output", OUTPUT_DEFAULT)
    except (OSError, ValueError):
        out = OUTPUT_DEFAULT
    out = dict(out or {"size": None})
    out.setdefault("size", None)
    return out


def encoder_args(out: dict) -> list[str]:
    """ffmpeg video encoder flags. Size None (old behaviour) = libx264 CRF 20, no B-frames.
    VideoToolbox: constant quality (-q:v) on Apple Silicon, tagged hvc1 for HEVC."""
    enc = str(out.get("encoder") or "libx264") if out.get("size") else "libx264"
    if enc.endswith("_videotoolbox"):
        q = out.get("quality")
        args = ["-c:v", enc] + (["-q:v", str(int(q))] if q else ["-b:v", str(out.get("bitrate") or "40M")])
        args += ["-allow_sw", "1"] + (["-tag:v", "hvc1"] if enc.startswith("hevc") else [])
        if out.get("prio_speed"):    # 1.86x faster 4K60 on the M3, same picture (SSIM 0.9997)
            args += ["-prio_speed", "1"]
    else:
        args = ["-c:v", enc, "-preset", PRESET, "-crf", CRF, "-bf", "0"]
        if enc == "libx265":
            args += ["-tag:v", "hvc1"]
    return args + ["-pix_fmt", "yuv420p", "-colorspace", "bt709", "-color_primaries", "bt709",
                   "-color_trc", "bt709", "-color_range", "tv"]


def extract_video(source: Path, start: float, n: int, out_path: Path,
                  rate: str, threads: int | None, hdr: bool,
                  fit: str | None = None, enc: list[str] | None = None,
                  blurs: list[dict] | None = None) -> None:
    cmd = ["ffmpeg", "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
           "-ss", f"{start:.3f}", "-i", str(source), "-an"]
    pre = TONEMAP_CHAIN if hdr else None
    if blurs:
        # blur at the source's own size (rects are source pixels), then scale
        g = [f"[0:v:0]{pre or 'null'}[bin]"] + _blur.blur_graph("bin", "bout", blurs) + [f"[bout]{fit or 'null'}[v]"]
        cmd += ["-filter_complex", ";".join(g), "-map", "[v]"]
    else:
        cmd += ["-map", "0:v:0"]
        vf = ",".join(f for f in (pre, fit) if f)
        if vf:
            cmd += ["-vf", vf]
    cmd += (enc or encoder_args({"size": None})) + ["-r", rate, "-fps_mode", "cfr", "-frames:v", str(n)]
    if threads:
        cmd += ["-threads", str(threads)]
    _run(cmd + [str(out_path)], f"video {out_path.name}")


def extract_audio(source: Path, start: float, dur_exact: float, out_path: Path) -> None:
    ns = round(dur_exact * SR)
    fade = min(FADE_S, dur_exact / 2)
    af = (f"aresample={SR},apad=whole_len={ns},atrim=end_sample={ns},"
          f"afade=t=in:st=0:d={fade:.4f},afade=t=out:st={dur_exact - fade:.4f}:d={fade:.4f}")
    cmd = ["ffmpeg", "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
           "-ss", f"{start:.3f}", "-i", str(source), "-map", "0:a:0", "-vn",
           "-af", af, "-c:a", "pcm_s16le", "-ar", str(SR), "-ac", "2", str(out_path)]
    _run(cmd, f"audio {out_path.name}")


def segment_key(source: Path, start: float, n: int, rate: str, out_size: tuple[int, int],
                vf: str, extra: str = "") -> str:
    st = source.stat()
    parts = [str(source.resolve()), str(st.st_size), str(st.st_mtime_ns), f"{start:.3f}", str(n),
             rate, f"{out_size[0]}x{out_size[1]}", vf, CRF, PRESET, repr(FADE_S), str(SR),
             str(CACHE_VERSION)]
    if extra:                        # encoder + blurs; empty = the old keys exactly
        parts.append(extra)
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:20]


def _video_packets(path: Path) -> int | None:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets",
                          "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True)
    try:
        return int(out.stdout.strip().split(",")[0])
    except ValueError:
        return None


def segment_valid(v: Path, a: Path, n: int, ns: int) -> bool:
    """Cheap check: the mp4 parses and has exactly n video packets (1 packet per frame, no
    B-frames); the wav is at least the PCM payload (ns * 2 ch * 2 bytes) plus a header and
    not absurdly bigger. Files only ever appear under their final name via rename, so a
    crash leaves a temp file, never a half-written <key>.mp4/.wav."""
    try:
        asz = a.stat().st_size
        if v.stat().st_size <= 0:
            return False
    except OSError:
        return False
    if not (ns * 4 + 44 <= asz <= ns * 4 + 4096):
        return False
    return _video_packets(v) == n


def update_manifest(seg_dir: Path, edl_path: Path, keys: list[str]) -> set[str]:
    """Append this render, keep the last KEEP_RENDERS, delete every other file in seg_dir."""
    mpath = seg_dir / MANIFEST
    try:
        renders = json.loads(mpath.read_text()).get("renders") or []
        if not isinstance(renders, list):
            renders = []
    except (OSError, ValueError, AttributeError):
        renders = []
    renders.append({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "edl": str(edl_path), "keys": list(dict.fromkeys(keys))})
    renders = renders[-KEEP_RENDERS:]
    keep = {k for r in renders for k in (r.get("keys") or [])}
    tmp = seg_dir / (MANIFEST + ".tmp")
    tmp.write_text(json.dumps({"renders": renders}, indent=1))
    os.replace(tmp, mpath)
    for f in seg_dir.iterdir():
        if f.name == MANIFEST or not f.is_file():
            continue
        if f.suffix in (".mp4", ".wav") and f.stem in keep:
            continue
        f.unlink(missing_ok=True)
    return keep


def concat_copy(paths: list[Path], out_path: Path, edit_dir: Path) -> None:
    """Concat demuxer with -c copy (video mp4s or PCM wavs)."""
    lst = edit_dir / f"_concat_{out_path.stem}.txt"
    lst.write_text("".join("file '{}'\n".format(str(p.resolve()).replace("'", "'\\''"))
                           for p in paths))
    cmd = ["ffmpeg", "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
           "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(out_path)]
    try:
        _run(cmd, f"concat {out_path.name}")
    finally:
        lst.unlink(missing_ok=True)


def voice_filter(profile: Path | None = None) -> str:
    """The profile's render.voice_filter (an ffmpeg -af chain), or "" when unset/disabled.
    User, 2026-11-03: MacBook mic in a boomy room -> cut rumble and mud, lift clarity."""
    profile = profile or CODE / "profiles" / "tutorial.json"
    try:
        vf = (json.loads(profile.read_text()).get("render") or {}).get("voice_filter")
    except (OSError, ValueError):
        return ""
    return vf.strip() if isinstance(vf, str) else ""


def _mono_window(path: Path, start: float, dur: float):
    import numpy as np
    proc = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}",
                           "-i", str(path), "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
                          capture_output=True)
    return np.frombuffer(proc.stdout, np.float32).astype(np.float64)


def filter_delay_samples(src: Path, filtered: Path, max_ms: float = 100.0,
                         min_ms: float = 1.0) -> int:
    """How many samples `filtered` lags `src` (cross-correlation over up to 30 s from the middle).
    0 when under min_ms (EQ phase shifts), when the lag is negative, or when there's too little
    sound to measure."""
    import numpy as np
    proc = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                           "-of", "csv=p=0", str(src)], capture_output=True, text=True)
    try:
        dur = float(proc.stdout.strip())
    except ValueError:
        return 0
    win = min(30.0, dur)
    start = max(0.0, (dur - win) / 2)
    a, b = _mono_window(src, start, win), _mono_window(filtered, start, win)
    m = int(SR * max_ms / 1000)
    n = min(len(a), len(b)) - 2 * m
    if n < SR // 2 or float(np.dot(a, a)) < 1e-6:
        return 0
    a = a[m:m + n] - a[m:m + n].mean()
    # correlation over lags -m..m via FFT
    size = 1 << int(np.ceil(np.log2(n + 2 * m + n)))
    corr = np.fft.irfft(np.fft.rfft(b[:n + 2 * m], size) * np.conj(np.fft.rfft(a, size)), size)
    lag = int(np.argmax(corr[:2 * m + 1])) - m
    return lag if lag >= SR * min_ms / 1000 else 0


def final_encode(base_v: Path, base_a: Path, out_path: Path, total_samples: int,
                 voice_af: str | None = None) -> None:
    """Voice cleanup (profile render.voice_filter, whole edit), then two-pass loudnorm
    (video-use measure_loudness + constants) measured on the cleaned audio, AAC encoded once,
    then padded/trimmed to the exact sample count (loudnorm adds ~60 ms of tail)."""
    voice_af = voice_filter() if voice_af is None else voice_af
    cleaned = None
    if voice_af:
        cleaned = base_a.with_name("base_a_voice.wav")
        print(f"  voice filter: {voice_af}")
        _run(["ffmpeg", "-y", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(base_a),
              "-af", voice_af, "-c:a", "pcm_s16le", "-ar", str(SR), str(cleaned)], "voice filter")
        k = filter_delay_samples(base_a, cleaned)
        if k:
            # afftdn delays its output (~25 ms): pad the input's tail by the same amount, run the
            # chain again and drop the first k samples, so speech lines up with the picture and
            # nothing at the end is lost (user, 2026-11-05).
            print(f"  voice filter delay: {k * 1000 / SR:.2f} ms, compensated")
            _run(["ffmpeg", "-y", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(base_a),
                  "-af", f"apad=pad_len={k},{voice_af},atrim=start_sample={k},asetpts=PTS-STARTPTS",
                  "-c:a", "pcm_s16le", "-ar", str(SR), str(cleaned)], "voice filter")
        base_a = cleaned
    try:
        _loudnorm_encode(base_v, base_a, out_path, total_samples)
    finally:
        if cleaned is not None:
            cleaned.unlink(missing_ok=True)


def _loudnorm_encode(base_v: Path, base_a: Path, out_path: Path, total_samples: int) -> None:
    m = _vu.measure_loudness(base_a)
    i, tp, lra = _vu.LOUDNORM_I, _vu.LOUDNORM_TP, _vu.LOUDNORM_LRA
    if m is None:
        ln = f"loudnorm=I={i}:TP={tp}:LRA={lra}"
    else:
        print(f"  loudnorm measured: I={m['input_i']} LUFS TP={m['input_tp']} LRA={m['input_lra']}")
        ln = (f"loudnorm=I={i}:TP={tp}:LRA={lra}:measured_I={m['input_i']}"
              f":measured_TP={m['input_tp']}:measured_LRA={m['input_lra']}"
              f":measured_thresh={m['input_thresh']}:offset={m['target_offset']}:linear=true")
    # Loudness and peaks are steered apart, on the ENCODED audio (HMJ v2/v3, 2026-11-09): a
    # sharp transient can land over TP after loudnorm (v1: +0.70 dBTP) and AAC adds its own
    # overshoot (up to ~1.6 dB), but turning the whole mix down to fix that left v3 at
    # -16.7 LUFS. So each try encodes the audio alone (AAC, seconds) and measures it: too quiet
    # or loud -> the gain moves; peaks over TP - PEAK_MARGIN_DB -> only the limiter's ceiling
    # drops (it shaves the transients, the loudness barely moves). Then the video is muxed once.
    # The limiter runs on loudnorm's 192 kHz output (4x oversampled: sample peaks ~ true peaks);
    # latency=1 keeps sync. PCM first: AAC straight off the loudnorm graph ends ~65 ms long.
    target, ceiling = float(i), float(tp) - PEAK_MARGIN_DB
    norm = base_a.with_name("base_a_norm.wav")
    tries: list[dict] = []

    def attempt(gain: float, limit: float) -> dict:
        vol = f"volume={gain:.2f}dB," if gain else ""
        af = (f"{ln},{vol}{peak_limiter(limit)},aresample={SR},apad=whole_len={total_samples},"
              f"atrim=end_sample={total_samples}")
        _run(["ffmpeg", "-y", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(base_a),
              "-af", af, "-c:a", "pcm_s16le", str(norm)], "loudnorm")
        aac = base_a.with_name(f"base_a_try{len(tries)}.m4a")
        _run(["ffmpeg", "-y", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(norm),
              "-c:a", "aac", "-b:a", "192k", "-ar", str(SR), str(aac)], "aac encode")
        lufs, peak = ebur(aac)
        t = {"path": aac, "gain": gain, "limit": limit, "lufs": lufs, "peak": peak}
        tries.append(t)
        print(f"  audio try {len(tries)}: gain {gain:+.2f} dB, limiter {limit:+.1f} dBFS -> "
              f"{lufs} LUFS, {peak} dBTP")
        return t

    def good_peak(t: dict) -> bool:
        return t["peak"] is not None and t["peak"] <= ceiling

    def good(t: dict) -> bool:
        return good_peak(t) and t["lufs"] is not None and abs(t["lufs"] - target) <= LOUDNESS_TOL_LU

    try:
        gain, limit = 0.0, LIMIT_START_DB
        for _ in range(MAX_AUDIO_TRIES):
            t = attempt(gain, limit)
            if t["lufs"] is None or t["peak"] is None or good(t):
                break
            if abs(t["lufs"] - target) > LOUDNESS_TOL_LU:
                gain += max(-6.0, min(6.0, target - t["lufs"]))
            if not good_peak(t):
                limit = max(LIMIT_FLOOR_DB, limit - (t["peak"] - ceiling) - 0.4)
        ok = [t for t in tries if good_peak(t)]
        if ok:
            best = min(ok, key=lambda t: abs((t["lufs"] if t["lufs"] is not None else -99) - target))
        else:
            # never got under the ceiling: the old fallback, the whole mix down by the excess
            last = tries[-1]
            cut = (last["peak"] - ceiling + 0.1) if last["peak"] is not None else 0.0
            print(f"  peaks still over {ceiling:+.2f} dBTP: turning the mix down {cut:.2f} dB")
            best = attempt(last["gain"] - cut, last["limit"])
        if best["lufs"] is not None and abs(best["lufs"] - target) > LOUDNESS_TOL_LU:
            print(f"  WARNING: loudness {best['lufs']} LUFS, target {target}")
        _run(["ffmpeg", "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
              "-i", str(base_v), "-i", str(best["path"]), "-map", "0:v", "-map", "1:a",
              "-c", "copy", "-movflags", "+faststart", str(out_path)], "final mux")
    finally:
        norm.unlink(missing_ok=True)
        for t in tries:
            t["path"].unlink(missing_ok=True)


LIMIT_START_DB = -3.0      # first try's limiter ceiling (dBFS, on the 192 kHz loudnorm output)
LIMIT_FLOOR_DB = -9.0      # the ceiling never drops below this
LOUDNESS_TOL_LU = 0.5      # integrated loudness within this of LOUDNORM_I counts as on target
MAX_AUDIO_TRIES = 4
PEAK_MARGIN_DB = 0.3       # the AAC audio must land this far under LOUDNORM_TP


def peak_limiter(limit_db: float) -> str:
    return (f"alimiter=limit={10 ** (limit_db / 20):.4f}:attack=1:release=50:level=0"
            f":latency=1")


def ebur(path: Path) -> tuple[float | None, float | None]:
    """(integrated LUFS, true peak dBTP) of a file's first audio stream, by ffmpeg ebur128."""
    proc = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-i", str(path),
                           "-map", "0:a:0", "-af", "ebur128=peak=true", "-f", "null", "-"],
                          capture_output=True, text=True)
    err = proc.stderr
    summary = err[err.rfind("Summary:"):] if "Summary:" in err else err
    m_i = re.search(r"I:\s+(-?[\d.]+)\s+LUFS", summary)
    m_tp = re.search(r"True peak:\s*\n\s*Peak:\s*(-?[\d.]+)", summary)
    return (float(m_i.group(1)) if m_i else None, float(m_tp.group(1)) if m_tp else None)


def true_peak_db(path: Path) -> float | None:
    """True peak (dBTP) of a file's audio, by ffmpeg ebur128."""
    proc = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-i", str(path), "-map", "0:a:0",
                           "-af", "ebur128=peak=true", "-f", "null", "-"],
                          capture_output=True, text=True)
    m = re.search(r"True peak:\s*\n\s*Peak:\s*(-?[\d.]+|-inf)", proc.stderr)
    if not m or m.group(1) == "-inf":
        return None
    return float(m.group(1))


def render(edl_path: Path, out_path: Path, threads: int | None = None,
           keep_intermediates: bool = False, use_cache: bool = True,
           profile: Path | None = None) -> Path:
    edl_path = edl_path.resolve()
    edl = json.loads(edl_path.read_text())
    edit_dir = edl_path.parent
    out_path = out_path.resolve()
    ranges, sources = edl.get("ranges") or [], edl.get("sources") or {}
    if not ranges:
        raise RuntimeError("EDL has no ranges")

    resolved: dict[str, Path] = {}
    # output format = first source in EDL "sources" order (fall back to first range's)
    for name in [next(iter(sources), ranges[0]["source"])] + [r["source"] for r in ranges]:
        if name not in resolved:
            if name not in sources:
                raise RuntimeError(f"range source '{name}' not in EDL sources")
            # local files only: a URL or protocol path here would let ffmpeg reach the network
            if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", str(sources[name])):
                raise RuntimeError(f"source must be a local file, not a URL/protocol: {sources[name]}")
            p = resolve_path(sources[name], edit_dir)
            if not p.is_file():
                raise RuntimeError(f"source not found: {p}")
            if not has_audio(p):
                raise RuntimeError(f"source has no audio stream: {p}")
            resolved[name] = p

    first = resolved[next(iter(sources), ranges[0]["source"])]
    out_cfg = output_settings(profile)
    out_size = tuple(int(v) for v in out_cfg["size"]) if out_cfg.get("size") else video_size(first)
    enc = encoder_args(out_cfg)
    enc_key = "" if not out_cfg.get("size") else " ".join(enc)
    sizes = {n: video_size(p) for n, p in resolved.items()}
    blurs: list[dict] = []
    if (edit_dir / "blurs.json").is_file():
        blurs = json.loads((edit_dir / "blurs.json").read_text()).get("blurs") or []
        blurs = _blur.check_blur_sources(blurs, sources)   # unknown source -> stop, never skip
    blurred = 0
    rate = probe_source_fps(first)
    if not rate:
        raise RuntimeError(f"could not probe frame rate of {first}")
    hdr = {n: is_hdr_source(p) for n, p in resolved.items()}

    clips_dir = edit_dir / ("segments" if use_cache else "clips_tutorial")
    clips_dir.mkdir(parents=True, exist_ok=True)
    keys: list[str] = []
    reused = extracted = 0
    vids: list[Path] = []
    auds: list[Path] = []
    fps = float(Fraction(rate))
    print(f"extracting {len(ranges)} range(s) @ {rate} fps, output {out_size[0]}x{out_size[1]} {enc[1]}")
    total, total_samples = 0.0, 0
    for i, r in enumerate(ranges):
        start, end = float(r["start"]), float(r["end"])
        if end <= start:
            raise RuntimeError(f"range {i} has end <= start ({start}-{end})")
        n = frame_count(end - start, fps)
        dur_exact = n / fps
        total += dur_exact
        total_samples += round(dur_exact * SR)
        src = resolved[r["source"]]
        fit = fit_filter(sizes[r["source"]], out_size) if tuple(sizes[r["source"]]) != tuple(out_size) else None
        bl = _blur.blurs_for(blurs, r["source"], start, end)
        tag = ""
        if bl:
            tag = f" (blur x{len(bl)})"
            blurred += 1
        if use_cache:
            vf = ",".join(f for f in (TONEMAP_CHAIN if hdr[r["source"]] else None, fit) if f)
            extra = enc_key + (" blurs=" + json.dumps(bl, sort_keys=True) if bl else "")
            key = segment_key(src, start, n, rate, out_size, vf, extra)
            keys.append(key)
            v, a = clips_dir / f"{key}.mp4", clips_dir / f"{key}.wav"
            if segment_valid(v, a, n, round(dur_exact * SR)):
                reused += 1
                tag += " (cached)"
            else:
                # write under temp names, rename only when both are complete
                tv, ta = clips_dir / f"{key}.tmp.mp4", clips_dir / f"{key}.tmp.wav"
                try:
                    extract_video(src, start, n, tv, rate, threads, hdr[r["source"]], fit, enc, bl)
                    extract_audio(src, start, dur_exact, ta)
                    os.replace(tv, v)
                    os.replace(ta, a)
                finally:
                    tv.unlink(missing_ok=True)
                    ta.unlink(missing_ok=True)
                extracted += 1
        else:
            v, a = clips_dir / f"v_{i:04d}.mp4", clips_dir / f"a_{i:04d}.wav"
            extract_video(src, start, n, v, rate, threads, hdr[r["source"]], fit, enc, bl)
            extract_audio(src, start, dur_exact, a)
            extracted += 1
        vids.append(v)
        auds.append(a)
        print(f"  [{i:04d}] {start:9.3f}-{end:9.3f} -> {n} frames ({dur_exact:6.3f}s){tag}")
    print(f"cache: {reused} reused, {extracted} extracted")
    if blurs:
        print(f"blurs: {len(blurs)} in E/blurs.json, {blurred} segment(s) blurred")

    base_v, base_a = edit_dir / "base_v.mp4", edit_dir / "base_a.wav"
    concat_copy(vids, base_v, edit_dir)
    concat_copy(auds, base_a, edit_dir)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    _vu.LOUDNORM_TP = LOUDNORM_TP   # module globals are read at call time
    final_encode(base_v, base_a, out_path, total_samples,
                 voice_filter(profile) if profile else None)
    print(f"expected duration {total:.3f}s")

    if use_cache:
        update_manifest(clips_dir, edl_path, keys)
    if not keep_intermediates:
        if not use_cache:
            for p in vids + auds:
                p.unlink(missing_ok=True)
            try:
                clips_dir.rmdir()
            except OSError:
                pass
        base_v.unlink(missing_ok=True)
        base_a.unlink(missing_ok=True)
    return out_path


def main() -> None:
    ap = argparse.ArgumentParser(description="Render a tutorial EDL (4K 16:9 HEVC by default)")
    ap.add_argument("edl", type=Path)
    ap.add_argument("-o", "--output", type=Path, required=True)
    ap.add_argument("--threads", type=int, default=None,
                    help="ffmpeg/x264 threads per segment (lower = less RAM)")
    ap.add_argument("--keep-intermediates", action="store_true",
                    help="keep E/clips_tutorial/ (with --no-cache) and E/base_v.mp4 + base_a.wav")
    ap.add_argument("--no-cache", action="store_true",
                    help="extract every range and keep nothing (ignore/skip E/segments/)")
    ap.add_argument("--profile", type=Path, default=None,
                    help="profile for render.output and render.voice_filter (default: this system's)")
    args = ap.parse_args()
    if not args.edl.exists():
        sys.exit(f"render_tutorial: edl not found: {args.edl}")
    try:
        out = render(args.edl, args.output, args.threads, args.keep_intermediates,
                     use_cache=not args.no_cache, profile=args.profile)
    except (RuntimeError, subprocess.CalledProcessError, KeyError, ValueError) as e:
        sys.exit(f"render_tutorial: {e}")
    print(f"done: {out} ({out.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
