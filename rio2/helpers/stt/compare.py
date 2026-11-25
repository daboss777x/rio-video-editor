#!/usr/bin/env python3
"""STT bake-off: compare AssemblyAI / Deepgram against an existing Scribe transcript.

  compare.py --video V --reference E/transcripts/<stem>.json --out DIR [--providers assemblyai,deepgram]
"""
import argparse
import difflib
import json
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import providers as P  # noqa: E402

FILLERS = {"um", "umm", "uh", "uhh", "er", "erm", "ah", "hmm", "hm", "mm", "mhm", "uh-huh", "eh"}
# Assumed list prices, USD per audio hour (public pricing pages, from memory; verify before deciding).
RATES = {"scribe": (0.40, "ElevenLabs Scribe ~$0.40/h (elevenlabs.io/pricing)"),
         "assemblyai": (0.15, "AssemblyAI Universal $0.15/h (assemblyai.com/pricing)"),
         "deepgram": (0.258, "Deepgram Nova-3 pre-recorded PAYG $0.0043/min (deepgram.com/pricing)")}
ENV = {"assemblyai": "ASSEMBLYAI_API_KEY", "deepgram": "DEEPGRAM_API_KEY"}
FN = {"assemblyai": P.transcribe_assemblyai, "deepgram": P.transcribe_deepgram}


def norm(t):
    return re.sub(r"[^a-z0-9'-]", "", t.lower())


def word_list(tr):
    out = []
    for w in tr.get("words", []):
        if w.get("type", "word") == "word":
            n = norm(w["text"])
            if n:
                out.append((n, float(w["start"]), float(w["end"])))
    return out


def count_fillers(tr):
    return sum(1 for n, _, _ in word_list(tr) if n in FILLERS)


def pct(vals, p):
    if not vals:
        return 0.0
    s = sorted(vals)
    return s[min(len(s) - 1, int(round(p * (len(s) - 1))))]


def align_metrics(ref, hyp):
    a, b = word_list(ref), word_list(hyp)
    sm = difflib.SequenceMatcher(None, [x[0] for x in a], [x[0] for x in b], autojunk=False)
    ds, de, n = [], [], 0
    for blk in sm.get_matching_blocks():
        for k in range(blk.size):
            x, y = a[blk.a + k], b[blk.b + k]
            ds.append(abs(x[1] - y[1]) * 1000)
            de.append(abs(x[2] - y[2]) * 1000)
            n += 1
    return {"aligned": n, "ref_words": len(a), "hyp_words": len(b),
            "start_med_ms": statistics.median(ds) if ds else 0, "start_p90_ms": pct(ds, 0.9),
            "end_med_ms": statistics.median(de) if de else 0, "end_p90_ms": pct(de, 0.9)}


def filler_diffs(ref, hyp, limit=5):
    """Spots where one transcript has a filler (within 0.6 s) that the other lacks."""
    rf = [(s, n) for n, s, _ in word_list(ref) if n in FILLERS]
    hf = [(s, n) for n, s, _ in word_list(hyp) if n in FILLERS]
    spots = []
    for s, n in rf:
        if not any(abs(s - t) < 0.6 for t, _ in hf):
            spots.append((s, "Scribe has '%s', provider missed it" % n))
    for s, n in hf:
        if not any(abs(s - t) < 0.6 for t, _ in rf):
            spots.append((s, "Provider has '%s', Scribe does not" % n))
    spots.sort()
    if len(spots) > limit:
        step = len(spots) / limit
        spots = [spots[int(i * step)] for i in range(limit)]
    return spots


def cost(provider, minutes):
    return RATES[provider][0] * minutes / 60.0


def audio_minutes(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True)
    return float(r.stdout.strip()) / 60.0


def build_report(ref, results, minutes):
    rw, rf = len(word_list(ref)), count_fillers(ref)
    summary = ["Compared on %.1f minutes of audio. Scribe heard %d words and %d fillers (um/uh...)." % (minutes, rw, rf)]
    body = []
    for name, r in results.items():
        if r.get("skipped"):
            body.append("## %s\nSkipped: %s\n" % (name, r["skipped"]))
            summary.append("%s: skipped (%s)." % (name, r["skipped"]))
            continue
        if r.get("error"):
            body.append("## %s\nFailed: %s\n" % (name, r["error"]))
            summary.append("%s: failed." % name)
            continue
        tr, m = r["transcript"], r["metrics"]
        fc = count_fillers(tr)
        c = cost(name, minutes)
        summary.append("%s: %d fillers kept (Scribe %d); %.0f%% of Scribe's words matched; typical start-time gap %.0f ms; "
                       "cost for this clip about $%.4f vs Scribe $%.4f." % (
                           name, fc, rf, 100.0 * m["aligned"] / max(1, m["ref_words"]),
                           m["start_med_ms"], c, cost("scribe", minutes)))
        diffs = "\n".join("  - %.1fs: %s" % d for d in filler_diffs(ref, tr)) or "  - none"
        body.append("## %s\n- words: %d (Scribe %d)\n- fillers: %d (Scribe %d)\n- words aligned to Scribe: %d of %d\n"
                    "- start diff: median %.0f ms, p90 %.0f ms\n- end diff: median %.0f ms, p90 %.0f ms\n"
                    "- wall time: %.1f s\n- audio: %.2f min\n- est. cost: $%.4f (rate: %s); Scribe at %s = $%.4f\n"
                    "- filler differences (examples):\n%s\n" % (
                        name, m["hyp_words"], rw, fc, rf, m["aligned"], m["ref_words"], m["start_med_ms"], m["start_p90_ms"],
                        m["end_med_ms"], m["end_p90_ms"], r["wall_s"], minutes, c, RATES[name][1],
                        RATES["scribe"][1], cost("scribe", minutes), diffs))
    head = "# STT bake-off\n\n## Summary\n" + "\n".join("- " + s for s in summary) + \
        "\n\nRates are assumed list prices and may be out of date; check the vendor pricing pages.\n"
    return "\n".join([head] + body)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True)
    ap.add_argument("--reference", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--providers", default="assemblyai,deepgram")
    a = ap.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    ref = json.loads(Path(a.reference).read_text())
    flac = P.extract_flac(a.video, out / "audio.flac")
    minutes = audio_minutes(flac)
    results = {}
    for name in [p.strip() for p in a.providers.split(",") if p.strip()]:
        if name not in FN:
            results[name] = {"skipped": "unknown provider"}
            continue
        if not P.has_key(ENV[name]):
            print("skipping %s: %s not found in .env" % (name, ENV[name]))
            results[name] = {"skipped": "%s not set" % ENV[name]}
            continue
        t0 = time.time()
        try:
            tr = FN[name](flac)
        except Exception as e:  # friendly message; never include headers
            results[name] = {"error": "%s: %s" % (type(e).__name__, str(e)[:200])}
            continue
        wall = time.time() - t0
        (out / ("%s.json" % name)).write_text(json.dumps(tr, indent=1))
        results[name] = {"transcript": tr, "wall_s": wall, "metrics": align_metrics(ref, tr)}
    (out / "report.md").write_text(build_report(ref, results, minutes))
    print("wrote", out / "report.md")


if __name__ == "__main__":
    main()
