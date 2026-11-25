"""Pre-run prediction of wall time, tokens and peak extra RAM, calibrated from history.

CLI: rio1/helpers/estimate.py (--source VIDEO [--source VIDEO2 ...] | --run-dir RUN) --out E/estimate.json [--profile P] [--history H]
                         [--transcript SCRIBE.json] [--probe-json PROBE.json] [--no-blur]
Exit codes: 0 gate ok (warnings allowed), 3 gate failed (estimate.json is still written), 1 bad input.

Model (cost = coefficient x driver):
  transcribe = transcribe_s_per_source_min x source_min
  detect     = detect_s_per_source_min x source_min
  edl        = llm_wall_s_per_chunk x chunks
  blur       = blur_s_per_output_min x output_min        (only when the blur scan runs: estimate(..., blur=False) / --no-blur)
  render     = 4K output (profile render.output.size set, 2026-11-19):
                 render4k_s_per_output_gigapixel x output frames x output MP / 1000   (RSS = render4k_peak_rss_mb)
               the recording's own size (render.output.size null, the old Rio):
                 render_s_per_output_megapixel_min x output_min x megapixels   (RSS = render_peak_rss_mb_per_megapixel x MP)
  verify     = verify_s_per_output_min x output_min
  verifier   = verifier_wall_s
  tokens     = words x tokens_per_source_word_editor + chunks x tokens_per_chunk_overhead + tokens_verifier   (subagents)
               + tokens_orchestrator (main)
  output_min = source_min x keep_ratio (prior 0.33 unless priors.keep_ratio is set)
Coefficients = profile priors, each replaced by the median of its last 5 observed values in history/runs.jsonl
(`observed.coefficients` written by record_run.py). Ranges are mid x (1 -/+ spread_eff), spread_eff shrinks with runs.

Public API: calibrate(priors, runs) -> (coeffs, n_observed); estimate(profile, source, runs, words=None) -> dict;
            combine_sources(list_of_source_dicts) -> source dict (first clip's format, summed duration, "clips" list)
            gate(est, budget) -> {"ok","reasons"}; load_history(path) -> list[dict]
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from helpers.paths import CODE_ROOT, PROJECT_ROOT  # noqa: E402  (code vs shared data)

KEEP_RATIO_PRIOR = 0.33
COEFFS = ["transcribe_s_per_source_min", "detect_s_per_source_min", "render_s_per_output_megapixel_min",
          "verify_s_per_output_min", "llm_wall_s_per_chunk", "verifier_wall_s",
          "tokens_per_source_word_editor", "tokens_per_chunk_overhead", "tokens_verifier",
          "tokens_orchestrator", "render_peak_rss_mb_per_megapixel", "keep_ratio",
          "render4k_s_per_output_gigapixel", "render4k_peak_rss_mb", "blur_s_per_output_min"]
# fixed RSS guesses (MB) for steps with no RSS prior; the render RSS is calibrated
STEP_RSS = {"transcribe": 150, "detect": 350, "edl": 500, "blur": 450, "verify": 300, "verifier": 500}
STEPS = ["transcribe", "detect", "edl", "blur", "render", "verify", "verifier"]
LAST_N = 5


def load_history(path: Path | str | None) -> list[dict]:
    p = Path(path) if path else PROJECT_ROOT / "history" / "runs.jsonl"
    runs = []
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                runs.append(json.loads(line))
            except ValueError:
                continue
    return runs


def calibrate(priors: dict, runs: list[dict]):
    prior_vals = dict(priors)
    prior_vals.setdefault("keep_ratio", KEEP_RATIO_PRIOR)
    coeffs, n_obs = {}, {}
    for name in COEFFS:
        vals = []
        for r in runs:
            v = ((r.get("observed") or {}).get("coefficients") or {}).get(name)
            if isinstance(v, (int, float)) and v > 0:
                vals.append(float(v))
        vals = vals[-LAST_N:]
        n_obs[name] = len(vals)
        coeffs[name] = statistics.median(vals) if vals else float(prior_vals[name])
    return coeffs, n_obs


def _rng(mid: float, s: float, nd=1):
    return [round(mid * (1 - s), nd), round(mid, nd), round(mid * (1 + s), nd)]


def fps_of(source: dict) -> float:
    v = source.get("fps", source.get("avg_fps")) or 30
    try:
        n, _, d = str(v).partition("/")
        return float(n) / float(d or 1) or 30.0
    except (ValueError, ZeroDivisionError):
        return 30.0


def output_size(profile: dict) -> list[int] | None:
    """render.output.size from the profile ([3840, 2160] by default in Rio 1), or None = the recording's size."""
    size = ((profile.get("render") or {}).get("output") or {}).get("size")
    return [int(size[0]), int(size[1])] if size else None


