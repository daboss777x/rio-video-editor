"""Segment cache in render_tutorial.py (docs/CONTRACTS.md, Revise loop)."""
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "helpers" / "media" / "render_tutorial.py"

import checks  # noqa: E402

# every range is inside a fixture speech segment (3-9 s, 17-23 s): an all-silent edit
# makes loudnorm measure -inf, which is outside what this test is about
RANGES = [{"source": "clip", "start": 3.5, "end": 4.7},
          {"source": "clip", "start": 5.0, "end": 6.5},
          {"source": "clip", "start": 17.5, "end": 18.5},
          {"source": "clip", "start": 20.0, "end": 21.3}]


def _render(edit: Path, src: Path, ranges, out_name="final.mp4", *extra):
    edl = {"version": 1, "sources": {"clip": str(src)}, "ranges": ranges,
           "grade": "none", "overlays": []}
    (edit / "edl.json").write_text(json.dumps(edl))
    out = edit / out_name
    proc = subprocess.run([sys.executable, str(SCRIPT), str(edit / "edl.json"), "-o", str(out),
                           "--threads", "2", *extra], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr[-800:]
    m = re.search(r"cache: (\d+) reused, (\d+) extracted", proc.stdout)
    assert m, proc.stdout
    return out, int(m.group(1)), int(m.group(2))


def _frames(path: Path) -> int:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets",
                          "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True)
    return int(out.stdout.strip())


def _seg_files(edit: Path) -> set[str]:
    return {p.name for p in (edit / "segments").iterdir() if p.name != "manifest.json"}


def test_cache_reuse_and_equals_uncached(fixture_paths, tmp_path):
    edit = tmp_path / "cache dir" / "edit"
    edit.mkdir(parents=True)
    src = Path(fixture_paths["video"])

    _, r, x = _render(edit, src, RANGES)
    assert (r, x) == (0, 4)
    assert len(_seg_files(edit)) == 8
    assert not (edit / "clips_tutorial").exists() and not (edit / "base_v.mp4").exists()

    changed = [dict(rg) for rg in RANGES]
    changed[2]["end"] = 18.9
    out2, r, x = _render(edit, src, changed)
    assert (r, x) == (3, 1)

    out_nc, r, x = _render(edit, src, changed, "nocache.mp4", "--no-cache")
    assert (r, x) == (0, 4)
    assert not (edit / "clips_tutorial").exists()
    assert _frames(out2) == _frames(out_nc)
    d2, dn = checks.av_durations(out2), checks.av_durations(out_nc)
    assert abs(d2["video_s"] - dn["video_s"]) < 1e-3, (d2, dn)
    assert abs(d2["audio_s"] - dn["audio_s"]) < 1e-3, (d2, dn)


def test_manifest_keeps_last_two_renders(fixture_paths, tmp_path):
    edit = tmp_path / "prune" / "edit"
    edit.mkdir(parents=True)
    src = Path(fixture_paths["video"])
    v1 = RANGES[:2]
    v2 = [RANGES[0], RANGES[2]]
    v3 = [RANGES[3]]
    _render(edit, src, v1)
    (edit / "segments" / "stray.tmp.mp4").write_bytes(b"junk")   # leftover of a crash
    _render(edit, src, v2)
    _, r, x = _render(edit, src, v3)
    assert (r, x) == (0, 1)
    man = json.loads((edit / "segments" / "manifest.json").read_text())
    assert len(man["renders"]) == 2
    assert all({"at", "edl", "keys"} <= set(e) for e in man["renders"])
    keep = {k for e in man["renders"] for k in e["keys"]}
    assert len(keep) == 3   # v2's two ranges + v3's one; v1's range 1 is pruned
    assert _seg_files(edit) == {f"{k}{ext}" for k in keep for ext in (".mp4", ".wav")}
    # v2 is still cached in full
    _, r, x = _render(edit, src, v2)
    assert (r, x) == (2, 0)


def test_corrupt_or_partial_segment_reextracted(fixture_paths, tmp_path):
    edit = tmp_path / "corrupt" / "edit"
    edit.mkdir(parents=True)
    src = Path(fixture_paths["video"])
    _render(edit, src, RANGES[:3])
    keys = json.loads((edit / "segments" / "manifest.json").read_text())["renders"][-1]["keys"]
    v0 = edit / "segments" / f"{keys[0]}.mp4"
    v0.write_bytes(v0.read_bytes()[: v0.stat().st_size // 2])      # truncated video
    a1 = edit / "segments" / f"{keys[1]}.wav"
    a1.write_bytes(a1.read_bytes()[: a1.stat().st_size - 1000])    # truncated audio
    (edit / "segments" / f"{keys[2]}.wav").unlink()                # half a pair missing
    out, r, x = _render(edit, src, RANGES[:3])
    assert (r, x) == (0, 3)
    _, r, x = _render(edit, src, RANGES[:3])
    assert (r, x) == (3, 0)
    d = checks.av_durations(out)
    assert abs(d["video_s"] - d["audio_s"]) <= 0.025, d
