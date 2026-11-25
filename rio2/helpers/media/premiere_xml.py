"""Premiere Pro export: Rio's cut list as a Final Cut Pro 7 XML (xmeml v4) sequence on the raw clips.

User, 2026-11-25: every finished edit and revision also writes output/<name>_edited[_vN].xml, so the
edit can be opened in Premiere (File > Import) and carried on by hand. V1 = Rio's cuts on the raw
clips (drag an edge and the footage Rio removed is still there). Blurs, the 4K upscale and the voice
filter are not rebuilt in Premiere (user chose raw cuts only): each blur is a marker instead.

Timeline frames come from the running total of exact seconds (rounded once per edge), so a long edit
never drifts; each clip's source length equals its timeline length.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.parse
from fractions import Fraction
from pathlib import Path
from xml.sax.saxutils import escape


def _rate(fps: str) -> tuple[int, bool, Fraction]:
    """'60/1' -> (60, False, 60); '30000/1001' -> (30, True, 29.97...)."""
    f = Fraction(fps)
    ntsc = f.denominator == 1001
    return round(float(f)), ntsc, f


def _url(path: str) -> str:
    return "file://localhost" + urllib.parse.quote(str(path))


def placements(ranges: list[dict], fps: str) -> list[dict]:
    """Each range's timeline start/end and source in/out, in frames."""
    _, _, f = _rate(fps)
    out, t = [], Fraction(0)
    for r in ranges:
        dur = Fraction(str(r["end"])) - Fraction(str(r["start"]))
        start, end = round(t * f), round((t + dur) * f)
        src_in = round(Fraction(str(r["start"])) * f)
        out.append({"source": r["source"], "start": start, "end": end, "in": src_in, "out": src_in + (end - start)})
        t += dur
    return out


def _tc_s(tc: str) -> float:
    secs = 0.0
    for part in str(tc).split(":"):
        secs = secs * 60 + float(part)
    return secs


def _out_span(ranges: list[dict], source: str, a: float, b: float) -> tuple[float, float] | None:
    """The output time span over which source seconds a..b play (None if all of it was cut)."""
    lo = hi = None
    t = 0.0
    for r in ranges:
        s, e = float(r["start"]), float(r["end"])
        if r["source"] == source and b > s and a < e:
            x, y = t + max(a, s) - s, t + min(b, e) - s
            lo = x if lo is None else min(lo, x)
            hi = y if hi is None else max(hi, y)
        t += e - s
    return None if lo is None else (lo, hi)


def markers(ranges: list[dict], problems: list[dict], blurs: list[dict]) -> list[dict]:
    """Markers in output seconds: one per blurred stretch (same kind, overlapping = one), one per
    verifier problem. Sorted by time. {"in", "out", "name", "comment"}."""
    spans: dict[str, list[list[float]]] = {}
    for bl in blurs:
        sp = _out_span(ranges, bl["source"], float(bl["start"]), float(bl["end"]))
        if sp:
            spans.setdefault(str(bl.get("text") or "blur"), []).append(list(sp))
    out = []
    for kind, ss in spans.items():
        ss.sort()
        merged = [ss[0]]
        for a, b in ss[1:]:
            if a <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        out += [{"in": a, "out": b, "name": f"Blur: {kind}", "comment": "blurred in Rio's video"} for a, b in merged]
    for p in problems:
        if p.get("output_tc"):
            t = _tc_s(p["output_tc"])
            out.append({"in": t, "out": t, "name": f"Ines #{p.get('rank', '?')}",
                        "comment": str(p.get("evidence") or "")[:300]})
    return sorted(out, key=lambda m: (m["in"], m["name"]))


