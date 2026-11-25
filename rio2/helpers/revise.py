#!/usr/bin/env python3
"""Revise loop: re-edit only the sections the user noted, splice, heal, check (docs/CONTRACTS.md "Revise loop").

API (pure; files only under E = the run's edit dir):
  parse_notes(text)                  -> (version | None, [{"tc_s", ["tc_end_s",] "text"}])
  out_spans(ranges, fps)             -> [(out_start, out_end)] cumulative n/fps like the renderer
  build_windows(ranges, notes, fps, window_s, probe_durs) -> windows.json list
  start(E, notes_file, of, window_s) -> N          creates E/revisions/vN/{notes.txt,windows.json,window_NN.md,meta.json}
  apply(E, N, profile)               -> (ok, violations)   splices window_NN.edl.json, refit + heal, checks
  report(E, N, lessons_path, today)  -> REVISION.md text   (and appends to ## Proposed in LESSONS.md)

CLI: revise.py start|apply|report --edit-dir E ...   (apply exits 1 and leaves E/edl.json untouched on failure)
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import date
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from helpers import chunk_edl as ce  # noqa: E402

CONTEXT_S = 20.0
EPS = ce.EPS
NOTE_RE = re.compile(r"^(\d+(?::\d{1,2}){1,2}(?:\.\d+)?)[\s\-–—:]*(.*)$")
TC = r"\d+(?::\d{1,2}){1,2}(?:\.\d+)?"
SPAN_RE = re.compile(rf"^({TC})\s*[-–—]\s*({TC})(?![\d.]|:\d)[\s\-–—:]*(.*)$")  # "20:30 - 21:55 cut this"
VERSION_RE = re.compile(r"^version\s*:\s*(\d+)\s*$", re.I)


# ---------------------------------------------------------------- notes
def parse_timecode(tc: str) -> float:
    parts = tc.split(":")
    secs = 0.0
    for p in parts:
        secs = secs * 60 + float(p)
    return round(secs, 3)


def parse_notes(text: str):
    """Returns (version or None, notes). Notes: [{"tc_s", "text"}] in file order. Raises ValueError
    naming the line for a line that does not start with a timecode or has no text."""
    version, notes = None, []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = VERSION_RE.match(line)
        if m:
            if version is not None or notes:
                raise ValueError(f"line {n}: 'version:' must be the first line")
            version = int(m.group(1))
            continue
        m = SPAN_RE.match(line)
        if m and m.group(3).strip():
            a, b = parse_timecode(m.group(1)), parse_timecode(m.group(2))
            if b < a:
                raise ValueError(f"line {n}: the span ends before it starts ({m.group(2)} is before {m.group(1)})")
            notes.append({"tc_s": a, "tc_end_s": b, "text": m.group(3).strip()})
            continue
        m = NOTE_RE.match(line)
        if not m or not m.group(2).strip():
            raise ValueError(f"line {n}: expected '<timecode> - <note>' (e.g. 12:34 - cut the aside), got {line!r}")
        notes.append({"tc_s": parse_timecode(m.group(1)), "text": m.group(2).strip()})
    return version, notes


def note_tc(nt: dict) -> str:
    """'20:30', or '20:30 - 21:55' for a span note."""
    return fmt_tc(nt["tc_s"]) + (f" - {fmt_tc(nt['tc_end_s'])}" if nt.get("tc_end_s") is not None else "")


def fmt_tc(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(int(t), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# ---------------------------------------------------------------- timeline + windows
def _fps(E: Path) -> float:
    pr = json.loads((E / "probe.json").read_text())
    return float(Fraction(pr.get("avg_fps") or pr.get("r_fps") or "30/1"))


def out_spans(ranges: list[dict], fps: float) -> list[tuple[float, float]]:
    """Output span of every range: n = max(1, round(dur*fps)) frames, cumulative n/fps (the renderer's)."""
    t, spans = 0.0, []
    for r in ranges:
        d = max(1, round((r["end"] - r["start"]) * fps)) / fps
        spans.append((t, t + d))
        t += d
    return spans


def build_windows(ranges, notes, fps, window_s=60.0, durs=None):
    """Windows over `ranges` for `notes`; raises ValueError if a note is past the end."""
    spans = out_spans(ranges, fps)
    total = spans[-1][1] if spans else 0.0
    sel, anchors = set(), []  # anchors: (note, range index)
    for nt in notes:
        tc = nt["tc_s"]
        if tc > total + 1e-6:
            raise ValueError(f"note at {fmt_tc(tc)} is past the end of this version ({fmt_tc(total)})")
        lo, hi = tc - window_s, max(tc, nt.get("tc_end_s") or tc) + window_s  # a span note covers A..B
        hit = [i for i, (a, b) in enumerate(spans) if b > lo and a < hi]
        sel.update(hit)
        anchor = next((i for i, (a, b) in enumerate(spans) if a <= tc < b), len(spans) - 1)
        sel.add(anchor)
        anchors.append((nt, anchor))
    # runs of consecutive indices overlap/merge; then split per source
    groups, cur = [], []
    for i in sorted(sel):
        if cur and i == cur[-1] + 1 and ranges[i]["source"] == ranges[cur[-1]]["source"]:
            cur.append(i)
        else:
            if cur:
                groups.append(cur)
            cur = [i]
    if cur:
        groups.append(cur)
    out = []
    for k, g in enumerate(groups, 1):
        src = ranges[g[0]]["source"]
        end_cap = (durs or {}).get(src)
        hi = ranges[g[-1]]["end"] + CONTEXT_S
        out.append({
            "id": k,
            "notes": [nt for nt, a in anchors if a in g],
            "out_start": round(spans[g[0]][0], 3), "out_end": round(spans[g[-1]][1], 3),
            "range_first": g[0], "range_last": g[-1], "source": src,
            "src_start": round(max(0.0, ranges[g[0]]["start"] - CONTEXT_S), 3),
            "src_end": round(min(hi, end_cap) if end_cap else hi, 3),
        })
    return out


# ---------------------------------------------------------------- versions
def _rev_dir(E: Path, n: int) -> Path:
    return E / "revisions" / f"v{n}"


def latest_version(E: Path) -> int:
    best = 1
    rd = E / "revisions"
    if rd.is_dir():
        for d in rd.iterdir():
            m = re.fullmatch(r"v(\d+)", d.name)
            if m and (d / "edl.json").exists():
                best = max(best, int(m.group(1)))
    return best


def load_version_edl(E: Path, n: int) -> dict:
    p = _rev_dir(E, n) / "edl.json"
    if not p.exists():
        if n == 1 and latest_version(E) == 1 and (E / "edl.json").exists():
            p = E / "edl.json"
        else:
            raise SystemExit(f"no EDL for version {n} ({_rev_dir(E, n) / 'edl.json'})")
    return json.loads(p.read_text())


def _words_in(E: Path, stems):
    return {s: ce.load_words(E, s) for s in stems}


def _kept(words, ranges, src):
    """Per word: True when its midpoint lies in a range of `src`."""
    rs = [r for r in ranges if r["source"] == src]
    return [any(r["start"] - EPS <= (w["start"] + w["end"]) / 2 <= r["end"] + EPS for r in rs) for w in words]


def _runs(words, flags, lo, hi):
    """Lines of consecutive words with the same KEPT/CUT state inside [lo, hi]."""
    lines, cur = [], None
    for w, k in zip(words, flags):
        if w["end"] < lo or w["start"] > hi:
            continue
        if cur and cur["k"] == k and w["start"] - cur["end"] < 1.0 and len(cur["t"]) < 25:
            cur["t"].append(w["text"])
            cur["end"] = w["end"]
        else:
            if cur:
                lines.append(cur)
            cur = {"k": k, "start": w["start"], "end": w["end"], "t": [w["text"]]}
    if cur:
        lines.append(cur)
    return [f"[{c['start']:.2f}-{c['end']:.2f}] {'KEPT' if c['k'] else 'CUT'}: {' '.join(c['t'])}" for c in lines]


def start(E: Path, notes_file: Path, of: int | None = None, window_s: float = 60.0) -> int:
    E = Path(E)
    text = Path(notes_file).read_text()
    version, notes = parse_notes(text)
    if not notes:
        raise SystemExit("no notes found in the notes file")
    latest = latest_version(E)
    base = of or version or latest
    if latest == 1 and not (_rev_dir(E, 1) / "edl.json").exists():
        (_rev_dir(E, 1)).mkdir(parents=True, exist_ok=True)
        shutil.copy2(E / "edl.json", _rev_dir(E, 1) / "edl.json")  # archive v1 before the first revision
    edl = load_version_edl(E, base)
    ranges = edl["ranges"]
    stems = list(dict.fromkeys(r["source"] for r in ranges))
    _, words, durs, _ = ce._context(E, stems)
    fps = _fps(E)
    try:
        wins = build_windows(ranges, notes, fps, window_s, durs)
    except ValueError as ex:
        raise SystemExit(str(ex))
    n = latest + 1
    d = _rev_dir(E, n)
    d.mkdir(parents=True, exist_ok=True)
    for old in d.glob("window_*"):  # a failed earlier attempt at vN leaves stale windows/replacements
        old.unlink()
    (d / "notes.txt").write_text(text)
    (d / "windows.json").write_text(json.dumps(wins, indent=2) + "\n")
    (d / "meta.json").write_text(json.dumps({"of": base, "window_s": window_s, "fps": fps}, indent=2) + "\n")
    for w in wins:
        ws = words[w["source"]]
        flags = _kept(ws, ranges, w["source"])
        mine = ranges[w["range_first"]:w["range_last"] + 1]
        prev_end = next((r["end"] for r in reversed(ranges[:w["range_first"]]) if r["source"] == w["source"]), None)
        next_start = next((r["start"] for r in ranges[w["range_last"] + 1:] if r["source"] == w["source"]), None)
        lines = [f"# Window {w['id']:02d} (revision v{n} of v{base})", "", "## Notes"]
        lines += [f"- [{note_tc(x)}] {x['text']}" for x in w["notes"]]
        lines += ["", f"Source `{w['source']}`, src {w['src_start']}-{w['src_end']} s "
                      f"(output {fmt_tc(w['out_start'])}-{fmt_tc(w['out_end'])}). "
                      f"Your array replaces ranges {w['range_first']}..{w['range_last']} and must stay inside "
                      f"[{w['src_start']}, {w['src_end']}] of `{w['source']}`."]
        if prev_end is not None or next_start is not None:
            lines.append(f"Neighbouring kept ranges: previous ends {prev_end}, next starts {next_start}; do not overlap them.")
        lines += ["", "## Transcript (KEPT = in the current edit, CUT = dropped)"]
        lines += _runs(ws, flags, w["src_start"], w["src_end"])
        lines += ["", "## Current ranges of this window (JSON)", "```json", json.dumps(mine, indent=2), "```", ""]
        (d / f"window_{w['id']:02d}.md").write_text("\n".join(lines))
    print(f"revision v{n} (of v{base}): {len(wins)} window(s) in {d}")
    for w in wins:
        print(f"  window_{w['id']:02d}: out {fmt_tc(w['out_start'])}-{fmt_tc(w['out_end'])}, {w['source']} "
              f"src {w['src_start']}-{w['src_end']}, ranges {w['range_first']}..{w['range_last']}, {len(w['notes'])} note(s)")
    return n


# ---------------------------------------------------------------- apply
def _check_replacement(w: dict, rep) -> list[str]:
    v = []
    if not isinstance(rep, list):
        return ["replacement must be a JSON array"]
    for i, r in enumerate(rep):
        if not isinstance(r, dict) or not all(k in r for k in ("source", "start", "end")):
            v.append(f"replacement range {i}: needs source/start/end")
            continue
        if r["source"] != w["source"]:
            v.append(f"replacement range {i}: source {r['source']!r} is not {w['source']!r}")
        if not isinstance(r["start"], (int, float)) or not isinstance(r["end"], (int, float)):
            v.append(f"replacement range {i}: start/end not numbers")
            continue
        if r["start"] < w["src_start"] - EPS or r["end"] > w["src_end"] + EPS:
            v.append(f"replacement range {i} [{r['start']}-{r['end']}] is outside the window [{w['src_start']}, {w['src_end']}]")
    return v


PRE_ROLL_MAX_S = 0.5


def pre_roll(r: dict, ns: float, words: list[dict]) -> dict:
    """A revise start the editor moved earlier than the snapped one, into the quiet just before the
    range's first word (no other word in between), is a deliberate pre-roll for a clipped word onset:
    the voice-band envelope can't see a hissy "T"/"s"/"f", so snapping and compaction would pull it
    back (2026-11-10, "Today" heard as "-day"). Keep it, at most PRE_ROLL_MAX_S before the first word,
    and hold the outer edges so compaction leaves them."""
    start = float(r["start"])
    if r.get("hold_edges") or start >= ns - 0.05:
        return r
    first = next((w for w in words if w["start"] >= start - EPS and (w["start"] + w["end"]) / 2 <= float(r["end"])), None)
    if first is None or first["start"] > ns + 0.25:   # snapping skipped a whole word: not a pre-roll
        return r
    return {**r, "start": round(max(start, first["start"] - PRE_ROLL_MAX_S), 3), "hold_edges": True}


def trust_audio_edges(start: float, end: float, ns: float, ne: float, words: list[dict], sil: dict,
                      r: dict) -> tuple[float, float]:
    """The editor measured her edges on the audio; snap_range only knows the transcript, which
    sometimes ends a word early ("point." ended 0.3 s before its sound did) or puts a phantom word
    in a silence. Keep her edge where the sound backs it up (user, 2026-11-03, tdevsetupmain1 v3):
    - end: snap pulled it in, but the sound runs past the snapped end -> end at the real fade +
      the body tail, never past her end or within NEIGHBOUR_GAP of the next word;
    - start: snap pulled it earlier into a transcript word, but that stretch is quiet -> keep her
      start (the word is mis-timed; the real onset is later).
    Ranges without an envelope are left as snapped. The refit, heal and clipped/pause checks still run."""
    if r.get("hold_edges"):
        # a user note asked for exactly these edges (the 2026-11-05 "project"/"new"/"there" notes:
        # the envelope read a soft word ending as quiet and kept pulling it back). Trust them.
        return round(start, 3), round(end, 3)
    env = (sil.get("envelopes") or {}).get(r.get("source"))
    if env is None:
        return ns, ne
    sec = ce.section(r, sil.get("intro_stems"))
    tail, lead = sil["tail"][sec], sil["lead"][sec]
    if end > ne + EPS:
        # the neighbour is the first word starting after HER end: a word her end falls inside was
        # stretched by the transcriber (HMJ v3: "one." 3372.61-3374.84, sound gone by 3373.0) and
        # snap dropped it whole; if it is really still audible at her end, clipped_edges rejects it
        nxt = min([w["start"] for w in words if w["start"] >= end - EPS], default=env.duration)
        off = env.offset(ne, min(end, nxt), edge=True)
        if off > ne + EPS:
            ne = round(min(end, off + tail, nxt - ce.NEIGHBOUR_GAP), 3)
    if start > ns + EPS and env.voiced(ns, max(ns, start - lead / 2), edge=True) < 0.03:
        ns = round(start, 3)
    return ns, max(ne, ns + 0.01)


def apply(E: Path, n: int, profile: dict | None = None):
    """Returns (ok, violations, edl, changes). Writes nothing unless ok."""
    E = Path(E)
    d = _rev_dir(E, n)
    wins = json.loads((d / "windows.json").read_text())
    meta = json.loads((d / "meta.json").read_text())
    base = load_version_edl(E, meta["of"])
    old = base["ranges"]
    stems_all = list(base.get("sources") or dict.fromkeys(r["source"] for r in old))
    prof = profile if profile is not None else ce.load_profile(None)
    viol: list[str] = []
    reps = {}
    for w in wins:
        p = d / f"window_{w['id']:02d}.edl.json"
        if not p.exists():
            viol.append(f"window {w['id']:02d}: missing {p.name}")
            continue
        try:
            rep = json.loads(p.read_text())
        except ValueError as ex:
            viol.append(f"window {w['id']:02d}: invalid JSON in {p.name} ({ex})")
            continue
        bad = _check_replacement(w, rep)
        viol += [f"window {w['id']:02d}: {x}" for x in bad]
        if not bad:
            reps[w["id"]] = rep
    if viol:
        return False, viol, None, None
    stems = list(dict.fromkeys(stems_all + [r["source"] for r in old]))
    _, words, durs, _ = ce._context(E, stems)
    sil = ce.silence_settings(prof, E)
    pad_lo, pad_hi = (x / 1000 for x in prof.get("edit", {}).get("pad_ms", [30, 200]))
    # splice, descending
    new = [dict(r) for r in old]
    spans = {}
    for w in sorted(wins, key=lambda x: -x["range_first"]):
        snapped = []
        for r in reps[w["id"]]:
            res = ce.snap_range(float(r["start"]), float(r["end"]), words.get(r["source"], []), pad_lo, pad_hi,
                                duration=durs.get(r["source"]))
            if res is None:
                continue  # no words survive: nothing to keep
            r = pre_roll(r, res[0], words.get(r["source"], []))
            ns, ne = trust_audio_edges(float(r["start"]), float(r["end"]), res[0], res[1],
                                       words.get(r["source"], []), sil, r)
            snapped.append({**r, "start": ns, "end": ne})
        olds = old[w["range_first"]:w["range_last"] + 1]
        pts = [x for r in snapped + olds for x in (r["start"], r["end"])]
        spans[w["id"]] = (w["source"], min(pts), max(pts))
        new[w["range_first"]:w["range_last"] + 1] = snapped
    if sil["policy"] != "keep_active":
        new, _notes = ce.refit_ranges(new, words, sil, stems)
    else:
        new = ce._join_touching(new, {s: i for i, s in enumerate(stems)})
    for r in new:
        r["start"], r["end"] = round(r["start"], 3), round(r["end"], 3)

    # a moved section (move_after) only belongs to a window whose own ranges were moved ones, even when
    # its source time falls inside the window's span (2026-11-10: it showed up as "restored" words)
    win_moved = {w["id"]: {ce._moved(x) for x in old[w["range_first"]:w["range_last"] + 1]} for w in wins}

    def touched(r, win_id):
        src, lo, hi = spans[win_id]
        return (r["source"] == src and r["start"] < hi - EPS and r["end"] > lo + EPS
                and ce._moved(r) in win_moved[win_id])

    def owner(i):
        return next((w["id"] for w in wins if touched(new[i], w["id"])), None)

    edl = {**base, "ranges": new, "total_duration_s": round(sum(r["end"] - r["start"] for r in new), 3)}
    for x in ce.validate_edl(edl, durs, None):
        viol.append(f"general: {x}")
    if not new:
        viol.append("general: the revised edl has no ranges")
    for c in ce.clipped_edges(new, words, sil):
        wid = owner(c["range_index"])
        if wid is not None:
            viol.append(f"window {wid:02d}: range {c['range_index']} {c['edge']} at {c['time']} cuts '{c['word']}' while still audible")
    for c in ce.long_silences(new, words, sil):
        wid = owner(c["range_index"])
        if wid is not None:
            viol.append(f"window {wid:02d}: range {c['range_index']} keeps a pause {c['start']}-{c['end']} longer than the limit")
    if viol:
        return False, viol, None, None
    fps = meta.get("fps") or _fps(E)
    nspans = out_spans(new, fps)
    changes = []
    for w in wins:
        idx = [i for i, r in enumerate(new) if touched(r, w["id"])]
        if idx:
            a, b = nspans[idx[0]][0], nspans[idx[-1]][1]
        else:  # replacement is empty: the cut happened here
            src, lo, _ = spans[w["id"]]
            before = [nspans[i][1] for i, r in enumerate(new)
                      if (stems.index(r["source"]), r["start"]) < (stems.index(src), lo)]
            a = b = before[-1] if before else 0.0
        changes.append({"window": w["id"], "notes": w["notes"],
                        "before": old[w["range_first"]:w["range_last"] + 1],
                        "after": [new[i] for i in idx],
                        "new_out_start": round(a, 3), "new_out_end": round(b, 3)})
    return True, [], edl, changes


# ---------------------------------------------------------------- report
def _plain(words_removed, words_restored) -> str:
    def phrase(ws):
        txt = " ".join(w["text"] for w in ws[:14])
        return f"\"{txt}{' ...' if len(ws) > 14 else ''}\""
    bits = []
    if words_removed:
        bits.append(f"removed {len(words_removed)} word{'s' if len(words_removed) != 1 else ''} {phrase(words_removed)}")
    if words_restored:
        bits.append(f"restored {len(words_restored)} word{'s' if len(words_restored) != 1 else ''} {phrase(words_restored)}")
    return "; ".join(bits) if bits else "no words changed (only the cut edges moved)"


def _append_lessons(path: Path, lines: list[str]):
    text = path.read_text() if path.exists() else "# Lessons\n\n## Approved\n\n## Proposed\n"
    have = set(text.splitlines())
    lines = [x for x in lines if x not in have]
    if not lines:
        return
    rows = text.split("\n")
    if "## Proposed" not in rows:
        rows += ["", "## Proposed"]
    i = rows.index("## Proposed") + 1
    j = next((k for k in range(i, len(rows)) if rows[k].startswith("## ")), len(rows))
    while j > i and rows[j - 1].strip() == "":
        j -= 1
    rows[j:j] = lines
    out = "\n".join(rows)
    path.write_text(out if out.endswith("\n") else out + "\n")


def _out_at(ranges: list[dict], spans: list[tuple[float, float]], src: str, t: float) -> float | None:
    """Output time of source time t (inside a kept range of src), else None."""
    for r, (a, _b) in zip(ranges, spans):
        if r["source"] == src and r["start"] - EPS <= t <= r["end"] + EPS:
            return a + max(0.0, t - r["start"])
    return None


def _edge_moves(before: list[dict], after: list[dict], old_ranges, old_spans, src, min_ms: float = 20.0):
    """Range edges that moved without adding or removing a word: [(out time in the old video, text)]."""
    out = []
    for b in before:
        a = next((x for x in after if x["source"] == b["source"] and x["start"] < b["end"] and x["end"] > b["start"]), None)
        if a is None:
            continue
        for edge, label in (("start", "starts"), ("end", "ends")):
            dt = a[edge] - b[edge]
            if abs(dt) * 1000 >= min_ms:
                way = ("earlier" if dt < 0 else "later")
                t = _out_at(old_ranges, old_spans, src, b[edge] if edge == "start" else max(b["start"], b[edge] - EPS))
                if t is not None:
                    out.append((t, f"a cut edge moved: the piece {label} {abs(dt):.2f} s {way}"))
    return out


def report(E: Path, n: int, lessons: Path | None = None, today: str | None = None) -> str:
    E = Path(E)
    d = _rev_dir(E, n)
    wins = {w["id"]: w for w in json.loads((d / "windows.json").read_text())}
    changes = json.loads((d / "changes.json").read_text())
    meta = json.loads((d / "meta.json").read_text())
    stems = list(dict.fromkeys(r["source"] for c in changes for r in c["before"] + c["after"]))
    words = _words_in(E, stems)
    fps = meta.get("fps") or _fps(E)
    old_r = load_version_edl(E, meta["of"])["ranges"]
    new_r = load_version_edl(E, n)["ranges"]
    old_sp, new_sp = out_spans(old_r, fps), out_spans(new_r, fps)
    today = today or date.today().isoformat()
    run = E.resolve().parent.name
    md = [f"# Revision v{n} (from v{meta['of']})", "",
          "Each note is listed with what changed and where it now is in the new video.", ""]
    lessons_lines = []
    for c in changes:
        w = wins[c["window"]]
        ws = words.get(w["source"], [])
        lo, hi = w["src_start"], w["src_end"]
        kb = _kept(ws, c["before"], w["source"])
        ka = _kept(ws, c["after"], w["source"])
        removed = [x for x, b, a in zip(ws, kb, ka) if b and not a and lo <= x["start"] <= hi]
        restored = [x for x, b, a in zip(ws, kb, ka) if a and not b and lo <= x["start"] <= hi]
        where_all = f"now at {fmt_tc(c['new_out_start'])}-{fmt_tc(c['new_out_end'])}" if c["after"] else \
            f"the cut is now at {fmt_tc(c['new_out_start'])}"
        notes = c["notes"]
        # several notes in one window: give each note only the changes nearest to it (output time)
        per = {i: {"removed": [], "restored": [], "edges": [], "at": []} for i in range(len(notes))}
        src = w["source"]

        def nearest(t):
            return min(range(len(notes)), key=lambda i: abs(notes[i]["tc_s"] - t)) if t is not None else 0

        for x in removed:
            t = _out_at(old_r, old_sp, src, (x["start"] + x["end"]) / 2)
            i = nearest(t)
            per[i]["removed"].append(x)
            per[i]["at"].append(_out_at(new_r, new_sp, src, x["start"]) or t)
        for x in restored:
            t = _out_at(new_r, new_sp, src, (x["start"] + x["end"]) / 2)
            i = nearest(t)
            per[i]["restored"].append(x)
            per[i]["at"].append(t)
        moves = _edge_moves(c["before"], c["after"], old_r, old_sp, src)
        big = [m for m in moves if float(m[1].split(" s ")[0].split()[-1]) >= 0.25]
        for t, txt in big or sorted(moves, key=lambda m: -float(m[1].split(" s ")[0].split()[-1]))[:1]:
            i = nearest(t)
            per[i]["edges"].append(txt)
            per[i]["at"].append(t)
        for i, nt in enumerate(notes):
            pi = per[i]
            if pi["removed"] or pi["restored"]:
                what = _plain(pi["removed"], pi["restored"])
            elif pi["edges"]:
                what = "no words added or removed; " + "; ".join(dict.fromkeys(pi["edges"]))
            else:
                what = _plain([], [])
            ats = [t for t in pi["at"] if t is not None]
            where = f"around {fmt_tc(min(ats))}" if ats and len(notes) > 1 else where_all
            md += [f"- **{note_tc(nt)}** \"{nt['text']}\"", f"  - Changed: {what}.", f"  - Where: {where}."]
            lessons_lines.append(f"- [{today} {run} v{n}] \"{nt['text']}\" -> {what}")
    md += ["", "## Checks", "- Splice, edge repair, clipped-word and long-pause checks on the changed parts: passed."]
    chk = E / "verify" / "checks.json"
    if chk.exists():
        try:
            md.append(f"- Full-video checks (verify_edit): {json.loads(chk.read_text()).get('overall', 'unknown')}.")
        except ValueError:
            md.append("- Full-video checks: checks.json unreadable.")
    else:
        md.append("- Full-video checks: not run yet.")
    text = "\n".join(md) + "\n"
    (d / "REVISION.md").write_text(text)
    if lessons is not None:
        _append_lessons(Path(lessons), lessons_lines)
    return text


# ---------------------------------------------------------------- CLI
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    a1 = sub.add_parser("start")
    a1.add_argument("--edit-dir", required=True, type=Path)
    a1.add_argument("--notes", required=True, type=Path)
    a1.add_argument("--of", type=int, default=None)
    a1.add_argument("--window-s", type=float, default=60.0)
    a2 = sub.add_parser("apply")
    a2.add_argument("--edit-dir", required=True, type=Path)
    a2.add_argument("--version", required=True, type=int)
    a2.add_argument("--profile", type=Path, default=None)
    a3 = sub.add_parser("report")
    a3.add_argument("--edit-dir", required=True, type=Path)
    a3.add_argument("--version", required=True, type=int)
    a3.add_argument("--lessons", type=Path, default=None)
    a = ap.parse_args(argv)
    E = a.edit_dir
    if a.cmd == "start":
        try:
            start(E, a.notes, a.of, a.window_s)
        except ValueError as ex:
            print(f"notes error: {ex}", file=sys.stderr)
            return 1
        return 0
    if a.cmd == "apply":
        ok, viol, edl, changes = apply(E, a.version, ce.load_profile(a.profile))
        if not ok:
            print(f"apply FAILED: {len(viol)} violation(s); edl.json untouched", file=sys.stderr)
            for x in viol:
                print(f"  - {x}", file=sys.stderr)
            return 1
        d = _rev_dir(E, a.version)
        body = json.dumps(edl, indent=2) + "\n"
        (d / "edl.json").write_text(body)
        (d / "changes.json").write_text(json.dumps(changes, indent=2) + "\n")
        (E / "edl.json").write_text(body)
        print(f"v{a.version} OK: {len(edl['ranges'])} ranges, {edl['total_duration_s']:.1f}s -> {E / 'edl.json'}")
        return 0
    print(report(E, a.version, a.lessons))
    return 0


if __name__ == "__main__":
    sys.exit(main())
