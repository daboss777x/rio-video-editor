"""Two-camera 4K composite (docs/CONTRACTS.md, "Rio 2.0: two-camera 4K mode"). Shared by
render_tutorial.py (one segment per kept range) and layout_preview.py (single PNG stills).

One ffmpeg process per segment, three inputs, no frames ever held in Python:
    0  the background plate: the layout background (or background_color) scaled to the canvas
       ONCE per render into a PNG; in the graph it is decoded once, converted to yuv420p once
       and looped (refcounted, so a 4K frame is not re-decoded or re-scaled per output frame)
    1  the screen clip, `-ss start`, fps -> output rate (CFR), aspect-fitted into screen_rect
    2  the camera, `-ss cam(start)`, VideoToolbox decode, timestamps / (1 + drift), fps -> output
       rate, cropped to cam_crop (trimmed to cam_rect's aspect), scaled into cam_rect, HDR
       (HLG/PQ) mapped to SDR BT.709 by a generated 3D LUT on the small scaled frame
Rounded corners are not an alpha mask over the whole screen/camera (a 6 MP per-frame blend):
four r x r patches of the plate, with an anti-aliased quarter-circle alpha, are overlaid on each
rect's corners. The visible result is the same, the per-frame cost is 8 tiny overlays.

Design choices (documented defaults):
  * rects are rounded to even pixels (4:2:0 chroma), radii to even, radius <= min(w, h)/2
  * NATIVE_SNAP 0.01: if the screen would be scaled by less than 1 %, it is placed at its native
    size centred in screen_rect instead (sharper, and no 6 MP lanczos per frame); the user's
    measured rect 3034x1972 vs the 3024x1964 source is exactly this case
  * HDR camera: no zscale in this ffmpeg build (so video-use's TONEMAP_CHAIN cannot run); the
    LUT does BT.2100 HLG (OOTF gamma 1.2, 1000 nit) or PQ -> display light, reference white
    203 nit -> SDR 1.0, BT.2020 -> BT.709 primaries, soft knee from KNEE (0.75) up, inverse
    BT.1886 (1/2.4). layout.cam_hdr_gain (default 1.0) scales the light before the knee.

Per-range modes ("Rio 2.0: layouts and inserts", build_full_cmd):
  * split  the graph above (screen + camera bubble); the default
  * face   the camera alone fills the canvas: a crop of the canvas's aspect, layout.face_zoom
           (1.3) times tighter than the full frame, centred on layout.face_center (camera
           pixels; default the centre of cam_crop), clamped inside the frame, scaled to the canvas
  * full   an insert clip (e.g. a testimonial) fitted to the canvas over the plate colour
           (lanczos; 1080p -> 4K), HDR mapped by the same LUT
Both are one input, no corners: a full-canvas picture has no rounded rect.

Camera look (user, 2026-11-09, "Rio 2.0: camera look and blur" in CONTRACTS):
  * layout.cam_hdr_gain + layout.cam_eq (an ffmpeg eq after the LUT, on camera pixels only:
    split bubble and face mode) set the camera's brightness/contrast; picked by eye from stills
  * layout.face_track on: E/face_track.json (face_track.py) moves the bubble crop and the face
    crop onto the face, per cut (face_path: fixed when still, a smooth pan when the face moves)
  * E/blurs.json (blur_find.py): boxes on the screen blurred for their time windows (blur_graph)
"""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
from fractions import Fraction
from pathlib import Path

import numpy as np

CODE = Path(__file__).resolve().parents[2]
PROJECT = CODE.parent
PROFILE = CODE / "profiles" / "tutorial.json"

COMPOSITE_VERSION = 3     # bump when the graph changes what a composite segment looks like
NATIVE_SNAP = 0.01
KNEE = 0.75
LUT_SIZE = 33
HDR_KINDS = {"arib-std-b67": "hlg", "smpte2084": "pq"}
COVERS_TOL_S = 0.05       # a range may end this far past the camera's last frame (last frame held)

DEFAULT_LAYOUT = {"canvas": [3840, 2160], "fps": None, "background": None,
                  "background_color": "#101418", "screen_rect": None, "screen_radius": 0,
                  "cam_crop": None, "cam_rect": None, "cam_radius": 0,
                  "encoder": "hevc_videotoolbox", "bitrate": "40M", "quality": None,
                  "max_sync_residual_ms": 20,
                  "cam_hdr_gain": 1.0, "face_zoom": 1.3, "face_center": None, "min_face_s": 6.0,
                  "cam_eq": "", "face_track": None}