def build_xml(ranges: list[dict], clips: dict, *, fps: str, size: tuple[int, int], name: str,
              markers: list[dict] = (), audio: dict | None = None) -> str:
    """The xmeml text. V1 = the cuts on the raw clips; A1/A2 = Rio's finished audio (`audio`: {path,
    duration_s}) when given, A3/A4 = the raw clips' own audio for each cut, switched off while Rio's
    audio is there; `markers` in output seconds (see markers())."""
    tb, ntsc, f = _rate(fps)
    rate = f"<rate><timebase>{tb}</timebase><ntsc>{'TRUE' if ntsc else 'FALSE'}</ntsc></rate>"
    places = placements(ranges, fps)
    total = places[-1]["end"] if places else 0
    w, h = size
    seen: set[str] = set()

    def file_el(stem: str) -> str:
        fid = "file-" + re.sub(r"[^A-Za-z0-9_-]", "_", stem)   # ids carry no XML symbols
        if fid in seen:
            return f'<file id="{fid}"/>'
        seen.add(fid)
        c = clips[stem]
        dur = round(Fraction(str(c["duration_s"])) * f)
        return (f'<file id="{fid}"><name>{escape(c["path"].rsplit("/", 1)[-1])}</name>'
                f'<pathurl>{escape(_url(c["path"]))}</pathurl>{rate}<duration>{dur}</duration>'
                f'<media><video><samplecharacteristics>{rate}<width>{w}</width><height>{h}</height>'
                f'</samplecharacteristics></video><audio><channelcount>{c.get("channels", 2)}</channelcount>'
                f'</audio></media></file>')

    def raw_audio(ch: int, enabled: bool) -> str:
        return "".join(
            f'<clipitem id="a{ch}-{i}"><name>{escape(p["source"])}</name><enabled>{"TRUE" if enabled else "FALSE"}</enabled>'
            f'{rate}<start>{p["start"]}</start><end>{p["end"]}</end><in>{p["in"]}</in><out>{p["out"]}</out>'
            f'{file_el(p["source"])}<sourcetrack><mediatype>audio</mediatype><trackindex>{ch}</trackindex></sourcetrack>'
            f'</clipitem>' for i, p in enumerate(places))

    def rio_audio(ch: int) -> str:
        first = ch == 1
        fdur = round(Fraction(str(audio["duration_s"])) * f)
        fel = (f'<file id="rio-audio"><name>{escape(str(audio["path"]).rsplit("/", 1)[-1])}</name>'
               f'<pathurl>{escape(_url(audio["path"]))}</pathurl>{rate}<duration>{fdur}</duration>'
               f'<media><audio><channelcount>2</channelcount></audio></media></file>') if first else '<file id="rio-audio"/>'
        return (f'<clipitem id="rio-audio-{ch}"><name>Rio audio</name><enabled>TRUE</enabled>{rate}'
                f'<start>0</start><end>{total}</end><in>0</in><out>{total}</out>{fel}'
                f'<sourcetrack><mediatype>audio</mediatype><trackindex>{ch}</trackindex></sourcetrack></clipitem>')

    marks = "".join(
        f'<marker><name>{escape(m["name"])}</name><comment>{escape(m.get("comment", ""))}</comment>'
        f'<in>{round(Fraction(str(m["in"])) * f)}</in>'
        f'<out>{round(Fraction(str(m["out"])) * f) if m["out"] > m["in"] else -1}</out></marker>'
        for m in markers)
    items = "".join(
        f'<clipitem id="v-{i}"><name>{escape(p["source"])}</name><enabled>TRUE</enabled>{rate}'
        f'<start>{p["start"]}</start><end>{p["end"]}</end><in>{p["in"]}</in><out>{p["out"]}</out>'
        f'{file_el(p["source"])}</clipitem>'
        for i, p in enumerate(places))
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n<xmeml version="4">'
            f'<sequence id="rio-sequence"><name>{escape(name)}</name><duration>{total}</duration>{rate}'
            f'<timecode>{rate}<string>00:00:00:00</string><frame>0</frame><displayformat>NDF</displayformat></timecode>{marks}'
            f'<media><video><format><samplecharacteristics>{rate}<width>{w}</width><height>{h}</height>'
            f'<pixelaspectratio>square</pixelaspectratio></samplecharacteristics></format>'
            f'<track>{items}</track></video><audio><numOutputChannels>2</numOutputChannels>'
            + (f'<track>{rio_audio(1)}</track><track>{rio_audio(2)}</track>' if audio else '')
            + f'<track>{raw_audio(1, not audio)}</track><track>{raw_audio(2, not audio)}</track>'
            f'</audio></media></sequence></xmeml>\n')


def _load(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def export(edit_dir: Path, out_xml: Path) -> Path:
    """Write out_xml (+ <stem>_audio.wav next to it, Rio's finished sound) for the edit in edit_dir."""
    E = Path(edit_dir).resolve()
    edl = json.loads((E / "edl.json").read_text())
    probe = _load(E / "probe.json", {})
    clips_probe = probe.get("clips") or {}
    clips = {}
    for stem, rel in (edl.get("sources") or {}).items():
        p = Path(rel)
        p = p if p.is_absolute() else (E / p).resolve()
        info = clips_probe.get(stem) or {}
        clips[stem] = {"path": str(p), "duration_s": info.get("duration_s") or 0,
                       "channels": info.get("audio_channels") or 2}
    first = clips_probe.get(edl["ranges"][0]["source"]) if edl.get("ranges") else None
    first = first or probe
    out_xml = Path(out_xml).resolve()
    out_xml.parent.mkdir(parents=True, exist_ok=True)
    wav = out_xml.with_name(out_xml.stem + "_audio.wav")
    audio = None
    final = E / "final.mp4"
    if final.exists():
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(final), "-vn", "-ac", "2", "-ar", "48000",
                        "-c:a", "pcm_s16le", str(wav)], check=True)
        dur = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                              str(wav)], capture_output=True, text=True, check=True).stdout
        audio = {"path": str(wav), "duration_s": float(dur)}
    marks = markers(edl["ranges"], (_load(E / "verify" / "verdict.json", {}) or {}).get("problems") or [],
                    (_load(E / "blurs.json", {}) or {}).get("blurs") or [])
    xml = build_xml(edl["ranges"], clips, fps=str(first.get("avg_fps") or "30/1"),
                    size=(int(first.get("width") or 1920), int(first.get("height") or 1080)),
                    name=out_xml.stem, markers=marks, audio=audio)
    out_xml.write_text(xml)
    return out_xml


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Write a Premiere Pro XML (+ Rio's audio as .wav) for an edit.")
    ap.add_argument("--edit-dir", type=Path, required=True)
    ap.add_argument("-o", "--out", type=Path, required=True, help="the .xml to write")
    a = ap.parse_args(argv)
    p = export(a.edit_dir, a.out)
    print(f"premiere xml: {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
