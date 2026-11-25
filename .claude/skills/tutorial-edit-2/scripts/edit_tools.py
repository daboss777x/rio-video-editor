#!/usr/bin/env python3
"""Small, deterministic views over a run's files, so the agents never read big JSON.

Run from the project root:
    .venv/bin/python .claude/skills/tutorial-edit-2/scripts/edit_tools.py <cmd> [args]

Commands:
    lessons                              print only the `## Approved` section of history/LESSONS.md
    activity --edit-dir E [--stem CLIP] [--from S --to T]
                                         gap counts/seconds per class (all clips, or one with --stem);
                                         with a window (needs --stem), one line per gap
    kept --edit-dir E [--edl F]          the kept transcript in OUTPUT order, one line per EDL range,
                                         with the dropped words between close ranges; `~` = squashed timing
    joins --edit-dir E [--n 15]          the joins to spot-check: first 3, last 3, every cut in running
                                         sound (up to 20 in all), then the riskiest rest,
                                         each with a ready timeline_view command on final.mp4
    out2src --edit-dir E <output_s>      which EDL range an output time falls in, and its source time
    stats --edit-dir E                   the numbers REPORT.md needs, as markdown
    loud --edit-dir E --stem CLIP --from S --to T
                                         the clip's real loudness (envelope) in a window of at most 30 s,
                                         one char per 0.1 s, with the transcript words and detected
                                         silences in it: where the speech really is

Exits non-zero with a one-line reason when an input it needs is missing.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]  # shared project root (history/, vendor/, runs/)
CODE = ROOT / "rio2"  # the system this skill drives (tutorial-edit's copy says rio1)
sys.path.insert(0, str(CODE))
from helpers import chunk_edl as ce  # noqa: E402  (reuse the contract loaders)

TIMELINE = "vendor/video-use/helpers/timeline_view.py"


class Fail(Exception):
    pass


def load_json(p: Path):
    if not p.exists():
        raise Fail(f"missing {p}")
    return json.loads(p.read_text())


def tc(s: float) -> str:
    s = max(0.0, float(s))
    m, sec = divmod(s, 60)
    return f"{int(m):02d}:{sec:04.1f}"


def ranges_with_output(edl: dict) -> list[dict]:
    out, t = [], 0.0
    for i, r in enumerate(edl.get("ranges", [])):
        d = float(r["end"]) - float(r["start"])
        out.append({**r, "i": i, "out_start": t, "out_end": t + d})
        t += d
    return out


def gaps(E: Path, stem: str | None = None) -> list[dict]:
    """Gaps of one clip (stem), or of every clip in join order when stem is None.
    Multi-clip runs keep E/activity/<stem>.json; old runs have E/activity.json."""
    if stem is None:
        out = []
        for st in ce.load_sources(E)["order"] or [None]:
            act = ce.load_activity(E, st)
            if act is not None:
                out += act.get("gaps", [])
                if st is None or not (E / "activity" / f"{st}.json").exists():
                    break  # old single-clip activity.json: counted once, not once per clip
        if out or (E / "activity.json").exists() or (E / "activity").is_dir():
            return out
        raise Fail(f"missing {E / 'activity'}/<clip>.json")
    act = ce.load_activity(E, stem)
    if act is None:
        raise Fail(f"missing {E / 'activity' / (stem + '.json')}")
    return act.get("gaps", [])


# ---------------------------------------------------------------- commands
def cmd_lessons(_a) -> None:
    p = ROOT / "history" / "LESSONS.md"
    if not p.exists():
        print("(no history/LESSONS.md: no approved lessons)")
        return
    text = re.sub(r"<!--.*?-->", "", p.read_text(), flags=re.S)
    m = re.search(r"^## Approved\s*$(.*?)(?=^## |\Z)", text, flags=re.M | re.S)
    body = (m.group(1).strip() if m else "")
    print(body or "(no approved lessons)")


def cmd_activity(a) -> None:
    stem = a.stem
    if stem is None and (a.from_ is not None or a.to is not None):
        order = ce.load_sources(a.edit_dir)["order"]
        if len(order) > 1:
            raise Fail("--from/--to are source times: pass --stem <clip> too")
        stem = order[0] if order else None
    gs = gaps(a.edit_dir, stem)
    if a.from_ is not None or a.to is not None:
        lo = a.from_ if a.from_ is not None else float("-inf")
        hi = a.to if a.to is not None else float("inf")
        for g in gs:
            if g["end"] >= lo and g["start"] <= hi:
                print(f"[{g['start']:.2f}-{g['end']:.2f}] {g['class']} must_keep={g.get('must_keep')}")
        return
    agg: dict[str, list[float]] = {}
    for g in gs:
        agg.setdefault(g["class"], []).append(g["end"] - g["start"])
    print(f"{len(gs)} gaps")
    for k in ("static", "typing", "active"):
        v = agg.get(k, [])
        print(f"  {k:7s} {len(v):4d} gaps  {sum(v):8.1f} s")
    act = sorted((g for g in gs if g["class"] == "active"), key=lambda g: g["start"] - g["end"])[:10]
    if act:
        print("longest active gaps:")
        for g in act:
            print(f"  [{g['start']:.2f}-{g['end']:.2f}] must_keep={g.get('must_keep')}")


def _squashed_marks(words: list[dict]) -> set[int]:
    """Indices of words in runs of 3+ words whose timings were squashed (<= 45 ms each)."""
    out, run = set(), []
    for i, w in enumerate(words + [{"start": 0, "end": 1}]):
        if w["end"] - w["start"] <= ce.SQUASHED_S:
            run.append(i)
        else:
            if len(run) >= 3:
                out.update(run)
            run = []
    return out


def kept_text(words: list[dict], s: float, e: float, marks: set[int] | None = None) -> str:
    return " ".join(w["text"] + ("~" if marks and i in marks else "")
                    for i, w in enumerate(words) if s <= (w["start"] + w["end"]) / 2 <= e)


def cmd_kept(a) -> None:
    """With --edl (a chunk's edl_NN.json array, or any EDL), the editor's read-through. Between two
    ranges of the same clip less than 4 s apart it also prints the words that were dropped."""
    E = a.edit_dir
    edl = load_json(Path(a.edl) if a.edl else E / "edl.json")
    if isinstance(edl, list):
        edl = {"ranges": edl}
    cache: dict[str, tuple[list[dict], set[int]]] = {}
    prev = None
    for r in ranges_with_output(edl):
        if r["source"] not in cache:
            ws = ce.load_words(E, r["source"])
            cache[r["source"]] = (ws, _squashed_marks(ws))
        words, marks = cache[r["source"]]
        if prev and prev["source"] == r["source"] and 0 < r["start"] - prev["end"] < 4.0:
            gone = kept_text(words, prev["end"], r["start"], marks)
            if gone:
                print(f"      ✂ dropped: {gone}")
        print(f"#{r['i']} out {tc(r['out_start'])}-{tc(r['out_end'])} | src {r['start']:.2f}-{r['end']:.2f} "
              f"| {r['source']}{' INSERT' if r.get('insert') else (' FACE' if r.get('layout') == 'face' else '')} | {r.get('beat', '')} | {kept_text(words, r['start'], r['end'], marks) or '(no speech)'}")
        prev = r


def cmd_loud(a) -> None:
    """Where the sound really is, next to where the transcript says the words are. AssemblyAI can
    put a word inside a pause the audio shows as quiet; the envelope is the truth."""
    E, stem = a.edit_dir, a.stem
    lo, hi = float(a.from_), float(a.to)
    if not hi > lo:
        raise Fail("--to must be after --from")
    hi = min(hi, lo + 30.0)
    env = ce.load_envelope(E, stem)
    if env is None:
        raise Fail(f"missing or unreadable {E / 'silences' / (stem + '.envelope.json')}")
    step = 0.1
    cells = []
    t = lo
    while t < hi - 1e-9:
        fr = env.db[env._i(t):env._i(min(t + step, hi))]
        m = max(fr) if fr else -999
        cells.append("#" if m > env.loud else "+" if m > env.thr else ".")
        t += step
    print(f"loudness {stem} {lo:.2f}-{hi:.2f} s: one char per 0.1 s, '#' speech, '+' soft sound "
          f"(word endings, breaths, clicks), '.' quiet")
    for i in range(0, len(cells), 20):
        print(f"  {lo + i * step:8.2f} |{''.join(cells[i:i + 20])}|")
    ws = [w for w in ce.load_words(E, stem) if w["end"] > lo and w["start"] < hi]
    print("words: " + (" ".join(f"{w['text']}@{w['start']:.2f}-{w['end']:.2f}" for w in ws) or "(none)"))
    sil = [s for s in ce.load_silences(E, stem) if s[1] > lo and s[0] < hi]
    print("detected silences: " + (", ".join(f"{s0:.2f}-{s1:.2f}" for s0, s1 in sil) or "(none)"))


def cmd_edges(a) -> None:
    """Every edge verify flagged (cut_in_sound, clipped_words), one line each, for Ines to give a call
    on: its id, output time, the words either side of the cut and the sound 1 s either side
    ('#' speech, '+' soft, '.' quiet, '|' the cut; the kept side is after '|' for a start, before it
    for an end)."""
    E = a.edit_dir
    checks = load_json(E / "verify" / "checks.json")
    edl = load_json(E / "edl.json")
    ranges = edl.get("ranges", [])
    flagged = [(c["name"], e) for c in checks.get("checks", []) if c.get("name") in ("cut_in_sound", "clipped_words")
               for e in (c.get("data") or {}).get("edges") or []]
    envs: dict = {}
    words: dict = {}
    seen = set()
    for name, e in flagged:
        eid = e.get("id") or f"r{e['range_index']}.{e['edge']}"
        if eid in seen:
            continue
        seen.add(eid)
        i = e["range_index"]
        stem = ranges[i]["source"] if i < len(ranges) else "?"
        t = float(e.get("time", ranges[i]["start" if e["edge"] == "start" else "end"] if i < len(ranges) else 0))
        if stem not in envs:
            envs[stem] = ce.load_envelope(E, stem)
            words[stem] = ce.load_words(E, stem) if (E / "transcripts" / f"{stem}.json").exists() else []
        env, ws = envs[stem], words[stem]
        strip = ""
        if env is not None:
            for k in range(-10, 10):
                lo = t + k * 0.1
                fr = env.db[env._i(max(lo, 0)):env._i(max(lo + 0.1, 0))] if lo + 0.1 > 0 else []
                m = max(fr) if fr else -999
                strip += ("|" if k == 0 else "") + ("#" if m > env.loud else "+" if m > env.thr else ".")
        before = " ".join([w["text"] for w in ws if w["end"] <= t + 0.05 and w["end"] > t - 2.0][-3:])
        after = " ".join([w["text"] for w in ws if w["start"] >= t - 0.05 and w["start"] < t + 2.0][:3])
        out = f" out {tc(float(e['output_s']))}" if e.get("output_s") is not None else ""
        extra = f" word '{e['word']}'" if e.get("word") else ""
        print(f"{eid}{out} {name} src {t:.2f} {stem}{extra} | ...{before} / {after}... [{strip}]")
    print(f"{len(seen)} flagged edge(s): give each a call in verdict.json \"edges\"")


def cmd_joins(a) -> None:
    E = a.edit_dir
    edl = load_json(E / "edl.json")
    rs = ranges_with_output(edl)
    try:  # cuts made while the sound keeps going (same measure as verify's cut_in_sound)
        sil = ce.silence_settings(ce.load_profile(None), E)
        in_sound = {(c["range_index"], c["edge"]) for c in ce.cuts_in_sound(edl.get("ranges", []), sil)}
    except Exception:  # noqa: BLE001  (no envelopes: skip this risk factor)
        in_sound = set()
    gcache: dict[str, list[dict]] = {}
    wcache: dict[str, list[dict]] = {}

    def gs_of(st):
        if st not in gcache:
            try:
                gcache[st] = gaps(E, st)
            except Fail:
                gcache[st] = []
        return gcache[st]
    joins = []
    for k in range(1, len(rs)):
        p, r = rs[k - 1], rs[k]
        gs = gs_of(r["source"])
        words = wcache.setdefault(r["source"], ce.load_words(E, r["source"]))
        marks = [x for g in gs if g["class"] == "active" and g.get("must_keep") for x in g["must_keep"]]
        cut_s, cut_e = float(p["end"]), float(r["start"])
        risk, why = 0, []
        if any(abs(x - cut_s) < 0.3 or abs(x - cut_e) < 0.3 for x in marks):
            risk += 2; why.append("active edge")
        if (k - 1, "end") in in_sound or (k, "start") in in_sound:
            risk += 3; why.append("cut in running sound")
        if min(p["end"] - p["start"], r["end"] - r["start"]) < 2.0:
            risk += 1; why.append("short range")
        if p["source"] == r["source"]:
            removed = cut_e - cut_s
            if removed > 30:
                risk += 1; why.append(f"big cut {removed:.0f}s")
            if 0 < removed < 0.3:
                risk += 1; why.append("tiny cut")
            if any(g["class"] == "typing" and g["start"] < cut_e and g["end"] > cut_s for g in gs):
                risk += 1; why.append("typing trimmed")
            gone = {w["text"].lower().strip(".,!?") for w in words if cut_s <= w["start"] < cut_e}
            nxt = [w["text"].lower().strip(".,!?") for w in words if w["start"] >= cut_e][:4]
            if len(nxt) >= 3 and sum(t in gone for t in nxt) >= 3:
                risk += 2; why.append("retake swap?")
        else:
            removed = None
            risk += 1; why.append("source change")
        joins.append({"k": k, "out": r["out_start"], "cut": (cut_s, cut_e), "removed": removed,
                      "risk": risk, "why": why})
    n = max(a.n, 0)
    if len(joins) <= n:
        pick = joins
    else:
        ends = joins[:3] + joins[-3:]
        ids = {j["k"] for j in ends}
        rest = sorted((j for j in joins if j["k"] not in ids), key=lambda j: (-j["risk"], j["k"]))
        # every cut in running sound is looked at (the likeliest clipped words), up to 20 views
        must = [j for j in rest if "cut in running sound" in j["why"]]
        others = [j for j in rest if "cut in running sound" not in j["why"]]
        room = max(min(max(n, len(ends) + len(must)), 20) - len(ends), 0)
        chosen = (must + others)[:room]
        pick = sorted(ends + chosen, key=lambda j: j["k"])
    print(f"{len(joins)} joins; checking {len(pick)}")
    for j in pick:
        rem = f"{j['removed']:.2f}s" if j["removed"] is not None else "n/a"
        lo, hi = max(0.0, j["out"] - 2.0), j["out"] + 2.0
        print(f"join {j['k']} (#{j['k'] - 1}->#{j['k']}) out {tc(j['out'])} ({j['out']:.2f}s) "
              f"src cut {j['cut'][0]:.2f}-{j['cut'][1]:.2f} removed {rem} risk {j['risk']} {','.join(j['why'])}")
        png = E / "verify" / "views" / f"join_{j['k']:03d}.png"
        print(f"  view: .venv/bin/python {TIMELINE} \"{E / 'final.mp4'}\" {lo:.2f} {hi:.2f} "
              f"-o \"{png}\" --n-frames 6")
        if a.render:
            # One Bash call renders every picked join (one at a time, low RAM): no shell loops,
            # which the headless run's permission check denies.
            png.parent.mkdir(parents=True, exist_ok=True)
            r = subprocess.run([sys.executable, TIMELINE, str(E / "final.mp4"), f"{lo:.2f}", f"{hi:.2f}",
                                "-o", str(png), "--n-frames", "6"], capture_output=True, text=True)
            print(f"  rendered: {png}" if r.returncode == 0 and png.exists()
                  else f"  render FAILED: {(r.stderr or '').strip()[-200:]}")


def cmd_out2src(a) -> None:
    for r in ranges_with_output(load_json(a.edit_dir / "edl.json")):
        if r["out_start"] <= a.t <= r["out_end"]:
            print(f"range #{r['i']} source {r['source']} t={r['start'] + a.t - r['out_start']:.3f} "
                  f"(range {r['start']:.3f}-{r['end']:.3f})")
            return
    raise Fail(f"output time {a.t} is past the end of the edit")


def ffprobe_duration(p: Path) -> float | None:
    if not p.exists():
        return None
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(p)],
                         capture_output=True, text=True)
    try:
        return float(out.stdout.strip())
    except ValueError:
        return None


def cmd_stats(a) -> None:
    E = a.edit_dir
    edl = load_json(E / "edl.json")
    src = float(load_json(E / "probe.json")["duration_s"])
    rs = edl.get("ranges", [])
    final = ffprobe_duration(E / "final.mp4")
    kept = sum(float(r["end"]) - float(r["start"]) for r in rs)
    cuts = max(len(rs) - 1, 0) + (1 if rs and float(rs[0]["start"]) > 0.05 else 0) \
        + (1 if rs and src - float(rs[-1]["end"]) > 0.05 else 0)
    usd_h = ce.load_profile().get("estimator_priors", {}).get("scribe_usd_per_hour", 0.22)
    print("| Metric | Value |\n|---|---|")
    print(f"| Source duration | {tc(src)} ({src:.1f} s) |")
    print(f"| Final duration | {tc(final) + f' ({final:.1f} s)' if final else 'final.mp4 missing'} |")
    print(f"| EDL duration | {kept:.1f} s |")
    print(f"| Kept ratio (output / source) | {kept / src:.1%} |" if src else "| Kept ratio | n/a |")
    print(f"| Ranges kept | {len(rs)} |")
    print(f"| Cuts | {cuts} |")
    print(f"| Scribe cost | ${src / 3600 * usd_h:.2f} ({src / 3600:.2f} h x ${usd_h}/h) |")
    ch = E / "verify" / "checks.json"
    if ch.exists():
        c = json.loads(ch.read_text())
        print(f"\nChecks: **{c.get('overall')}**\n\n| Check | Status | Detail |\n|---|---|---|")
        for x in c.get("checks", []):
            print(f"| {x.get('name')} | {x.get('status')} | {str(x.get('detail', '')).replace('|', '/')} |")
    v = E / "verify" / "verdict.json"
    if v.exists():
        d = json.loads(v.read_text())
        print(f"\nVerifier verdict: **{d.get('verdict')}** ({len(d.get('problems', []))} problem(s))")
        for p in d.get("problems", []):
            print(f"- #{p.get('rank')} @{p.get('output_tc')}: {p.get('evidence')}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("lessons")
    for name in ("activity", "kept", "joins", "out2src", "stats", "loud", "edges"):
        p = sub.add_parser(name)
        p.add_argument("--edit-dir", required=True, type=Path)
        if name == "activity":
            p.add_argument("--stem", default=None, help="clip stem (default: all clips for the summary)")
            p.add_argument("--from", dest="from_", type=float, default=None)
            p.add_argument("--to", type=float, default=None)
        if name == "joins":
            p.add_argument("--n", type=int, default=15)
            p.add_argument("--render", action="store_true", help="also render each picked join's PNG")
        if name == "out2src":
            p.add_argument("t", type=float)
        if name == "loud":
            p.add_argument("--stem", required=True, help="clip stem")
            p.add_argument("--from", dest="from_", type=float, required=True)
            p.add_argument("--to", type=float, required=True)
        if name == "kept":
            p.add_argument("--edl", default=None, help="read this EDL (e.g. E/chunks/edl_03.json) instead of E/edl.json")
    a = ap.parse_args(argv)
    try:
        {"lessons": cmd_lessons, "activity": cmd_activity, "kept": cmd_kept, "joins": cmd_joins,
         "out2src": cmd_out2src, "stats": cmd_stats, "loud": cmd_loud,
         "edges": cmd_edges}[a.cmd](a)
    except Fail as e:
        print(f"edit_tools: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
