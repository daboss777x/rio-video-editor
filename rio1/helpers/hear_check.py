#!/usr/bin/env python3
"""Listen back: transcribe the finished video and compare it, word for word, with what the EDL
meant to keep (user, 2026-11-03: Ines missed a chopped "approval" and a stray "ah" because she
can only see frames).

    .venv/bin/python rio1/helpers/hear_check.py --edit-dir E [--final F] [--no-transcribe]

Transcribes F (default E/final.mp4) with helpers/stt/transcribe.py into E/verify/hear/ (AssemblyAI,
about $0.02 per 5 output minutes; re-done whenever final.mp4 changes), then diffs the heard words
against the expected kept words (EDL ranges over the source transcripts, in output order).

Writes E/verify/heard.json and the short E/verify/heard.md for the verifier. A difference within
JOIN_S of a join is the likely sign of a clipped word ("approval" missing) or a stray sound left
at a cut ("ah" heard extra); differences elsewhere are mostly the transcriber hearing the same
speech slightly differently, so they are only counted. Never fails the run: if transcription is
impossible it says so and exits 0.
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from helpers import chunk_edl as ce  # noqa: E402

JOIN_S = 0.6


def norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def tc(s: float) -> str:
    m, sec = divmod(max(0.0, s), 60)
    return f"{int(m)}:{sec:04.1f}"


def expected_words(E: Path, ranges: list[dict]) -> tuple[list[dict], list[float]]:
    """Kept source words in output order, each with its output time; and the output join times."""
    out, joins, t, cache = [], [], 0.0, {}
    for i, r in enumerate(ranges):
        if i:
            joins.append(t)
        ws = cache.setdefault(r["source"], ce.load_words(E, r["source"]))
        for w in ws:
            if r["start"] - ce.EPS <= (w["start"] + w["end"]) / 2 <= r["end"] + ce.EPS and norm(w["text"]):
                out.append({"text": w["text"], "t": round(t + max(w["start"] - r["start"], 0.0), 2)})
        t += r["end"] - r["start"]
    return out, joins


def compare(expected: list[dict], heard: list[dict], joins: list[float]) -> dict:
    a = [norm(w["text"]) for w in expected]
    b = [norm(w["text"]) for w in heard]
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    at_join, elsewhere = [], []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            continue
        exp = [w["text"] for w in expected[i1:i2]]
        got = [w["text"] for w in heard[j1:j2]]
        t = expected[i1]["t"] if i2 > i1 else (heard[j1]["t"] if j2 > j1 else 0.0)
        if i2 > i1 and j2 > j1:
            t = min(t, heard[j1]["t"])
        near = min(joins, key=lambda x: abs(x - t)) if joins else None
        kind = {"delete": "missing", "insert": "extra", "replace": "changed"}[op]
        d = {"output_s": t, "kind": kind, "expected": " ".join(exp), "heard": " ".join(got)}
        if near is not None and abs(near - t) <= JOIN_S + 0.4 * max(len(exp), len(got), 1):
            d["join_s"] = round(near, 2)
            at_join.append(d)
        else:
            elsewhere.append(d)
    return {"match_ratio": round(sm.ratio(), 4), "n_expected": len(a), "n_heard": len(b),
            "at_joins": at_join, "elsewhere_count": len(elsewhere), "elsewhere": elsewhere[:30]}


def syllables(text: str) -> int:
    return max(1, len(re.findall(r"[aeiouy]+", norm(text))))


def short_at_joins(heard: list[dict], joins: list[float], per_syllable: float = 0.045) -> list[dict]:
    """The one heard word on each side of every cut, when it is far too short for its syllables: a
    chopped word still gets transcribed whole, but squashed (v1 of 2026-11-03: 'approval.' 0.12 s,
    the 'I' of 'I paused' 0.02 s). Needs start/end on the heard words."""
    found, seen = [], set()
    ws = [w for w in heard if "end" in w]
    for j in joins:
        before = [w for w in ws if w["t"] < j - 0.05 and w["t"] >= j - JOIN_S - 1.0]
        after = [w for w in ws if j - 0.05 <= w["t"] <= j + JOIN_S]
        for w in ([before[-1]] if before else []) + ([after[0]] if after else []):
            dur = w["end"] - w["t"]
            if id(w) not in seen and dur < per_syllable * syllables(w["text"]):
                seen.add(id(w))
                found.append({"output_s": w["t"], "kind": "short", "expected": w["text"],
                              "heard": f"{w['text']} ({dur * 1000:.0f} ms)", "join_s": round(j, 2)})
    return found


def ensure_transcript(final: Path, hear: Path) -> tuple[bool, str]:
    tr = hear / "transcripts" / f"{final.stem}.json"
    stamp = hear / "final.stamp"
    st = final.stat()
    key = f"{st.st_size}:{int(st.st_mtime)}"
    if tr.exists() and (not stamp.exists() or stamp.read_text().strip() != key):
        tr.unlink()  # a re-render: listen again
        (hear / "transcripts" / f"{final.stem}.assemblyai.json").unlink(missing_ok=True)
    from helpers.stt import transcribe
    rc = transcribe.main([str(final), "--edit-dir", str(hear)])
    if rc != 0 or not tr.exists():
        return False, f"transcription failed (exit {rc})"
    hear.mkdir(parents=True, exist_ok=True)
    stamp.write_text(key)
    return True, "ok"


def write_md(path: Path, res: dict) -> None:
    lines = ["# Listen-back (the finished video, transcribed and compared with the EDL)", ""]
    if res.get("skipped"):
        lines.append(f"Skipped: {res['skipped']}. Judge by clipped_words and cut_in_sound only.")
    else:
        lines.append(f"Words expected {res['n_expected']}, heard {res['n_heard']}, match {res['match_ratio']:.1%}.")
        lines.append("")
        if res["at_joins"]:
            lines.append(f"## At joins ({len(res['at_joins'])}): likely clipped words or stray sounds, check each ('short' = heard, but squashed: maybe chopped)")
            for d in res["at_joins"]:
                lines.append(f"- {tc(d['output_s'])} (join {tc(d['join_s'])}) {d['kind']}: "
                             f"expected '{d['expected']}' heard '{d['heard']}'")
        else:
            lines.append("## At joins: none. Every word at every cut was heard as expected.")
        lines += ["", f"Elsewhere: {res['elsewhere_count']} small difference(s) away from any cut "
                      "(usually the transcriber hearing the same speech differently; ignore unless a step is missing)."]
    path.write_text("\n".join(lines) + "\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Transcribe the finished video and diff it against the EDL")
    ap.add_argument("--edit-dir", required=True, type=Path)
    ap.add_argument("--final", type=Path, default=None)
    ap.add_argument("--edl", type=Path, default=None, help="the EDL the video was rendered from (default E/edl.json)")
    ap.add_argument("--out", type=Path, default=None, help="where heard.json/heard.md go (default E/verify)")
    ap.add_argument("--no-transcribe", action="store_true", help="use the existing listen-back transcript only")
    a = ap.parse_args(argv)
    E = a.edit_dir
    final = a.final or E / "final.mp4"
    hear = (a.out or E / "verify") / "hear"
    od = a.out or E / "verify"
    out_json, out_md = od / "heard.json", od / "heard.md"
    out_json.parent.mkdir(parents=True, exist_ok=True)
    res: dict
    if not final.exists():
        res = {"skipped": f"{final} not found"}
    else:
        ok, why = (True, "ok") if a.no_transcribe else ensure_transcript(final, hear)
        heard_raw = ce.load_words(hear, final.stem) if ok else []
        if not ok or not heard_raw:
            res = {"skipped": why if not ok else "no listen-back transcript"}
        else:
            edl = json.loads((a.edl or E / "edl.json").read_text())
            expected, joins = expected_words(E, edl.get("ranges", []))
            heard = [{"text": w["text"], "t": round(w["start"], 2), "end": round(w["end"], 3)}
                     for w in heard_raw if norm(w["text"])]
            res = compare(expected, heard, joins)
            res["at_joins"] = sorted(res["at_joins"] + short_at_joins(heard, joins), key=lambda d: d["output_s"])
    out_json.write_text(json.dumps(res, indent=1))
    write_md(out_md, res)
    if res.get("skipped"):
        print(f"hear_check: skipped ({res['skipped']})")
    else:
        print(f"hear_check: match {res['match_ratio']:.1%}, {len(res['at_joins'])} difference(s) at joins, "
              f"{res['elsewhere_count']} elsewhere -> {out_md}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
