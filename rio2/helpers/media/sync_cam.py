"""Audio-sync each camera file to the screen clip(s) -> E/cam_sync.json (docs/CONTRACTS.md,
"Rio 2.0: two-camera 4K mode").

Usage:
    .venv/bin/python rio2/helpers/media/sync_cam.py --edit-dir E
    .venv/bin/python rio2/helpers/media/sync_cam.py --screen a.mp4 [--screen b.mp4] --cam c.MOV -o out.json

Mapping written per cam:  cam_t = offset_s + (1 + drift) * screen_t   (both in file time, the
time `ffmpeg -ss` seeks to, so the renderer can use it directly).

Method (deterministic, no network):
  1. Decode the first decodable (AAC/PCM/...) audio stream of each file to 8 kHz mono s16 through ffmpeg (an hour is
     57.6 MB as int16; never float64 at 48 kHz). The stream's start_time minus the container's
     start_time is added, so a camera whose audio starts late is still placed correctly.
  2. Coarse: voice-band (300-3000 Hz) log-energy envelope at 100 Hz, minus a 1 s moving mean,
     z-scored; FFT cross-correlation over every lag whose overlap is at least
     max(min_overlap_s, overlap_frac * the shorter file). Score = Pearson-like correlation
     at the peak. With several screen clips the camera goes to the best-scoring one.
  3. Fine: n_points windows of win_s seconds spread over the overlap. Each screen window is
     matched against the camera's 8 kHz audio within +-search_s of the coarse prediction
     (band-limited normalized cross-correlation, parabolic sub-sample peak). Points whose
     correlation is below min_conf are dropped.
  4. Least-squares line through the points, outliers (> max(outlier_ms, 3 MAD)) removed,
     residual_ms = RMS of what is left. ok = enough points and residual <= max_residual_ms.

Defaults (all CLI flags):
  sr 8000          enough for the 300-3000 Hz voice band; 0.125 ms per sample before
                   interpolation, so sub-ms accuracy needs no upsampling
  env_hz 100       10 ms envelope frames: an hour is 360k values, one ~1M-point FFT
  n_points 12      windows; 3+ confident ones are needed for the line fit
  win_s 20         long enough to contain several words even in a slow passage
  search_s 1.0     covers the coarse step's 10 ms error plus 270 ppm of drift over an hour
                   (phone vs Mac clocks are typically within +-50 ppm)
  min_conf 0.25    band-limited waveform correlation between two different mics in a room is
                   typically 0.3-0.9 on speech; silence or noise windows give < 0.1
  max_residual_ms  from the profile's layout.max_sync_residual_ms (20): just under one frame
                   at 50 fps and well under the ~45 ms where lip-sync errors become visible
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import numpy as np

CODE = Path(__file__).resolve().parents[2]
PROFILE = CODE / "profiles" / "tutorial.json"

DEFAULTS = {
    "sr": 8000, "env_hz": 100, "band": (300.0, 3000.0), "n_points": 12, "win_s": 20.0,
    "search_s": 1.0, "min_conf": 0.25, "min_points": 3, "min_overlap_s": 10.0,
    "overlap_frac": 0.25, "outlier_ms": 5.0, "max_residual_ms": 20.0, "max_drift": 1e-3,
}

VIDEO_EXT = {".mp4", ".mov", ".m4v", ".mkv"}


# ----------------------------------------------------------------------------- decoding

# Codecs ffmpeg can decode that a camera's main track uses. iPhone .MOVs also carry an "apac"
# (spatial audio) track that ffprobe reports as codec "unknown" and ffmpeg cannot decode, so the
# stream is picked by codec, never by position.
DECODABLE_AUDIO = ("aac", "alac", "mp3", "opus", "flac", "vorbis", "ac3", "eac3")


def _decodable(codec: str) -> bool:
    return codec in DECODABLE_AUDIO or codec.startswith("pcm_")


def probe_audio(path: Path) -> dict:
    """Duration, the first decodable audio stream's absolute index, and where it starts in
    file time (s)."""
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                          "format=duration,start_time:stream=index,codec_type,codec_name,start_time,duration",
                          "-of", "json", str(path)], capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"ffprobe failed on {path}: {out.stderr.strip()[-200:]}")
    d = json.loads(out.stdout or "{}")
    fmt = d.get("format") or {}
    f0 = float(fmt.get("start_time") or 0.0)
    aud = [s for s in d.get("streams") or [] if s.get("codec_type") == "audio"]
    if not aud:
        raise RuntimeError(f"no audio stream in {path}")
    ok = [s for s in aud if _decodable(str(s.get("codec_name") or ""))]
    if not ok:
        raise RuntimeError(f"no decodable (AAC/PCM) audio stream in {path}: "
                           + ", ".join(str(s.get("codec_name")) for s in aud))
    st = ok[0]
    a0 = float(st.get("start_time") or f0)
    dur = float(fmt.get("duration") or st.get("duration") or 0.0)
    return {"t0": a0 - f0, "duration": dur, "index": int(st.get("index", 0)),
            "codec": st.get("codec_name")}


def decode_pcm(path: Path, sr: int, index: int | None = None) -> np.ndarray:
    """One audio stream (absolute `index`, default the first) -> mono int16 at sr, streamed
    through a pipe (bounded RAM)."""
    amap = f"0:{index}" if index is not None else "0:a:0"
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(path),
           "-map", amap, "-vn", "-ac", "1", "-ar", str(sr), "-f", "s16le", "-c:a", "pcm_s16le", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    buf = bytearray()
    assert proc.stdout is not None
    while True:
        chunk = proc.stdout.read(1 << 20)
        if not chunk:
            break
        buf += chunk
    err = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
    if proc.wait() != 0:
        raise RuntimeError(f"audio decode failed for {path}: {err.strip()[-200:]}")
    if len(buf) % 2:
        del buf[-1]
    return np.frombuffer(bytes(buf), dtype=np.int16)


def envelope(pcm: np.ndarray, sr: int, hz: int, band: tuple[float, float]) -> np.ndarray:
    """Voice-band log energy per 1/hz frame, minus a 1 s moving mean, z-scored (float32)."""
    hop = sr // hz
    nfr = len(pcm) // hop
    if nfr < 2:
        return np.zeros(max(nfr, 0), dtype=np.float32)
    freqs = np.fft.rfftfreq(hop, 1.0 / sr)
    sel = (freqs >= band[0]) & (freqs <= band[1])
    win = np.hanning(hop).astype(np.float32)
    energy = np.empty(nfr, dtype=np.float64)
    block = 20000                                   # frames per block (~3 min at 100 Hz)
    for b in range(0, nfr, block):
        e = min(nfr, b + block)
        fr = pcm[b * hop:e * hop].astype(np.float32).reshape(e - b, hop) * win
        spec = np.fft.rfft(fr, axis=1)
        energy[b:e] = (np.abs(spec[:, sel]) ** 2).sum(axis=1)
    floor = max(float(np.mean(energy)) * 1e-4, 1e-6)
    env = np.log(energy + floor)
    k = max(1, hz)                                  # 1 s moving mean (removes gain/AGC drift)
    kern = np.ones(k) / k
    env = env - np.convolve(env, kern, mode="same")
    sd = float(env.std())
    env = (env - env.mean()) / (sd if sd > 0 else 1.0)
    return env.astype(np.float32)


# ----------------------------------------------------------------------------- coarse

def coarse_lag(env_s: np.ndarray, env_c: np.ndarray, hz: int, min_overlap_s: float,
               overlap_frac: float) -> tuple[float, float]:
    """Lag L (s, envelope frames / hz) maximizing sum env_s[n]*env_c[n+L] / overlap.
    Returns (lag_s, score); score ~ Pearson correlation over the overlap at the peak."""
    ns, nc = len(env_s), len(env_c)
    if ns < 2 or nc < 2:
        return 0.0, 0.0
    nfft = 1 << int(np.ceil(np.log2(ns + nc)))
    r = np.fft.irfft(np.conj(np.fft.rfft(env_s, nfft)) * np.fft.rfft(env_c, nfft), nfft)
    lags = np.arange(nfft)
    lags = np.where(lags < nfft - ns + 1, lags, lags - nfft)    # [-(ns-1), nc-1]
    valid = (lags > -ns) & (lags < nc)
    # overlap of s[n] and c[n+L] for n in [max(0,-L), min(ns, nc-L))
    ov = np.minimum(ns, nc - lags) - np.maximum(0, -lags)
    need = max(min_overlap_s * hz, overlap_frac * min(ns, nc), 2)
    valid &= ov >= min(need, min(ns, nc))
    if not valid.any():
        return 0.0, 0.0
    score = np.where(valid, r / np.maximum(ov, 1), -np.inf)
    i = int(np.argmax(score))
    return float(lags[i]) / hz, float(score[i])


# ----------------------------------------------------------------------------- fine

def _band_mask(nfft: int, sr: int, band: tuple[float, float]) -> np.ndarray:
    f = np.fft.rfftfreq(nfft, 1.0 / sr)
    return ((f >= band[0]) & (f <= band[1])).astype(np.float64)


def match_window(x: np.ndarray, y: np.ndarray, sr: int, band: tuple[float, float]
                 ) -> tuple[float, float]:
    """Best position k (samples, sub-sample) of x inside y and its normalized correlation."""
    n, m = len(x), len(y)
    if m < n or n < 16:
        return 0.0, 0.0
    nfft = 1 << int(np.ceil(np.log2(n + m)))
    mask = _band_mask(nfft, sr, band)
    X = np.fft.rfft(x.astype(np.float64), nfft) * mask
    Y = np.fft.rfft(y.astype(np.float64), nfft) * mask
    r = np.fft.irfft(np.conj(X) * Y, nfft)[:m - n + 1]
    xf = np.fft.irfft(X, nfft)[:n]
    yf = np.fft.irfft(Y, nfft)[:m]
    ex = float(np.dot(xf, xf))
    c2 = np.concatenate(([0.0], np.cumsum(yf * yf)))
    ey = c2[n:] - c2[:m - n + 1]
    if ex <= 0:
        return 0.0, 0.0
    rho = r / np.sqrt(ex * np.maximum(ey, 1e-12))
    k = int(np.argmax(rho))
    frac = 0.0
    if 0 < k < len(rho) - 1:
        a, b, c = rho[k - 1], rho[k], rho[k + 1]
        den = a - 2 * b + c
        if den < 0:
            frac = float(np.clip(0.5 * (a - c) / den, -0.5, 0.5))
    return k + frac, float(rho[k])


def refine(pcm_s: np.ndarray, t0s: float, pcm_c: np.ndarray, t0c: float, lag: float,
           span: tuple[float, float], p: dict) -> list[list[float]]:
    """[[screen_t, cam_t, conf], ...] for the confident windows."""
    sr, win, search = p["sr"], p["win_s"], p["search_s"]
    a, b = span
    w = min(win, max(0.0, b - a))
    if w < 1.0:
        return []
    n = int(p["n_points"])
    starts = [a + (b - a - w) * (i + 0.5) / n for i in range(n)] if b - a > w else [a]
    pts = []
    for ts in starts:
        i0 = int(round((ts - t0s) * sr))
        x = pcm_s[max(i0, 0):max(i0, 0) + int(w * sr)]
        ts = t0s + max(i0, 0) / sr
        cs = ts + lag - search
        j0 = int(round((cs - t0c) * sr))
        j1 = j0 + len(x) + int(2 * search * sr)
        j0c, j1c = max(j0, 0), min(j1, len(pcm_c))
        y = pcm_c[j0c:j1c]
        if len(y) < len(x) or len(x) < sr:
            continue
        k, rho = match_window(x, y, sr, p["band"])
        if rho < p["min_conf"]:
            continue
        # The match is best at the window's middle (drift stretches the window), so the point
        # is (screen middle, camera middle); using the window start would bias offset_s by
        # drift * win_s / 2 (0.5 ms at 50 ppm).
        half = len(x) / sr / 2
        cam_t = t0c + (j0c + k) / sr + half
        pts.append([round(ts + half, 4), round(cam_t, 6), round(rho, 3)])
    return pts


def fit_line(pts: list[list[float]], p: dict) -> tuple[float, float, float, list] | None:
    """Least-squares cam = a + b*screen with outlier removal -> (a, drift, residual_ms, kept)."""
    kept = list(pts)
    while len(kept) >= p["min_points"]:
        s = np.array([q[0] for q in kept])
        c = np.array([q[1] for q in kept])
        if np.ptp(s) > 0:
            b, a = np.polyfit(s, c, 1)
        else:
            b, a = 1.0, float(np.median(c - s))
        res = c - (a + b * s)
        mad = float(np.median(np.abs(res - np.median(res))))
        lim = max(p["outlier_ms"] / 1000.0, 3 * 1.4826 * mad)
        bad = np.abs(res) > lim
        if not bad.any() or len(kept) - int(bad.sum()) < p["min_points"]:
            rms = float(np.sqrt(np.mean(res ** 2))) * 1000.0
            return float(a), float(b - 1.0), rms, kept
        worst = int(np.argmax(np.abs(res)))          # drop one at a time (robust to masking)
        kept.pop(worst)
    return None


# ----------------------------------------------------------------------------- driver

def sync_one(cam: Path, screens: list[tuple[str, Path]], p: dict) -> tuple[dict, list[str]]:
    notes: list[str] = []
    sr, hz = p["sr"], p["env_hz"]
    cinfo = probe_audio(cam)
    pcm_c = decode_pcm(cam, sr, cinfo["index"])
    env_c = envelope(pcm_c, sr, hz, p["band"])
    best = None                                     # (score, stem, path, lag, info, pcm)
    for stem, spath in screens:
        sinfo = probe_audio(spath)
        pcm_s = decode_pcm(spath, sr, sinfo["index"])
        env_s = envelope(pcm_s, sr, hz, p["band"])
        lag_env, score = coarse_lag(env_s, env_c, hz, p["min_overlap_s"], p["overlap_frac"])
        lag = cinfo["t0"] - sinfo["t0"] + lag_env
        notes.append(f"{cam.name} vs {spath.name}: coarse offset {lag:.3f}s score {score:.3f}")
        if best is None or score > best[0]:
            best = (score, stem, spath, lag, sinfo, pcm_s)
        del pcm_s, env_s
    entry = {"cam": cam.stem, "file": cam.name, "screen": None, "offset_s": None, "drift": None,
             "residual_ms": None, "covers": None, "points": []}
    if best is None:
        notes.append(f"{cam.name}: no screen clip to sync to")
        return entry, notes
    score, stem, spath, lag, sinfo, pcm_s = best
    entry["screen"] = stem
    span = (max(0.0, -lag), min(sinfo["duration"], cinfo["duration"] - lag))
    pts = refine(pcm_s, sinfo["t0"], pcm_c, cinfo["t0"], lag, span, p)
    entry["points"] = pts
    fit = fit_line(pts, p)
    if fit is None:
        notes.append(f"{cam.name}: no confident audio match ({len(pts)} of {p['n_points']} "
                     f"windows over min_conf {p['min_conf']}, need {p['min_points']})")
        return entry, notes
    a, drift, rms, kept = fit
    b = 1.0 + drift
    lo = max(0.0, (0.0 - a) / b)
    hi = min(sinfo["duration"], (cinfo["duration"] - a) / b)
    entry.update({"offset_s": round(a, 6), "drift": float(f"{drift:.4g}"),
                  "residual_ms": round(rms, 2), "covers": [round(lo, 3), round(hi, 3)]})
    if len(kept) < len(pts):
        notes.append(f"{cam.name}: dropped {len(pts) - len(kept)} outlier point(s)")
    if abs(drift) > p["max_drift"]:
        notes.append(f"{cam.name}: drift {drift * 1e6:.0f} ppm is implausible")
        entry["residual_ms"] = max(rms, p["max_residual_ms"] + 1.0)
    return entry, notes


def cam_ok(entry: dict, p: dict) -> bool:
    return (entry.get("residual_ms") is not None and entry.get("covers") is not None
            and entry["residual_ms"] <= p["max_residual_ms"] and abs(entry["drift"]) <= p["max_drift"]
            and entry["covers"][1] > entry["covers"][0])


def sync(cams: list[Path], screens: list[tuple[str, Path]], p: dict | None = None) -> dict:
    p = {**DEFAULTS, **(p or {})}
    out = {"ok": bool(cams), "cams": [], "notes": []}
    if not cams:
        out["notes"].append("no camera files")
    for cam in cams:
        entry, notes = sync_one(cam, screens, p)
        out["cams"].append(entry)
        out["notes"] += notes
        if not cam_ok(entry, p):
            out["ok"] = False
            if entry.get("residual_ms") is not None:
                out["notes"].append(f"{cam.name}: residual {entry['residual_ms']} ms > "
                                    f"{p['max_residual_ms']} ms limit")
    return out


def profile_params(profile: Path = PROFILE) -> dict:
    try:
        lay = json.loads(profile.read_text()).get("layout") or {}
    except (OSError, ValueError):
        lay = {}
    p = {}
    if isinstance(lay.get("max_sync_residual_ms"), (int, float)):
        p["max_residual_ms"] = float(lay["max_sync_residual_ms"])
    return p


def from_edit_dir(edit_dir: Path) -> tuple[list[Path], list[tuple[str, Path]]]:
    run_dir = edit_dir.parent
    src = json.loads((edit_dir / "sources.json").read_text())
    files = src.get("files") or {}
    screens = [(stem, run_dir / files[stem]) for stem in src.get("order") or [] if stem in files]
    cams = [run_dir / f for f in (src.get("cams") or {}).values()]
    for p in [s for _, s in screens] + cams:
        if not p.is_file():
            raise RuntimeError(f"file not found: {p}")
    return cams, screens


def main() -> None:
    ap = argparse.ArgumentParser(description="Audio-sync camera files to the screen clip(s)")
    ap.add_argument("--edit-dir", type=Path, help="E: reads E/sources.json, writes E/cam_sync.json")
    ap.add_argument("--screen", type=Path, action="append", default=[])
    ap.add_argument("--cam", type=Path, action="append", default=[])
    ap.add_argument("-o", "--output", type=Path)
    for k in ("n_points", "min_points"):
        ap.add_argument("--" + k.replace("_", "-"), type=int, default=None)
    for k in ("win_s", "search_s", "min_conf", "max_residual_ms", "min_overlap_s"):
        ap.add_argument("--" + k.replace("_", "-"), type=float, default=None)
    args = ap.parse_args()

    p = profile_params()
    for k in ("n_points", "min_points", "win_s", "search_s", "min_conf", "max_residual_ms",
              "min_overlap_s"):
        if getattr(args, k) is not None:
            p[k] = getattr(args, k)
    try:
        if args.edit_dir:
            cams, screens = from_edit_dir(args.edit_dir)
            out_path = args.output or args.edit_dir / "cam_sync.json"
        else:
            if not args.screen or not args.cam or not args.output:
                ap.error("give --edit-dir, or --screen/--cam/-o")
            cams, screens = args.cam, [(s.stem, s) for s in args.screen]
            out_path = args.output
        if not cams:
            print("sync_cam: no camera files; nothing to do (screen-only render)")
            return
        res = sync(cams, screens, p)
    except (RuntimeError, OSError, ValueError, KeyError) as e:
        sys.exit(f"sync_cam: {e}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(res, indent=1))
    tmp.replace(out_path)
    for c in res["cams"]:
        print(f"{c['cam']} -> {c['screen']}: offset {c['offset_s']} s, drift "
              f"{None if c['drift'] is None else round(c['drift'] * 1e6, 1)} ppm, residual "
              f"{c['residual_ms']} ms, covers {c['covers']}, {len(c['points'])} points")
    print(f"ok: {res['ok']} -> {out_path}")


if __name__ == "__main__":
    main()