def estimate(profile: dict, source: dict, runs: list[dict], words: int | None = None,
             blur: bool = True) -> dict:
    pri = profile["estimator_priors"]
    coeffs, n_obs = calibrate(pri, runs)
    runs_used = sum(1 for r in runs if (r.get("observed") or {}).get("coefficients"))
    base = float(pri.get("spread", 0.35))
    s = max(base * 0.25, base / math.sqrt(1 + runs_used))
    chunk_min = float(profile.get("edit", {}).get("chunk_minutes", 15))
    dur = float(source["duration_s"])
    src_min = dur / 60
    mp = float(source["width"]) * float(source["height"]) / 1e6
    out_min = src_min * coeffs["keep_ratio"]
    clip_durs = [float(c) for c in source.get("clip_durations_s") or [dur]]
    chunks = sum(max(1, math.ceil(c / (chunk_min * 60))) for c in clip_durs)  # chunks never span clips
    words_src = "transcript" if words is not None else "estimate"
    n_words = float(words) if words is not None else float(pri["words_per_source_min"]) * src_min

    osize = output_size(profile)
    fps = fps_of(source)
    out_mp = osize[0] * osize[1] / 1e6 if osize else mp
    gpx = out_min * 60 * fps * out_mp / 1000
    wall = {
        "transcribe": coeffs["transcribe_s_per_source_min"] * src_min,
        "detect": coeffs["detect_s_per_source_min"] * src_min,
        "edl": coeffs["llm_wall_s_per_chunk"] * chunks,
        "blur": coeffs["blur_s_per_output_min"] * out_min,
        "render": (coeffs["render4k_s_per_output_gigapixel"] * gpx if osize
                   else coeffs["render_s_per_output_megapixel_min"] * out_min * mp),
        "verify": coeffs["verify_s_per_output_min"] * out_min,
        "verifier": coeffs["verifier_wall_s"],
    }
    rss = dict(STEP_RSS)
    rss["render"] = coeffs["render4k_peak_rss_mb"] if osize else coeffs["render_peak_rss_mb_per_megapixel"] * mp
    if not blur:
        wall.pop("blur")
        rss.pop("blur")
    steps = {k: {"wall_s": _rng(wall[k], s), "peak_rss_mb": round(rss[k])} for k in STEPS if k in wall}

    sub = (n_words * coeffs["tokens_per_source_word_editor"] + chunks * coeffs["tokens_per_chunk_overhead"]
           + coeffs["tokens_verifier"])
    main = coeffs["tokens_orchestrator"]
    peak = max(rss.values())
    return {
        "source": {"duration_s": dur, "width": source["width"], "height": source["height"],
                   "fps": source.get("fps", source.get("avg_fps")), "vfr": bool(source.get("vfr", False)),
                   "clip_count": len(clip_durs)},
        "steps": steps,
        "totals": {"wall_s": _rng(sum(wall.values()), s),
                   "tokens": {"main": int(round(main)), "subagents": int(round(sub)),
                              "total": _rng(main + sub, s, 0)},
                   "peak_extra_ram_mb": _rng(peak, s, 0),
                   "scribe_usd": round(float(pri["scribe_usd_per_hour"]) * dur / 3600, 3)},
        "calibration": {"runs_used": runs_used, "coefficients": {k: round(v, 4) for k, v in coeffs.items()},
                        "n_observed": n_obs, "spread": round(s, 4)},
        "drivers": {"source_min": round(src_min, 3), "words": round(n_words), "words_source": words_src,
                    "chunks": chunks, "output_min": round(out_min, 3), "megapixels": round(mp, 3),
                    "render_mode": "4k" if osize else "source", "output_megapixels": round(out_mp, 4),
                    "output_fps": round(fps, 3), "output_gigapixels": round(gpx, 3), "blur": bool(blur)},
        "gate": {"ok": True, "reasons": []},
    }


def gate(est: dict, budget: dict) -> dict:
    reasons, ok = [], True
    lo, mid, hi = est["totals"]["peak_extra_ram_mb"]
    cap = budget.get("max_extra_ram_mb")
    if cap is not None:
        if mid > cap:
            ok = False
            reasons.append(f"FAIL: peak extra RAM mid {mid:.0f} MB exceeds budget {cap} MB")
        elif hi > cap:
            reasons.append(f"WARN: peak extra RAM high {hi:.0f} MB exceeds budget {cap} MB (mid {mid:.0f} MB ok)")
    tcap = budget.get("token_budget_per_window")
    tlo, tmid, thi = est["totals"]["tokens"]["total"]
    if tcap is None:
        reasons.append(f"WARN: token_budget_per_window not set; tokens {tlo:,.0f}-{thi:,.0f} unchecked")
    elif thi > tcap:
        ok = False
        reasons.append(f"FAIL: tokens high {thi:,.0f} exceeds window budget {tcap:,}")
    elif tmid > 0.8 * tcap:
        reasons.append(f"WARN: tokens mid {tmid:,.0f} is over 80% of window budget {tcap:,}")
    return {"ok": ok, "reasons": reasons}


def _hms(sec: float) -> str:
    sec = int(round(sec))
    return f"{sec // 3600}h{(sec % 3600) // 60:02d}m" if sec >= 3600 else f"{sec // 60}m{sec % 60:02d}s"


