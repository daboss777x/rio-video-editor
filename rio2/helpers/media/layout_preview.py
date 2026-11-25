"""PNG stills of the two-camera composite, for the user to approve the layout before any video
is rendered (docs/CONTRACTS.md, "Rio 2.0: two-camera 4K mode").

Usage:
    .venv/bin/python rio2/helpers/media/layout_preview.py --edit-dir E [--at S ...] -o E/layout/
        [--screen STEM] [--profile P]

Reads E/sources.json (screen files, relative to the run dir E/..) and E/cam_sync.json (run
sync_cam.py first). S is a time in seconds on the camera's screen clip (default: 15 %, 50 % and
85 % of the first camera's covers). Each still is the exact composite graph the renderer uses
(composite.build_cmd), one frame, so what you approve is what renders. Writes
<out>/layout_<S>.png at the canvas size and prints the paths. No video render, a second or two
per still. If cam_sync.json is not ok the stills show the screen on the background only.
Layouts and inserts: every still with the camera also gets a face-mode still (face_<S>.png, the
camera alone, layout.face_zoom), and each insert clip in sources.json gets one full-canvas still
at its middle (insert_<stem>.png), both through composite.build_full_cmd like the renderer.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import composite as C  # noqa: E402

sys.path.insert(0, str(C.PROJECT / "vendor" / "video-use" / "helpers"))
from render import probe_source_fps  # noqa: E402  (video-use)


def _screens(edit_dir: Path) -> dict[str, Path]:
    run_dir = edit_dir.parent
    out: dict[str, Path] = {}
    try:
        src = json.loads((edit_dir / "sources.json").read_text())
        files = src.get("files") or {}
        for stem in src.get("order") or list(files):
            if stem in files:
                out[stem] = run_dir / files[stem]
    except (OSError, ValueError):
        pass
    if not out:
        try:
            edl = json.loads((edit_dir / "edl.json").read_text())
            for k, v in (edl.get("sources") or {}).items():
                p = Path(v)
                out[k] = p if p.is_absolute() else (edit_dir / p).resolve()
        except (OSError, ValueError):
            pass
    return out


def preview(edit_dir: Path, out_dir: Path, at: list[float] | None = None,
            screen: str | None = None, profile: Path | None = None) -> list[Path]:
    edit_dir = Path(edit_dir)
    layout = C.load_layout(profile)
    if layout is None:
        raise RuntimeError("the profile has no `layout` block")
    sync_path = edit_dir / "cam_sync.json"
    if not sync_path.exists():
        raise RuntimeError(f"{sync_path} not found: run sync_cam.py --edit-dir first")
    sync = C.load_cam_sync(edit_dir)
    screens = _screens(edit_dir)
    if not screens:
        raise RuntimeError("no screen clips found (E/sources.json or E/edl.json)")
    cams = [c for c in (sync or {}).get("cams") or [] if c.get("offset_s") is not None]
    stem = screen or (cams[0]["screen"] if cams else next(iter(screens)))
    if stem not in screens or not screens[stem].is_file():
        raise RuntimeError(f"screen clip not found: {stem}")
    spath = screens[stem]
    if not at:
        c0 = next((c for c in cams if c["screen"] == stem), None)
        lo, hi = c0["covers"] if c0 else (0.0, float(C.probe_duration(spath)))
        at = [round(lo + f * (hi - lo), 2) for f in (0.15, 0.5, 0.85)]
    if sync is None:
        print("WARNING: cam_sync.json is not ok -> stills without the camera")
    sinfo = C.probe_video(spath)
    rate = str(layout["fps"]) if layout.get("fps") else (probe_source_fps(spath) or "30")
    cam_info: dict[str, dict] = {}
    for c in cams:
        p = C.resolve_cam(c, edit_dir)
        cam_info[c["cam"]] = {"path": p, **C.probe_video(p)}
    work = out_dir / "_assets"
    assets = C.prepare(layout, work, {v["hdr"] for v in cam_info.values() if v["hdr"]})
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    try:
        for t in at:
            cam = None
            if sync is not None:
                entry, why = C.cam_for_range(sync, stem, t, t)
                if entry is None:
                    print(f"  {t:.2f}s: {why} -> no camera in this still")
                else:
                    info = cam_info[entry["cam"]]
                    cam = {"path": info["path"], "start": C.cam_time(entry, t),
                           "drift": entry.get("drift") or 0.0, "size": (info["w"], info["h"]),
                           "hdr": info["hdr"]}
            out = out_dir / f"layout_{t:09.2f}.png"
            C.run(C.build_cmd(layout, assets, rate, spath, (sinfo["w"], sinfo["h"]), t, 1, out,
                              cam, still=True), f"still at {t}s")
            written.append(out)
            print(f"  {out}")
            if cam is not None:
                fout = out_dir / f"face_{t:09.2f}.png"
                C.run(C.build_full_cmd(layout, assets, rate, cam["path"], cam["size"], cam["start"], 1,
                                       fout, "face", drift=cam["drift"], hdr=cam["hdr"], still=True),
                      f"face still at {t}s")
                written.append(fout)
                print(f"  {fout}")
        try:
            src = json.loads((edit_dir / "sources.json").read_text())
        except (OSError, ValueError):
            src = {}
        for ist, fname in (src.get("inserts") or {}).items():
            ip = edit_dir.parent / fname
            if not ip.is_file():
                continue
            info = C.probe_video(ip)
            iout = out_dir / f"insert_{ist}.png"
            C.run(C.build_full_cmd(layout, assets, rate, ip, (info["w"], info["h"]),
                                   C.probe_duration(ip) / 2, 1, iout, "full", still=True),
                  f"insert still {ist}")
            written.append(iout)
            print(f"  {iout}")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return written


def main() -> None:
    ap = argparse.ArgumentParser(description="PNG stills of the two-camera composite layout")
    ap.add_argument("--edit-dir", type=Path, required=True)
    ap.add_argument("--at", type=float, action="append", default=None,
                    help="screen-clip time (s); repeatable; default 3 points across the camera")
    ap.add_argument("-o", "--output", type=Path, default=None, help="folder (default E/layout/)")
    ap.add_argument("--screen", default=None, help="screen clip stem (default the camera's)")
    ap.add_argument("--profile", type=Path, default=None)
    a = ap.parse_args()
    try:
        paths = preview(a.edit_dir, a.output or a.edit_dir / "layout", a.at, a.screen, a.profile)
    except (RuntimeError, OSError, ValueError, KeyError) as e:
        sys.exit(f"layout_preview: {e}")
    print(f"{len(paths)} still(s) written")


if __name__ == "__main__":
    main()