MODES = ("split", "face", "full")
TRACK_DEFAULTS = {"still_px": 160, "deadband_px": 80, "y_offset": 0.1, "max_gap_s": 3.0,
                  "yaw_shift": 0.4}


# ----------------------------------------------------------------------------- loading

def _even(v: float) -> int:
    return int(round(float(v) / 2.0)) * 2


def load_layout(profile: Path | None = None) -> dict | None:
    """The profile's `layout` block (defaults filled, rects even), or None when there is none."""
    try:
        lay = json.loads(Path(profile or PROFILE).read_text()).get("layout")
    except (OSError, ValueError):
        return None
    if not isinstance(lay, dict):
        return None
    out = {**DEFAULT_LAYOUT, **lay}
    out["canvas"] = [_even(v) for v in out["canvas"]]
    for k in ("screen_rect", "cam_rect", "cam_crop"):
        if out.get(k) is not None:
            out[k] = [_even(v) for v in out[k]]
    if out.get("screen_rect") is None:
        out["screen_rect"] = [0, 0] + out["canvas"]
    return out


def load_cam_sync(edit_dir: Path) -> dict | None:
    """E/cam_sync.json when it exists and says ok: true, else None."""
    try:
        d = json.loads((Path(edit_dir) / "cam_sync.json").read_text())
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) and d.get("ok") is True and d.get("cams") else None


def resolve_cam(entry: dict, edit_dir: Path) -> Path:
    f = Path(str(entry.get("file") or ""))
    for p in ([f] if f.is_absolute() else [Path(edit_dir).parent / f, Path(edit_dir) / f]):
        if p.is_file():
            return p
    raise RuntimeError(f"camera file not found for {entry.get('cam')}: {f}")


def cam_for_range(sync: dict, source: str, start: float, end: float
                  ) -> tuple[dict | None, str]:
    """The cam entry synced to `source` whose covers hold [start, end], or (None, reason)."""
    cands = [c for c in sync.get("cams") or [] if c.get("screen") == source
             and c.get("offset_s") is not None and c.get("covers")]
    if not cands:
        return None, f"no camera synced to {source}"
    for c in cands:
        lo, hi = c["covers"]
        if start >= lo - 1e-6 and end <= hi + COVERS_TOL_S:
            return c, ""
    return None, "outside camera covers " + ", ".join(
        f"[{c['covers'][0]:.2f}, {c['covers'][1]:.2f}]" for c in cands)


def cam_time(entry: dict, screen_t: float) -> float:
    return float(entry["offset_s"]) + (1.0 + float(entry.get("drift") or 0.0)) * screen_t


def probe_video(path: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height,color_transfer:stream_side_data=rotation",
                          "-of", "json", str(path)], capture_output=True, text=True)
    try:
        st = (json.loads(out.stdout).get("streams") or [])[0]
    except (ValueError, IndexError):
        raise RuntimeError(f"ffprobe found no video stream in {path}")
    w, h = int(st["width"]), int(st["height"])
    rot = 0
    for sd in st.get("side_data_list") or []:
        if "rotation" in sd:
            rot = int(sd["rotation"])
    if abs(rot) % 180 == 90:          # ffmpeg autorotates on decode: crops are in display space
        w, h = h, w
    return {"w": w, "h": h, "hdr": HDR_KINDS.get(str(st.get("color_transfer") or ""))}


def probe_duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    try:
        return float(out.stdout.strip())
    except ValueError:
        return 0.0


# ----------------------------------------------------------------------------- geometry

def fit_rect(src: tuple[int, int], rect: list[int]) -> list[int]:
    """Aspect-fit src into rect, centred, even; native size when within NATIVE_SNAP."""
    sw, sh = src
    x, y, w, h = rect
    s = min(w / sw, h / sh)
    if abs(s - 1.0) <= NATIVE_SNAP and sw <= w + 2 * NATIVE_SNAP * w:
        fw, fh = sw - sw % 2, sh - sh % 2
    else:
        fw, fh = _even(sw * s), _even(sh * s)
        fw, fh = min(fw, w), min(fh, h)
    return [x + _even((w - fw) / 2), y + _even((h - fh) / 2), fw, fh]