def format_table(est: dict) -> str:
    d, c = est["drivers"], est["calibration"]
    src = est["source"]
    L = [f"Source {src['duration_s'] / 60:.1f} min {src['width']}x{src['height']}  words {d['words']:,} ({d['words_source']})  "
         f"chunks {d['chunks']}  output ~{d['output_min']:.1f} min",
         f"Calibration: {c['runs_used']} past run(s), spread +/-{c['spread']:.0%}",
         f"{'step':<11}{'lo':>8}{'mid':>8}{'hi':>8}{'rss MB':>9}"]
    for k in [x for x in STEPS if x in est["steps"]]:
        lo, mid, hi = est["steps"][k]["wall_s"]
        L.append(f"{k:<11}{_hms(lo):>8}{_hms(mid):>8}{_hms(hi):>8}{est['steps'][k]['peak_rss_mb']:>9}")
    t = est["totals"]
    lo, mid, hi = t["wall_s"]
    L.append(f"{'TOTAL':<11}{_hms(lo):>8}{_hms(mid):>8}{_hms(hi):>8}")
    tl, tm, th = t["tokens"]["total"]
    L.append(f"tokens      {tl:,.0f} / {tm:,.0f} / {th:,.0f}  (main {t['tokens']['main']:,}, subagents {t['tokens']['subagents']:,})")
    rl, rm, rh = t["peak_extra_ram_mb"]
    L.append(f"peak RAM    {rl:,.0f} / {rm:,.0f} / {rh:,.0f} MB     scribe ${t['scribe_usd']:.2f}")
    g = est["gate"]
    L.append("GATE " + ("OK" if g["ok"] else "FAIL") + "".join(f"\n  {r}" for r in g["reasons"]))
    return "\n".join(L)


def probe_source(path: Path) -> dict:
    from helpers.media import checks
    pj = checks.probe(str(path))
    return {"duration_s": pj["duration_s"], "width": pj["width"], "height": pj["height"],
            "fps": pj.get("avg_fps"), "vfr": pj.get("vfr", False)}


def combine_sources(sources: list[dict]) -> dict:
    """probe.json-style top level: first clip's format, summed duration, vfr if any."""
    first = sources[0]
    return {**first, "duration_s": sum(float(s["duration_s"]) for s in sources),
            "vfr": any(s.get("vfr") for s in sources),
            "clip_durations_s": [float(s["duration_s"]) for s in sources]}


def count_words(transcript: Path | list) -> int:
    """Real word count of one Scribe JSON or several (summed)."""
    paths = transcript if isinstance(transcript, (list, tuple)) else [transcript]
    total = 0
    for t in paths:
        data = json.loads(Path(t).read_text())
        total += sum(1 for w in data.get("words", []) if w.get("type", "word") == "word")
    return total


def _from_probe_json(pj: dict) -> dict:
    def one(d):
        return {"duration_s": d["duration_s"], "width": d["width"], "height": d["height"],
                "fps": d.get("avg_fps"), "vfr": d.get("vfr", False)}
    clips = pj.get("clips")
    if clips:
        src = one(pj)
        src["clip_durations_s"] = [float(c["duration_s"]) for c in clips.values()]
        return src
    return one(pj)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Predict wall time, tokens and RAM for a run")
    ap.add_argument("--source", type=Path, action="append", default=[], help="repeat for several clips")
    ap.add_argument("--run-dir", type=Path, default=None, help="use the clips listed in <run>/edit/sources.json")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--profile", type=Path, default=CODE_ROOT / "profiles" / "tutorial.json")
    ap.add_argument("--history", type=Path, default=PROJECT_ROOT / "history" / "runs.jsonl")
    ap.add_argument("--transcript", type=Path, action="append", default=[],
                    help="Scribe JSON; use its real word count (repeatable, summed)")
    ap.add_argument("--probe-json", type=Path, default=None, help="use an existing probe.json instead of ffprobe")
    ap.add_argument("--no-blur", action="store_true", help="the user said no sensitive info: no blur scan")
    a = ap.parse_args(argv)
    try:
        profile = json.loads(a.profile.read_text())
        sources = list(a.source)
        if a.run_dir and not sources and not a.probe_json:
            from helpers import chunk_edl as ce
            E = a.run_dir / "edit"
            sources = [a.run_dir / ce.load_sources(E)["files"][st] for st in ce.load_sources(E)["order"]]
        if not sources and not a.probe_json:
            raise ValueError("give --source (repeatable), --run-dir or --probe-json")
        if a.probe_json:
            source = _from_probe_json(json.loads(a.probe_json.read_text()))
        else:
            source = combine_sources([probe_source(p) for p in sources])
        words = count_words(a.transcript) if a.transcript else None
    except Exception as ex:  # noqa: BLE001
        print(f"estimate failed: {ex}", file=sys.stderr)
        return 1
    est = estimate(profile, source, load_history(a.history), words, blur=not a.no_blur)
    est["gate"] = gate(est, profile.get("budget", {}))
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(est, indent=2) + "\n")
    print(format_table(est))
    return 0 if est["gate"]["ok"] else 3


if __name__ == "__main__":
    sys.exit(main())
