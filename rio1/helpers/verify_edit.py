"""Deterministic verification of a rendered edit.

CLI: rio1/helpers/verify_edit.py --edit-dir E [--profile P] [--final F]
Writes E/verify/checks.json:
  {"overall": "PASS|FAIL", "checks": [{"name","status","detail","data"}]}
Checks: duration, no_long_silence (or step_coverage when edit.silence_policy is keep_active),
word_boundaries (an edge inside a detected silence, or where the loudness envelope is quiet, is fine),
clipped_words (an edge that cuts a kept word before it fades; needs E/silences/<stem>.envelope.json),
audio_peak, av_sync, cut_ratio.
Exit code is 0 whenever checks.json was written (read `overall`); non-zero only if inputs are unusable.

cut_ratio and cut_in_sound are ADVISORY: status FAIL means "flag" and does not change `overall`.
cut_ratio = kept fraction = sum(ranges) / source duration, flagged outside profile verify.cut_ratio_flag.

Public API: run_checks(edit_dir, profile, final=None) -> dict
Media functions (`probe`, `loudness`, `av_durations`) are thin wrappers over helpers.media.checks so tests can monkeypatch them.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from helpers import chunk_edl as ce  # noqa: E402

EPS = 0.002


def probe(path):
    from helpers.media import checks
    return checks.probe(str(path))


def loudness(path):
    from helpers.media import checks
    return checks.loudness(str(path))


def av_durations(path):
    from helpers.media import checks
    return checks.av_durations(str(path))


def _check(name, ok, detail, data=None, advisory=False):
    c = {"name": name, "status": "PASS" if ok else "FAIL", "detail": detail, "data": data or {}}
    if advisory:
        c["data"]["advisory"] = True
    return c


def run_checks(edit_dir: Path, profile: dict, final: Path | None = None) -> dict:
    E = Path(edit_dir)
    final = Path(final) if final else E / "final.mp4"
    vp = profile.get("verify", {})
    edl = json.loads((E / "edl.json").read_text())
    ranges = edl["ranges"]
    expected = sum(r["end"] - r["start"] for r in ranges)
    stems = list(dict.fromkeys(list(edl.get("sources") or []) + [r["source"] for r in ranges]))
    src_order = ce.load_sources(E)["order"]
    stems = [x for x in src_order if x in stems] + [x for x in stems if x not in src_order]
    activity, words, durs, only = ce._context(E, stems)
    acts = ce.activities(E, stems)
    per_clip = {st: {"kept_s": round(sum(r["end"] - r["start"] for r in ranges if r["source"] == st), 3),
                     "source_s": durs.get(st), "words": len(words.get(st, []))} for st in stems}
    checks: list[dict] = []

    # duration
    tol = float(vp.get("duration_tolerance_s", 0.5))
    try:
        actual = float(probe(final)["duration_s"])
        diff = actual - expected
        checks.append(_check("duration", abs(diff) <= tol,
                             f"final {actual:.2f}s vs ranges {expected:.2f}s (diff {diff:+.2f}s, tol {tol}s)",
                             {"final_s": round(actual, 3), "expected_s": round(expected, 3), "diff_s": round(diff, 3),
                              "per_clip": per_clip}))
    except Exception as ex:  # noqa: BLE001
        checks.append(_check("duration", False, f"could not probe {final.name}: {ex}", {"expected_s": round(expected, 3)}))
        actual = None

    # step_coverage (silence policy keep_active) or no_long_silence (cut_all)
    sil = ce.silence_settings(profile, E)
    silences = sil.get("silences") or {}
    if sil["policy"] != "keep_active":
        left = ce.long_silences(ranges, words, sil)
        checks.append(_check("no_long_silence", not left,
                             "no pause over the threshold left in kept ranges" if not left else
                             f"{len(left)} pause(s) left: " + ", ".join(f"{p['start']:.2f}-{p['end']:.2f}" for p in left[:10]),
                             {"pauses": left}))
    elif all(a is None for a in acts.values()):
        checks.append(_check("step_coverage", False, "activity.json missing: cannot check coverage"))
    else:
        unc = []
        for stem, act in acts.items():
            if act is None:
                continue
            mine = [r for r in ranges if r["source"] == stem]
            for g in act.get("gaps", []):
                if g.get("class") == "active" and g.get("must_keep"):
                    a, b = g["must_keep"]
                    if not any(r["start"] <= a + EPS and r["end"] >= b - EPS for r in mine):
                        unc.append([a, b] if len(stems) == 1 else [a, b, stem])
        checks.append(_check("step_coverage", not unc,
                             "all active spans kept" if not unc else
                             f"{len(unc)} active span(s) not fully kept: " + ", ".join(f"{u[0]:.2f}-{u[1]:.2f}" + (f" ({u[2]})" if len(u) > 2 else "") for u in unc),
                             {"uncovered": unc}))

    # clipped_words: an edge that cuts into audible speech (the ear check; needs envelopes)
    if sil.get("envelopes"):
        out_at = []
        t = 0.0
        for r in ranges:
            out_at.append(t)
            t += r["end"] - r["start"]
        clipped = ce.clipped_edges(ranges, words, sil)
        for c in clipped:
            i = c["range_index"]
            c["output_s"] = round(out_at[i] + (0 if c["edge"] == "start" else ranges[i]["end"] - ranges[i]["start"]), 2)
            c["id"] = edge_id(c)
        checks.append(_check("clipped_words", not clipped,
                             "no cut lands inside audible speech" if not clipped else
                             f"{len(clipped)} cut(s) inside audible speech, output " +
                             ", ".join(f"{c['id']} @{int(c['output_s'] // 60)}:{c['output_s'] % 60:04.1f} '{c['word']}'" for c in clipped[:12]),
                             {"edges": clipped}))

    # cut_in_sound (advisory): edges where the sound keeps going past the cut, measured on the audio
    # alone. Independent of the merge's limits, so it can catch what clipped_words shares a blind
    # spot with; a filler cut from running speech shows up too, so it is a review list.
    if sil.get("envelopes"):
        out_at, t = [], 0.0
        for r in ranges:
            out_at.append(t)
            t += r["end"] - r["start"]
        cis = ce.cuts_in_sound(ranges, sil)
        for c in cis:
            i = c["range_index"]
            c["output_s"] = round(out_at[i] + (0 if c["edge"] == "start" else ranges[i]["end"] - ranges[i]["start"]), 2)
            c["id"] = edge_id(c)
        checks.append(_check("cut_in_sound", not cis,
                             "every cut lands where the sound has stopped" if not cis else
                             f"{len(cis)} cut(s) while sound keeps going (review: clipped word, or a filler cut from running speech), output " +
                             ", ".join(f"{c['id']} @{int(c['output_s'] // 60)}:{c['output_s'] % 60:04.1f}" for c in cis[:15]),
                             {"edges": cis}, advisory=True))

    # word_boundaries
    bad = []
    for i, r in enumerate(ranges):
        for edge, label in ((r["start"], "start"), (r["end"], "end")):
            if any(a - EPS <= edge <= b + EPS for a, b in silences.get(r["source"], [])):
                continue  # edge inside a detected silence: the word was stretched over a pause
            env = (sil.get("envelopes") or {}).get(r["source"])
            if env is not None and env.voiced(edge - 0.02, edge + 0.02) < 0.02:
                continue  # the audio is quiet at the edge: the transcript stretched the word
            for w in words.get(r["source"], []):
                if w["start"] + EPS < edge < w["end"] - EPS:
                    bad.append({"range_index": i, "edge": label, "time": edge, "word": w["text"],
                                "word_span": [w["start"], w["end"]]})
                    break
    checks.append(_check("word_boundaries", not bad,
                         "no range edge inside a word" if not bad else f"{len(bad)} edge(s) inside a word",
                         {"violations": bad}))

    # audio_peak
    peak_max = float(vp.get("true_peak_max_dbtp", -1.0))
    try:
        ld = loudness(final)
        tp = float(ld["true_peak_dbtp"])
        checks.append(_check("audio_peak", tp <= peak_max, f"true peak {tp:.2f} dBTP (max {peak_max})",
                             {"true_peak_dbtp": tp, "integrated_lufs": ld.get("integrated_lufs")}))
    except Exception as ex:  # noqa: BLE001
        checks.append(_check("audio_peak", False, f"could not measure loudness: {ex}"))

    # av_sync
    sync_max = float(vp.get("av_sync_max_ms", 100))
    try:
        av = av_durations(final)
        d_ms = abs(float(av["video_s"]) - float(av["audio_s"])) * 1000
        checks.append(_check("av_sync", d_ms <= sync_max, f"A/V duration difference {d_ms:.0f} ms (max {sync_max:g})",
                             {"video_s": av["video_s"], "audio_s": av["audio_s"], "diff_ms": round(d_ms, 1)}))
    except Exception as ex:  # noqa: BLE001
        checks.append(_check("av_sync", False, f"could not read A/V durations: {ex}"))

    # cut_ratio (advisory)
    lo, hi = vp.get("cut_ratio_flag", [0.25, 0.70])
    src_total = sum(d for d in durs.values() if d)
    if src_total:
        ratio = expected / src_total
        checks.append(_check("cut_ratio", lo <= ratio <= hi,
                             f"kept {ratio:.1%} of source (flag outside {lo:.0%}-{hi:.0%})",
                             {"kept_ratio": round(ratio, 4), "cut_fraction": round(1 - ratio, 4),
                              "source_s": round(src_total, 3), "kept_s": round(expected, 3),
                              "per_clip": per_clip}, advisory=True))
    else:
        checks.append(_check("cut_ratio", True, "skipped: source duration unknown", {"skipped": True}, advisory=True))

    overall = "PASS" if all(c["status"] == "PASS" for c in checks if not c["data"].get("advisory")) else "FAIL"
    return {"overall": overall, "checks": checks}


REVIEW_CHECKS = ("cut_in_sound", "clipped_words")


def edge_id(e: dict) -> str:
    """The name Ines uses for one flagged edge: "r<range_index>.<start|end>"."""
    return f"r{e['range_index']}.{e['edge']}"


def _range_of(eid: str):
    try:
        return int(eid[1:].split(".")[0])
    except ValueError:
        return None


def verdict_coverage(checks: dict, verdict: dict) -> list[str]:
    """The flagged edges (cut_in_sound, clipped_words) the verdict leaves unreviewed: no call in its
    "edges" list, or called "clipped" with no problem fixing that range."""
    fixed = {(p.get("fix") or {}).get("range_index") for p in verdict.get("problems") or []}
    called = {str(x.get("id")) for x in verdict.get("edges") or []
              if x.get("call") and (x["call"] != "clipped" or _range_of(str(x.get("id"))) in fixed)}
    want = [edge_id(e) for c in checks.get("checks") or [] if c.get("name") in REVIEW_CHECKS
            for e in (c.get("data") or {}).get("edges") or []]
    return [i for i in dict.fromkeys(want) if i not in called]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Deterministic checks on final.mp4")
    ap.add_argument("--edit-dir", required=True, type=Path)
    ap.add_argument("--profile", type=Path, default=None)
    ap.add_argument("--final", type=Path, default=None)
    ap.add_argument("--coverage", action="store_true",
                    help="after the edit-verifier: list flagged edges verdict.json gives no call (exit 1 if any)")
    a = ap.parse_args(argv)
    if a.coverage:
        v = a.edit_dir / "verify"
        missing = verdict_coverage(json.loads((v / "checks.json").read_text()),
                                   json.loads((v / "verdict.json").read_text()))
        for i in missing:
            print(f"unreviewed edge {i}")
        print(f"coverage: {len(missing)} flagged edge(s) without a call")
        return 1 if missing else 0
    if not (a.edit_dir / "edl.json").exists():
        print(f"missing {a.edit_dir / 'edl.json'}", file=sys.stderr)
        return 1
    res = run_checks(a.edit_dir, ce.load_profile(a.profile), a.final)
    out = a.edit_dir / "verify"
    out.mkdir(exist_ok=True)
    (out / "checks.json").write_text(json.dumps(res, indent=2) + "\n")
    for c in res["checks"]:
        print(f"{c['status']:4} {c['name']:16} {c['detail']}")
    print(f"overall: {res['overall']} -> {out / 'checks.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