def cover_crop(crop: list[int] | None, frame: tuple[int, int], rect: list[int]) -> list[int]:
    """cam_crop trimmed centrally to cam_rect's aspect and clamped inside the camera frame."""
    fw, fh = frame
    cx, cy, cw, ch = crop if crop else [0, 0, fw, fh]
    cw, ch = min(cw, fw), min(ch, fh)
    ta = rect[2] / rect[3]
    if cw / ch > ta:
        nw = ch * ta
        cx, cw = cx + (cw - nw) / 2, nw
    else:
        nh = cw / ta
        cy, ch = cy + (ch - nh) / 2, nh
    cw, ch = _even(cw), _even(ch)
    cx = min(max(0, _even(cx)), fw - cw)
    cy = min(max(0, _even(cy)), fh - ch)
    return [cx, cy, cw, ch]


def face_crop(layout: dict, frame: tuple[int, int]) -> list[int]:
    """[x, y, w, h] in camera pixels for face mode: canvas aspect, face_zoom tighter than the
    largest such crop, centred on face_center (default: the centre of cam_crop), inside the frame."""
    fw, fh = frame
    cw, chh = layout["canvas"]
    ta = cw / chh
    w0, h0 = (fh * ta, fh) if fw / fh > ta else (fw, fw / ta)
    z = max(1.0, float(layout.get("face_zoom") or 1.0))
    w, h = _even(w0 / z), _even(h0 / z)
    fc = layout.get("face_center")
    if fc:
        cx, cy = float(fc[0]), float(fc[1])
    elif layout.get("cam_crop"):
        x, y, ww, hh = layout["cam_crop"]
        cx, cy = x + ww / 2, y + hh / 2
    else:
        cx, cy = fw / 2, fh / 2
    x = min(max(0, _even(cx - w / 2)), fw - w)
    y = min(max(0, _even(cy - h / 2)), fh - h)
    return [x, y, w, h]


# ----------------------------------------------------------------------------- face tracking

def track_cfg(layout: dict) -> dict | None:
    """layout.face_track merged over TRACK_DEFAULTS, or None when tracking is off."""
    ft = layout.get("face_track")
    if not ft:
        return None
    return {**TRACK_DEFAULTS, **(ft if isinstance(ft, dict) else {})}


def _med(v: list[float]) -> float:
    v = sorted(v)
    n = len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2


def face_path(samples: list[list[float]], t0: float, t1: float, cfg: dict,
              prev: list[list[float]] | None = None) -> list[list[float]] | None:
    """Where to centre the camera crop over camera time [t0, t1] (face_track.json samples
    [t, cx, cy, h(, w, yaw)], camera pixels). Returns [[rel_t, cx, cy], ...]: one point = a fixed
    crop, several = a smooth pan through them. The target is the HEAD, not the face: the face
    centre moved down by y_offset x face height (forehead room like the hand-tuned crop) and, when
    the speaker turns, sideways by -sin(yaw) x yaw_shift x face width (a turned face sits to one
    side of the head; HMJ v1: turned left = face at the bubble's left edge). A still face (spread under
    still_px) gets the median; a fixed crop within deadband_px of the previous cut's last point
    keeps that point (no twitch between cuts). No face within max_gap_s: None (default crop)."""
    if not samples:
        return None
    inside = [s for s in samples if t0 - 0.5 <= s[0] <= t1 + 0.5]
    if not inside:
        near = min(samples, key=lambda s: min(abs(s[0] - t0), abs(s[0] - t1)))
        if min(abs(near[0] - t0), abs(near[0] - t1)) > cfg["max_gap_s"]:
            return None
        inside = [near]
    pts = [(s[0], s[1] - math.sin(s[5] if len(s) > 5 else 0.0) * cfg["yaw_shift"] * (s[4] if len(s) > 4 else s[3]),
            s[2] + cfg["y_offset"] * s[3]) for s in inside]
    if len(pts) >= 3:                       # median of 3: drops single bad detections
        sm = []
        for i in range(len(pts)):
            w = pts[max(0, i - 1): i + 2]
            sm.append((pts[i][0], _med([p[1] for p in w]), _med([p[2] for p in w])))
        pts = sm
    xs, ys = [p[1] for p in pts], [p[2] for p in pts]
    if max(xs) - min(xs) < cfg["still_px"] and max(ys) - min(ys) < cfg["still_px"]:
        c = [0.0, round(_med(xs)), round(_med(ys))]
        if prev:
            px, py = prev[-1][1], prev[-1][2]
            if abs(px - c[1]) < cfg["deadband_px"] and abs(py - c[2]) < cfg["deadband_px"]:
                c = [0.0, px, py]
        return [c]
    dur = max(t1 - t0, 0.0)
    return [[round(min(max(p[0] - t0, 0.0), dur), 3), round(p[1]), round(p[2])] for p in pts]


