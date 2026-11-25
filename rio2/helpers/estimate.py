"""Pre-run prediction of wall time, tokens and peak extra RAM, calibrated from history.

CLI: rio2/helpers/estimate.py (--source VIDEO [--source VIDEO2 ...] [--cam CAM ...] [--composite] | --run-dir RUN) --out E/estimate.json [--profile P] [--history H]
                         [--transcript SCRIBE.json] [--probe-json PROBE.json]
Exit codes: 0 gate ok (warnings allowed), 3 gate failed (estimate.json is still written), 1 bad input.

Model (cost = coefficient x driver):
  transcribe = transcribe_s_per_source_min x source_min
  detect     = detect_s_per_source_min x source_min
  edl        = llm_wall_s_per_chunk x chunks
  render     = render_s_per_output_megapixel_min x output_min x megapixels   (RSS = render_peak_rss_mb_per_megapixel x MP)
  verify     = verify_s_per_output_min x output_min
  verifier   = verifier_wall_s
  tokens     = words x tokens_per_source_word_editor + chunks x tokens_per_chunk_overhead + tokens_verifier   (subagents)
               + tokens_orchestrator (main)
  output_min = source_min x keep_ratio (prior 0.33 unless priors.keep_ratio is set)
Coefficients = profile priors, each replaced by the median of its last 5 observed values in history/runs.jsonl
(`observed.coefficients` written by record_run.py). Ranges are mid x (1 -/+ spread_eff), spread_eff shrinks with runs.

Composite (two-camera 4K) mode, rio2 only: on when the source list has a camera file (stem contains "cam",
case-insensitive), when sources.json lists cams, or when --composite is given. Camera files are NOT screen clips:
they are left out of duration, words, chunks, transcription and the Scribe cost. Adds a `sync` step
(sync_s_per_source_hour x source hours, RSS sync_peak_rss_mb) and renders at the layout canvas
(layout.canvas, default [3840, 2160]) with their own coefficients, learned only from history lines with
`composite: true` (normal render coefficients ignore those lines; every other step learns from all lines).
Composite priors are UNCALIBRATED guesses until a composite run is recorded: render wall = 3x the normal per-output-minute
cost at the screen size, render RSS = 1200 MB, sync = 60 s per source hour at 400 MB.
Rio 2.0 layout steps (composite only, added 2026-11-10 after the mainscreen1 run under-forecast by ~25 min):
  slides     = slides_s_per_source_min x source_min       (slides.py, software decode; prior 3.2 s, RSS 280 MB)
  face_track = face_track_s_per_output_min x output_min   (face_track.py; prior 10.2 s, RSS 730 MB)
  layout     = layout_wall_s                              (layout_preview.py stills; prior 10 s, RSS 1300 MB)
Priors measured on mainscreen1 (40.5 source min, 16.8 output min); learned from composite history lines.

Public API: calibrate(priors, runs, composite=False) -> (coeffs, n_observed); estimate(profile, source, runs, words=None) -> dict;
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
          "tokens_orchestrator", "render_peak_rss_mb_per_megapixel", "keep_ratio"]
# composite-only coefficients (learned only from history lines with composite true)
COMPOSITE_COEFFS = ["composite_render_s_per_output_megapixel_min", "composite_render_peak_rss_mb_per_megapixel",
                    "sync_s_per_source_hour", "sync_peak_rss_mb",
                    "slides_s_per_source_min", "face_track_s_per_output_min", "layout_wall_s"]
NORMAL_RENDER_COEFFS = ("render_s_per_output_megapixel_min", "render_peak_rss_mb_per_megapixel")
COMPOSITE_RENDER_FACTOR = 3.0     # uncalibrated: composite render costs ~3x the normal per-output-minute cost
COMPOSITE_RENDER_RSS_MB = 1200.0  # uncalibrated
SYNC_S_PER_SOURCE_HOUR = 60.0
SYNC_RSS_MB = 400.0
SLIDES_S_PER_SOURCE_MIN = 3.2        # mainscreen1, software decode (127 s / 40.5 min)
FACE_TRACK_S_PER_OUTPUT_MIN = 10.2   # mainscreen1 (171.5 s / 16.8 output min)
LAYOUT_WALL_S = 10.0                 # mainscreen1 (8.7 s, 6 stills)
COMPOSITE_STEP_RSS = {"slides": 280, "face_track": 730, "layout": 1300}
DEFAULT_CANVAS = [3840, 2160]
# fixed RSS guesses (MB) for steps with no RSS prior; the render RSS is calibrated
STEP_RSS = {"transcribe": 150, "detect": 350, "edl": 500, "verify": 300, "verifier": 500}
STEPS = ["transcribe", "detect", "edl", "render", "verify", "verifier"]
STEPS_COMPOSITE = ["transcribe", "sync", "detect", "slides", "edl", "face_track", "layout", "render", "verify",
                   "verifier"]
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


def is_cam_path(p) -> bool:
    """A camera file: the stem contains "cam" (case-insensitive)."""
    return "cam" in Path(str(p)).stem.lower()


def canvas_of(profile: dict) -> list[int]:
    c = (profile.get("layout") or {}).get("canvas") or DEFAULT_CANVAS
    return [int(c[0]), int(c[1])]


def calibrate(priors: dict, runs: list[dict], composite: bool = False, mp: float | None = None,
              screen_mp: float | None = None):
    """Median of the last 5 observed values per coefficient, else the prior.
    Normal render coefficients ignore composite history lines; composite ones use only those lines."""
    prior_vals = dict(priors)
    prior_vals.setdefault("keep_ratio", KEEP_RATIO_PRIOR)
    cmp_mp = mp or DEFAULT_CANVAS[0] * DEFAULT_CANVAS[1] / 1e6
    scr_mp = screen_mp or cmp_mp
    prior_vals.setdefault("composite_render_s_per_output_megapixel_min",
                          COMPOSITE_RENDER_FACTOR * float(prior_vals.get("render_s_per_output_megapixel_min", 6.0)) * scr_mp / cmp_mp)
    prior_vals.setdefault("composite_render_peak_rss_mb_per_megapixel", COMPOSITE_RENDER_RSS_MB / cmp_mp)
    prior_vals.setdefault("sync_s_per_source_hour", SYNC_S_PER_SOURCE_HOUR)
    prior_vals.setdefault("sync_peak_rss_mb", SYNC_RSS_MB)
    prior_vals.setdefault("slides_s_per_source_min", SLIDES_S_PER_SOURCE_MIN)
    prior_vals.setdefault("face_track_s_per_output_min", FACE_TRACK_S_PER_OUTPUT_MIN)
    prior_vals.setdefault("layout_wall_s", LAYOUT_WALL_S)
    coeffs, n_obs = {}, {}
    for name in COEFFS + COMPOSITE_COEFFS:
        vals = []
        for r in runs:
            if name in NORMAL_RENDER_COEFFS and r.get("composite"):
                continue
            if name in COMPOSITE_COEFFS and not r.get("composite"):
                continue
            v = ((r.get("observed") or {}).get("coefficients") or {}).get(name)
            if isinstance(v, (int, float)) and v > 0:
                vals.append(float(v))
        vals = vals[-LAST_N:]
        n_obs[name] = len(vals)
        coeffs[name] = statistics.median(vals) if vals else float(prior_vals[name])
    return coeffs, n_obs


def _rng(mid: float, s: float, nd=1):
    return [round(mid * (1 - s), nd), round(mid, nd), round(mid * (1 + s), nd)]


def estimate(profile: dict, source: dict, runs: list[dict], words: int | None = None,
             composite: bool = False, n_cams: int = 0) -> dict:
    """`source` describes the screen clips only. composite=True adds the sync step and the canvas render."""
    pri = profile["estimator_priors"]
    canvas = canvas_of(profile)
    canvas_mp = canvas[0] * canvas[1] / 1e6
    screen_mp = float(source["width"]) * float(source["height"]) / 1e6
    coeffs, n_obs = calibrate(pri, runs, composite, canvas_mp, screen_mp)
    runs_used = sum(1 for r in runs if (r.get("observed") or {}).get("coefficients"))
    base = float(pri.get("spread", 0.35))
    s = max(base * 0.25, base / math.sqrt(1 + runs_used))
    chunk_min = float(profile.get("edit", {}).get("chunk_minutes", 15))
    dur = float(source["duration_s"])
    src_min = dur / 60
    mp = canvas_mp if composite else screen_mp
    out_min = src_min * coeffs["keep_ratio"]
    clip_durs = [float(c) for c in source.get("clip_durations_s") or [dur]]
    chunks = sum(max(1, math.ceil(c / (chunk_min * 60))) for c in clip_durs)  # chunks never span clips
    words_src = "transcript" if words is not None else "estimate"
    n_words = float(words) if words is not None else float(pri["words_per_source_min"]) * src_min

    wall = {
        "transcribe": coeffs["transcribe_s_per_source_min"] * src_min,
        "detect": coeffs["detect_s_per_source_min"] * src_min,
        "edl": coeffs["llm_wall_s_per_chunk"] * chunks,
        "render": coeffs["render_s_per_output_megapixel_min"] * out_min * mp,
        "verify": coeffs["verify_s_per_output_min"] * out_min,
        "verifier": coeffs["verifier_wall_s"],
    }
    rss = dict(STEP_RSS)
    rss["render"] = coeffs["render_peak_rss_mb_per_megapixel"] * mp
    names = list(STEPS)
    if composite:
        wall["render"] = coeffs["composite_render_s_per_output_megapixel_min"] * out_min * mp
        rss["render"] = coeffs["composite_render_peak_rss_mb_per_megapixel"] * mp
        wall["sync"] = coeffs["sync_s_per_source_hour"] * src_min / 60
        rss["sync"] = coeffs["sync_peak_rss_mb"]
        wall["slides"] = coeffs["slides_s_per_source_min"] * src_min
        wall["face_track"] = coeffs["face_track_s_per_output_min"] * out_min
        wall["layout"] = coeffs["layout_wall_s"]
        rss.update(COMPOSITE_STEP_RSS)
        names = STEPS_COMPOSITE
    steps = {k: {"wall_s": _rng(wall[k], s), "peak_rss_mb": round(rss[k])} for k in names}

    sub = (n_words * coeffs["tokens_per_source_word_editor"] + chunks * coeffs["tokens_per_chunk_overhead"]
           + coeffs["tokens_verifier"])
    main = coeffs["tokens_orchestrator"]
    peak = max(rss[k] for k in names)
    out = {
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
                    "chunks": chunks, "output_min": round(out_min, 3), "megapixels": round(mp, 3)},
        "gate": {"ok": True, "reasons": []},
    }
    out["composite"] = {"on": bool(composite), "cams": int(n_cams) if composite else 0,
                        "canvas": canvas if composite else None,
                        "uncalibrated": bool(composite) and n_obs["composite_render_s_per_output_megapixel_min"] == 0}
    return out


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
    cp = est.get("composite") or {}
    if cp.get("on"):
        L.insert(1, f"COMPOSITE: {cp['cams']} camera file(s), canvas {cp['canvas'][0]}x{cp['canvas'][1]}"
                    + (" (sync and render priors UNCALIBRATED)" if cp.get("uncalibrated") else ""))
    for k in est["steps"]:
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
    ap.add_argument("--cam", type=Path, action="append", default=[],
                    help="a camera file (not probed, not a screen clip); repeatable. Camera files given via --source "
                         "(stem contains 'cam') are treated the same")
    ap.add_argument("--composite", action="store_true", help="force composite (two-camera 4K) mode")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--profile", type=Path, default=CODE_ROOT / "profiles" / "tutorial.json")
    ap.add_argument("--history", type=Path, default=PROJECT_ROOT / "history" / "runs.jsonl")
    ap.add_argument("--transcript", type=Path, action="append", default=[],
                    help="Scribe JSON; use its real word count (repeatable, summed)")
    ap.add_argument("--probe-json", type=Path, default=None, help="use an existing probe.json instead of ffprobe")
    a = ap.parse_args(argv)
    try:
        profile = json.loads(a.profile.read_text())
        cams = [p for p in a.source if is_cam_path(p)] + list(a.cam)
        sources = [p for p in a.source if not is_cam_path(p)]
        if a.run_dir and not sources and not a.probe_json:
            from helpers import chunk_edl as ce
            E = a.run_dir / "edit"
            sources = [a.run_dir / ce.load_sources(E)["files"][st] for st in ce.load_sources(E)["order"]]
            try:
                cams += list(json.loads((E / "sources.json").read_text()).get("cams", {}).values())
            except (OSError, ValueError, AttributeError):
                pass
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
    composite = bool(cams) or a.composite
    est = estimate(profile, source, load_history(a.history), words, composite=composite, n_cams=len(cams))
    est["gate"] = gate(est, profile.get("budget", {}))
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(est, indent=2) + "\n")
    print(format_table(est))
    return 0 if est["gate"]["ok"] else 3


if __name__ == "__main__":
    sys.exit(main())
