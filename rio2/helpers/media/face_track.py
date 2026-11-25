"""Face tracking for the camera framing (Rio 2.0, docs/CONTRACTS.md "Camera look").

    .venv/bin/python rio2/helpers/media/face_track.py --edit-dir E [--step 1.0]

For every kept EDL range with camera coverage (inserts skipped), samples the synced camera once
every --step seconds (960 px wide JPEGs, VideoToolbox decode, one ffmpeg per range), finds the
face with macOS Vision (vision.swift, built into the OS: no installs) and writes
E/face_track.json:
    {"version": 2, "step": 1.0, "cams": {"<cam stem>": [[cam_t, cx, cy, face_h, face_w, yaw], ...]}}
in the camera's own pixels, sorted by time; samples with no face are left out. The render reads it
(composite.face_path) and centres the camera bubble and the face-only crop on the face, per cut:
a still face gets a fixed crop, a face that moves inside one cut gets a smooth pan.
Peak RAM: one 4K decoder + a 960 px frame (~300 MB); Vision runs after ffmpeg exits. No tokens.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import composite as C  # noqa: E402
from vision import run_vision  # noqa: E402,F401  (shared with blur_find: compiled, parallel)

SWIFT = HERE / "vision.swift"
MIN_CONF = 0.5
WIDTH = 960


def best_face(faces: list[list[float]]) -> list[float] | None:
    """The largest confident face [x, y, w, h, conf], or None."""
    ok = [f for f in faces if len(f) >= 5 and f[4] >= MIN_CONF]
    return max(ok, key=lambda f: f[2] * f[3]) if ok else None


def sample_ranges(edl: dict, sync: dict, edit_dir: Path, step: float = 1.0) -> list[dict]:
    """[{cam, path, start (cam s), dur, size}] per kept range with camera (merged per cam span)."""
    inserts = set(edl.get("inserts") or [])
    out = []
    for r in edl.get("ranges") or []:
        if r.get("insert") or r["source"] in inserts:
            continue
        entry, _ = C.cam_for_range(sync, r["source"], float(r["start"]), float(r["end"]))
        if entry is None:
            continue
        t0 = C.cam_time(entry, float(r["start"]))
        t1 = C.cam_time(entry, float(r["end"]))
        out.append({"cam": entry["cam"], "path": C.resolve_cam(entry, edit_dir),
                    "start": t0, "dur": max(t1 - t0, 0.05)})
    return out


def extract(spans: list[dict], work: Path, step: float) -> list[tuple[str, float, Path]]:
    """JPEGs every `step` s of each span -> [(cam, cam_t, path)]."""
    shots = []
    for i, s in enumerate(spans):
        pat = work / f"r{i:04d}_%03d.jpg"
        cmd = ["ffmpeg", "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
               "-hwaccel", "videotoolbox", "-ss", f"{s['start']:.3f}", "-t", f"{s['dur']:.3f}",
               "-i", str(s["path"]), "-map", "0:v:0", "-an",
               "-vf", f"fps=1/{step}:start_time=0,scale={WIDTH}:-2,format=yuvj420p", "-q:v", "4", str(pat)]
        proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"face_track: ffmpeg failed: {proc.stderr.strip()[-300:]}")
        for k, f in enumerate(sorted(work.glob(f"r{i:04d}_*.jpg"))):
            shots.append((s["cam"], s["start"] + k * step, f))
    return shots


def track(edit_dir: Path, step: float = 1.0) -> dict:
    edl = json.loads((edit_dir / "edl.json").read_text())
    sync = C.load_cam_sync(edit_dir)
    if sync is None:
        raise RuntimeError("face_track: E/cam_sync.json missing or not ok (camera mode only)")
    work = edit_dir / "_face_track"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    try:
        spans = sample_ranges(edl, sync, edit_dir, step)
        sizes = {s["cam"]: C.probe_video(s["path"]) for s in spans}
        shots = extract(spans, work, step)
        res = run_vision("faces", [p for _, _, p in shots], work)
        cams: dict[str, list] = {}
        seen = set()
        for cam, t, p in shots:
            f = best_face((res.get(str(p)) or {}).get("faces") or [])
            key = (cam, round(t, 1))
            if f is None or key in seen:
                continue
            seen.add(key)
            k = sizes[cam]["w"] / WIDTH
            x, y, w, h = (v * k for v in f[:4])
            yaw = float(f[5]) if len(f) > 5 else 0.0
            cams.setdefault(cam, []).append([round(t, 2), round(x + w / 2), round(y + h / 2), round(h),
                                             round(w), round(yaw, 3)])
        for v in cams.values():
            v.sort()
        out = {"version": 2, "step": step, "cams": cams,
               "samples": len(shots), "found": sum(len(v) for v in cams.values())}
        tmp = edit_dir / "face_track.json.tmp"
        tmp.write_text(json.dumps(out))
        os.replace(tmp, edit_dir / "face_track.json")
        return out
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--edit-dir", type=Path, required=True)
    ap.add_argument("--step", type=float, default=1.0)
    a = ap.parse_args(argv)
    out = track(a.edit_dir.resolve(), a.step)
    print(f"face_track: {out['found']}/{out['samples']} frames with a face "
          f"({', '.join(f'{c}: {len(v)}' for c, v in out['cams'].items())}) -> face_track.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