def _axis_expr(path: list[list[float]], k: int, size: int, frame: int) -> str:
    """ffmpeg crop x (k=1) or y (k=2) expression: centre on the path, clamped in the frame,
    linear between points, held before the first and after the last."""
    hi = frame - size
    if len(path) == 1:
        return str(min(max(0, _even(path[0][k] - size / 2)), hi))
    e = f"{path[-1][k] - size / 2:.1f}"
    for a, b in reversed(list(zip(path, path[1:]))):
        ta, tb = a[0], b[0]
        if tb <= ta:
            continue
        va, vb = a[k] - size / 2, b[k] - size / 2
        seg = f"{va:.1f}+({vb - va:.1f})*(t-{ta:.3f})/{tb - ta:.3f}"
        e = f"if(lt(t\\,{tb:.3f})\\,{seg}\\,{e})"
    e = f"if(lt(t\\,{path[0][0]:.3f})\\,{path[0][k] - size / 2:.1f}\\,{e})"
    return f"'clip({e}\\,0\\,{hi})'"


def tracked_crop(w: int, h: int, frame: tuple[int, int], path: list[list[float]] | None,
                 default_xy: tuple[int, int]) -> str:
    """crop=w:h:x:y with x/y following `path` (None: the fixed default)."""
    if not path:
        return f"crop={w}:{h}:{default_xy[0]}:{default_xy[1]}"
    return f"crop={w}:{h}:{_axis_expr(path, 1, w, frame[0])}:{_axis_expr(path, 2, h, frame[1])}"


# ----------------------------------------------------------------------------- blur

def blur_graph(inp: str, out: str, blurs: list[dict], scale: float = 1.0, tag: str = "b") -> list[str]:
    """Filter-graph lines blurring each {"a", "b", "rect"} (segment-relative seconds, source
    pixels times `scale`) of stream [inp] into [out]. Empty list when there is nothing to blur."""
    if not blurs:
        return []
    f = [f"[{inp}]split={len(blurs) + 1}[{tag}m0]" + "".join(f"[{tag}s{i}]" for i in range(len(blurs)))]
    last = f"{tag}m0"
    for i, b in enumerate(blurs):
        x, y, w, h = (_even(v * scale) for v in b["rect"])
        w, h = max(w, 4), max(h, 4)
        r = max(2, min(40, min(w, h) // 3))
        f.append(f"[{tag}s{i}]crop={w}:{h}:{x}:{y},boxblur=lr={r}:lp=3:cr={max(1, r // 2)}:cp=3[{tag}b{i}]")
        nxt = out if i == len(blurs) - 1 else f"{tag}m{i + 1}"
        f.append(f"[{last}][{tag}b{i}]overlay=x={x}:y={y}:"
                 f"enable='between(t\\,{b['a']:.3f}\\,{b['b']:.3f})'[{nxt}]")
        last = nxt
    return f


def check_blur_sources(blurs: list[dict], sources: dict) -> list[dict]:
    """E/blurs.json entries with `source` as the EDL's clip name. A clip's file name ("main.mp4")
    is mapped to its name ("main"); any other unknown source raises ValueError, so a blur can never
    be skipped silently (2026-11-17)."""
    by_file = {Path(str(p)).name: name for name, p in sources.items()}
    out, bad = [], []
    for b in blurs or []:
        src = b.get("source")
        name = src if src in sources else by_file.get(Path(str(src)).name) or by_file.get(str(src))
        if name is None:
            bad.append(str(src))
            continue
        out.append({**b, "source": name})
    if bad:
        raise ValueError(f"E/blurs.json names source(s) {sorted(set(bad))} that are not clips of this edit "
                         f"({sorted(sources)}); fix the 'source' of those blurs")
    return out


def blurs_for(blurs: list[dict], source: str, start: float, end: float) -> list[dict]:
    """E/blurs.json entries overlapping [start, end] of `source`, as segment-relative windows."""
    out = []
    for b in blurs or []:
        if b.get("source") != source or b["end"] <= start or b["start"] >= end:
            continue
        out.append({"a": round(max(0.0, b["start"] - start), 3), "b": round(b["end"] - start, 3),
                    "rect": list(b["rect"])})
    return out


def _radius(r, rect: list[int]) -> int:
    return max(0, min(_even(r or 0), (min(rect[2], rect[3]) // 2) // 2 * 2))


# ----------------------------------------------------------------------------- assets

def _write_pgm(path: Path, a: np.ndarray) -> None:
    h, w = a.shape
    path.write_bytes(f"P5\n{w} {h}\n255\n".encode() + a.astype(np.uint8).tobytes())


def corner_mask(r: int) -> np.ndarray:
    """r x r alpha for the TOP-LEFT corner: 255 outside the quarter circle (plate shows),
    0 inside (content shows), anti-aliased by 4x4 supersampling."""
    ss = 4
    g = (np.arange(r * ss) + 0.5) / ss
    yy, xx = np.meshgrid(g, g, indexing="ij")
    out = ((xx - r) ** 2 + (yy - r) ** 2 > r * r).astype(np.float64)
    out = out.reshape(r, ss, r, ss).mean(axis=(1, 3))
    return np.round(out * 255)


def _hlg_to_display(x: np.ndarray) -> np.ndarray:
    a, b, c = 0.17883277, 0.28466892, 0.55991073
    e = np.where(x <= 0.5, x * x / 3.0, (np.exp((x - c) / a) + b) / 12.0)
    ys = 0.2627 * e[..., 0] + 0.6780 * e[..., 1] + 0.0593 * e[..., 2]
    fd = (np.maximum(ys, 1e-6) ** 0.2)[..., None] * e          # OOTF, gamma 1.2 (1000 nit)
    return fd * 1000.0 / 203.0                                 # reference white -> 1.0


def _pq_to_display(x: np.ndarray) -> np.ndarray:
    m1, m2, c1, c2, c3 = 0.1593017578125, 78.84375, 0.8359375, 18.8515625, 18.6875
    p = np.power(np.clip(x, 0, 1), 1 / m2)
    lin = np.power(np.maximum(p - c1, 0) / (c2 - c3 * p), 1 / m1)
    return lin * 10000.0 / 203.0


def make_hdr_lut(kind: str, path: Path, gain: float = 1.0, size: int = LUT_SIZE) -> Path:
    """A .cube LUT: HLG/PQ BT.2020 RGB -> SDR BT.709 RGB (see module docstring)."""
    g = np.linspace(0.0, 1.0, size)
    bb, gg, rr = np.meshgrid(g, g, g, indexing="ij")           # .cube: red varies fastest
    rgb = np.stack([rr, gg, bb], axis=-1).reshape(-1, 3)
    lin = (_hlg_to_display if kind == "hlg" else _pq_to_display)(rgb) * float(gain)
    m = np.array([[1.6605, -0.5876, -0.0728], [-0.1246, 1.1329, -0.0083],
                  [-0.0182, -0.1006, 1.1187]])
    lin = np.maximum(lin @ m.T, 0.0)
    k = KNEE
    y = np.where(lin <= k, lin, k + (1 - k) * (1 - np.exp(-(lin - k) / (1 - k))))
    out = np.clip(y, 0, 1) ** (1 / 2.4)
    lines = [f"TITLE \"rio2 {kind} to sdr709 gain {gain}\"", f"LUT_3D_SIZE {size}"]
    lines += [f"{a:.6f} {b:.6f} {c:.6f}" for a, b, c in out]
    path.write_text("\n".join(lines) + "\n")
    return path


def make_plate(layout: dict, path: Path) -> Path:
    """Background scaled (cover, centred) to the canvas, or background_color, as one PNG."""
    cw, chh = layout["canvas"]
    bg = layout.get("background")
    src = None
    if bg:
        p = Path(bg)
        p = p if p.is_absolute() else PROJECT / p
        if p.is_file():
            src = p
    if src is not None:
        cmd = ["-i", str(src), "-vf",
               f"scale={cw}:{chh}:force_original_aspect_ratio=increase:flags=bicubic,"
               f"crop={cw}:{chh},format=rgb24"]
    else:
        col = str(layout.get("background_color") or "#101418").replace("#", "0x")
        cmd = ["-f", "lavfi", "-i", f"color=c={col}:s={cw}x{chh}:d=1", "-vf", "format=rgb24"]
    _ff(["ffmpeg", "-y", "-nostdin", "-hide_banner", "-loglevel", "error"] + cmd
        + ["-frames:v", "1", "-update", "1", str(path)], "background plate")
    return path


def background_source(layout: dict) -> Path | None:
    bg = layout.get("background")
    if not bg:
        return None
    p = Path(bg)
    p = p if p.is_absolute() else PROJECT / p
    return p if p.is_file() else None


def prepare(layout: dict, work: Path, hdr_kinds: set[str]) -> dict:
    """Write the plate, the two corner masks and any HDR LUTs into `work` (once per render)."""
    work.mkdir(parents=True, exist_ok=True)
    assets = {"plate": make_plate(layout, work / "plate.png"), "masks": {}, "luts": {}}
    for name in ("screen", "cam"):
        r = _radius(layout.get(f"{name}_radius"), layout[f"{name}_rect"] or [0, 0, 2, 2])
        if r >= 2:
            m = corner_mask(r)                 # TL | TR over BL | BR, cropped in the graph
            _write_pgm(work / f"mask_{name}_{r}.pgm",
                       np.block([[m, m[:, ::-1]], [m[::-1, :], m[::-1, ::-1]]]))
            assets["masks"][name] = (work / f"mask_{name}_{r}.pgm", r)
    for kind in sorted(hdr_kinds):
        assets["luts"][kind] = make_hdr_lut(kind, work / f"{kind}_to_sdr.cube",
                                            float(layout.get("cam_hdr_gain") or 1.0))
    return assets


# ----------------------------------------------------------------------------- the graph

def _q(p: Path) -> str:
    """Path for a filtergraph option: single-quoted (spaces and colons are literal inside)."""
    s = str(p)
    if "'" in s or "\\" in s:
        raise RuntimeError(f"path not usable in an ffmpeg filter (quote or backslash): {s}")
    return f"'{s}'"


def _rate_parts(rate: str) -> tuple[int, int]:
    f = Fraction(rate)
    return f.numerator, f.denominator


def encoder_args(layout: dict) -> list[str]:
    enc = str(layout.get("encoder") or "hevc_videotoolbox")
    args = ["-c:v", enc]
    if enc.endswith("_videotoolbox"):
        # quality (constant quality, Apple Silicon) wins over bitrate: a fixed 40M left the
        # face shots soft (VMAF 93.7) while the still screen shots wasted bits; q 80 gives the
        # face ~77 Mbit/s (VMAF 96.6) and the screen ~13 (same score as 40M) (HMJ, 2026-11-09)
        q = layout.get("quality")
        rate = ["-q:v", str(int(q))] if q else ["-b:v", str(layout.get("bitrate") or "40M")]
        args += rate + ["-allow_sw", "1"]
        if enc.startswith("hevc"):
            args += ["-tag:v", "hvc1"]
    else:
        args += ["-crf", "20", "-preset", "fast"]
        if enc == "libx265":
            args += ["-tag:v", "hvc1"]
    return args + ["-pix_fmt", "yuv420p", "-colorspace", "bt709", "-color_primaries", "bt709",
                   "-color_trc", "bt709", "-color_range", "tv"]


def build_cmd(layout: dict, assets: dict, rate: str, screen: Path, screen_size: tuple[int, int],
              start: float, n: int, out: Path, cam: dict | None = None,
              screen_pre: str | None = None, threads: int | None = None,
              still: bool = False, blurs: list[dict] | None = None) -> list[str]:
    """ffmpeg command for one composite segment of exactly n frames (or one PNG when still).
    cam = {"path", "start", "drift", "size": (w, h), "hdr": kind|None, "track": path|None} or
    None (screen only). blurs = blurs_for(...) windows on the screen (source pixels)."""
    num, den = _rate_parts(rate)
    srect = fit_rect(screen_size, layout["screen_rect"])
    cmd = ["ffmpeg", "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
           "-i", str(assets["plate"])]
    if threads:
        cmd += ["-threads", str(threads)]
    cmd += ["-ss", f"{start:.3f}", "-i", str(screen)]
    if cam is not None:
        cmd += ["-hwaccel", "videotoolbox", "-ss", f"{cam['start']:.3f}", "-i", str(cam["path"])]
    masks = assets.get("masks") or {}
    mask_in = {}
    for name, (mp, _r) in masks.items():
        if name == "cam" and cam is None:
            continue
        mask_in[name] = len([a for a in cmd if a == "-i"])
        cmd += ["-i", str(mp)]

    patch_rects = []
    if "screen" in mask_in:
        patch_rects.append(("screen", srect, masks["screen"][1]))
    crect = None
    if cam is not None:
        crect = layout["cam_rect"]
        if "cam" in mask_in:
            patch_rects.append(("cam", crect, masks["cam"][1]))

    f = []
    k = 1 + 4 * len(patch_rects)
    f.append(f"[0:v]format=yuv420p,split={k}" + "".join(f"[p{i}]" for i in range(k)))
    f.append(f"[p0]loop=loop=-1:size=1:start=0,settb={den}/{num},setpts=N[base]")
    sp = ",".join(x for x in (screen_pre, f"fps={rate}:start_time=0") if x)
    if [srect[2], srect[3]] != list(screen_size):
        sp += f",scale={srect[2]}:{srect[3]}:flags=lanczos"
    sp += ",format=yuv420p"
    if blurs:
        f.append(f"[1:v]{sp}[scr0]")
        f += blur_graph("scr0", "scr", blurs, srect[2] / screen_size[0])
    else:
        f.append(f"[1:v]{sp}[scr]")
    f.append(f"[base][scr]overlay=x={srect[0]}:y={srect[1]}:eof_action=repeat[c0]")
    last = "c0"
    if cam is not None:
        cc = cover_crop(layout.get("cam_crop"), cam["size"], crect)
        drift = float(cam.get("drift") or 0.0)
        cp = (f"[2:v]setpts=PTS/{1.0 + drift:.9f},fps={rate}:start_time=0,"
              + tracked_crop(cc[2], cc[3], cam["size"], cam.get("track"), (cc[0], cc[1])))
        if cam.get("hdr") and cam["hdr"] in assets["luts"]:
            cp += (f",scale={crect[2]}:{crect[3]}:flags=lanczos:in_color_matrix=bt2020:"
                   f"in_range=tv,format=gbrp16le,"
                   f"lut3d=file={_q(assets['luts'][cam['hdr']])}:interp=tetrahedral,"
                   f"scale=out_color_matrix=bt709:out_range=tv,format=yuv420p")
        else:
            cp += f",scale={crect[2]}:{crect[3]}:flags=lanczos,format=yuv420p"
        if layout.get("cam_eq"):
            cp += f",eq={layout['cam_eq']}"
        cp += ",setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=tv"
        f.append(cp + "[cam]")
        f.append(f"[{last}][cam]overlay=x={crect[0]}:y={crect[1]}:eof_action=repeat[c1]")
        last = "c1"
    pi = 1
    for name, rect, r in patch_rects:
        x, y, w, h = rect
        mi = mask_in[name]
        f.append(f"[{mi}:v]format=gray,split=4[{name}m0][{name}m1][{name}m2][{name}m3]")
        # (no hflip/vflip: on a tiny gray frame they crash ffmpeg 8.1 with SIGBUS)
        corners = [(x, y, 0, 0), (x + w - r, y, r, 0), (x, y + h - r, 0, r),
                   (x + w - r, y + h - r, r, r)]
        for j, (cx, cy, mx, my) in enumerate(corners):
            f.append(f"[{name}m{j}]crop={r}:{r}:{mx}:{my}[{name}a{j}]")
            f.append(f"[p{pi}]crop={r}:{r}:{cx}:{cy}[{name}c{j}]")
            f.append(f"[{name}c{j}][{name}a{j}]alphamerge,format=yuva420p,"
                     f"loop=loop=-1:size=1:start=0,settb={den}/{num},setpts=N[{name}q{j}]")
            nxt = f"o{pi}"
            f.append(f"[{last}][{name}q{j}]overlay=x={cx}:y={cy}[{nxt}]")
            last = nxt
            pi += 1
    # tag the output BT.709 SDR (the plate's PNG frame carries no colour info, and VideoToolbox
    # writes only what the frames say)
    f.append(f"[{last}]setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:"
             f"range=tv[out]")
    graph = ";".join(f)
    cmd += ["-filter_complex", graph, "-map", "[out]", "-an"]
    if still:
        cmd += ["-frames:v", "1", "-update", "1", str(out)]
    else:
        cmd += encoder_args(layout) + ["-r", rate, "-fps_mode", "cfr", "-frames:v", str(n),
                                       str(out)]
    return cmd


def build_full_cmd(layout: dict, assets: dict, rate: str, src: Path, src_size: tuple[int, int],
                   start: float, n: int, out: Path, mode: str, drift: float = 0.0,
                   hdr: str | None = None, pre: str | None = None, threads: int | None = None,
                   still: bool = False, track: list[list[float]] | None = None) -> list[str]:
    """One full-canvas segment: mode "face" (src = the camera, start in camera time) or "full"
    (src = an insert clip). Exactly n frames CFR, or one PNG when still."""
    if mode not in ("face", "full"):
        raise ValueError(f"build_full_cmd: mode must be face or full, not {mode}")
    cw, chh = layout["canvas"]
    cmd = ["ffmpeg", "-y", "-nostdin", "-hide_banner", "-loglevel", "error"]
    if threads:
        cmd += ["-threads", str(threads)]
    if mode == "face":
        cmd += ["-hwaccel", "videotoolbox"]
    cmd += ["-ss", f"{start:.3f}", "-i", str(src)]
    chain = []
    if mode == "face":
        chain.append(f"setpts=PTS/{1.0 + float(drift or 0.0):.9f}")
    if pre:
        chain.append(pre)
    chain.append(f"fps={rate}:start_time=0")
    if mode == "face":
        x, y, w, h = face_crop(layout, src_size)
        chain.append(tracked_crop(w, h, src_size, track, (x, y)))
        fit = f"scale={cw}:{chh}:flags=lanczos"
    else:
        col = str(layout.get("background_color") or "#000000").replace("#", "0x")
        fit = (f"scale={cw}:{chh}:force_original_aspect_ratio=decrease:flags=lanczos,"
               f"pad={cw}:{chh}:(ow-iw)/2:(oh-ih)/2:color={col}")
    if hdr and hdr in (assets.get("luts") or {}):
        chain.append(f"{fit}:in_color_matrix=bt2020:in_range=tv" if mode == "face" else fit)
        chain.append(f"format=gbrp16le,lut3d=file={_q(assets['luts'][hdr])}:interp=tetrahedral,"
                     f"scale=out_color_matrix=bt709:out_range=tv")
    else:
        chain.append(fit)
    if mode == "face" and layout.get("cam_eq"):
        chain.append(f"format=yuv420p,eq={layout['cam_eq']}")
    chain.append("format=yuv420p,setparams=color_primaries=bt709:color_trc=bt709:"
                 "colorspace=bt709:range=tv")
    cmd += ["-filter_complex", "[0:v]" + ",".join(chain) + "[out]", "-map", "[out]", "-an"]
    if still:
        cmd += ["-frames:v", "1", "-update", "1", str(out)]
    else:
        cmd += encoder_args(layout) + ["-r", rate, "-fps_mode", "cfr", "-frames:v", str(n), str(out)]
    return cmd


def key_extra(layout: dict, cam: dict | None, mode: str = "split",
              blurs: list[dict] | None = None) -> str:
    """Everything composite-specific that decides a segment's bytes, for the cache key.
    Split keys (the default) stay as before apart from COMPOSITE_VERSION."""
    parts = {"v": COMPOSITE_VERSION, "layout": layout, "native_snap": NATIVE_SNAP,
             "knee": KNEE, "lut": LUT_SIZE}
    if mode != "split":
        parts["mode"] = mode
    bg = background_source(layout)
    if bg is not None:
        st = bg.stat()
        parts["bg"] = [str(bg.resolve()), st.st_size, st.st_mtime_ns]
    if cam is not None:
        st = Path(cam["path"]).stat()
        parts["cam"] = [str(Path(cam["path"]).resolve()), st.st_size, st.st_mtime_ns,
                        f"{cam['start']:.3f}", f"{float(cam.get('drift') or 0):.9g}", cam.get("hdr")]
        if cam.get("track"):
            parts["track"] = cam["track"]
    if blurs:
        parts["blurs"] = blurs
    return "composite:" + hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()


def _ff(cmd: list[str], what: str) -> None:
    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"{what} failed: {proc.stderr.strip()[-400:]}")


def run(cmd: list[str], what: str) -> None:
    _ff(cmd, what)
