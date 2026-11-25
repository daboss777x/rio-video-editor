"""Record a finished run: predicted vs actual, calibration ratios, run log, proposed lessons.

CLI: rio2/helpers/record_run.py --run-dir runs/<id> [--history-dir history] [--runs-dir runs] [--force]
Reads (all optional; missing ones are recorded as null and listed under "missing"):
  E/estimate.json, E/timings.jsonl, E/monitor_summary.json, E/tokens.json,
  E/verify/checks.json, E/verify/verdict.json, E/probe.json     (E = <run-dir>/edit)
Writes: <history>/runs.jsonl (one line), <history>/RUN_LOG.md (section), <history>/LESSONS.md (## Proposed only).
Re-recording an already recorded run id is a no-op unless --force (which replaces the runs.jsonl line).

runs.jsonl line:
 {"run_id","recorded_at","source":{duration_s,width,height,fps,vfr},
  "predicted":{"wall_s","tokens","peak_extra_ram_mb","steps":{step:wall_mid}},      # mids
  "actual":{"wall_s","tokens","peak_extra_ram_mb","output_s","wall_s_all_steps","swap_growth_mb",
            "steps":{step:{"wall_s","max_rss_mb"}}},
  "error_pct":{"wall_s","tokens","peak_extra_ram_mb"},                               # (pred-actual)/actual*100
  "observed":{"coefficients":{name: observed value}},                                 # feeds estimate.py
  "outcome":{"status":"PASS|FIXED|FAIL|UNKNOWN","checks":..,"verdict":..,"problems":n},
  "missing":[...], "feedback_ingested":[run ids],
  "system":"rio2", "composite":bool}      # composite = E/cam_sync.json exists with ok true. No "system" = rio1.
 Composite runs write their render observation to composite_render_* coefficients (and sync_*), never the normal render ones.

Public API: record(run_dir, history_dir, runs_dir, force=False) -> dict (the line)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # this system's code root (rio1/ or rio2/)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from helpers.paths import CODE_ROOT, PROJECT_ROOT  # noqa: E402  (code vs shared data)
STEPS = ["transcribe", "sync", "detect", "slides", "edl", "face_track", "layout", "render", "verify", "verifier"]
# step names that would otherwise match a broader prefix ("detect_slides" is not screen detection)
STEP_ALIASES = {"detect_slides": "slides", "face_track": "face_track", "layout_preview": "layout"}


def _read_json(p: Path, missing: list[str], label: str):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        missing.append(label)
        return None


def _read_jsonl(p: Path) -> list[dict]:
    out = []
    try:
        for line in p.read_text().splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    except OSError:
        pass
    return out


def _tok_sum(d) -> float | None:
    if not isinstance(d, dict):
        return None
    return float(sum(v for v in d.values() if isinstance(v, (int, float))))


def step_of(name: str) -> str | None:
    for alias, s in STEP_ALIASES.items():
        if name == alias or name.startswith((alias + "_", alias + "-", alias + ":", alias + ".")):
            return s
    for s in STEPS:
        if name == s or name.startswith((s + "_", s + "-", s + ":", s + ".")):
            return s
    return None


def _err(pred, act):
    if pred is None or act is None or act == 0:
        return None
    return round((pred - act) / act * 100, 1)


def _gather(E: Path):
    missing: list[str] = []
    est = _read_json(E / "estimate.json", missing, "estimate.json")
    timings = _read_jsonl(E / "timings.jsonl") if (E / "timings.jsonl").exists() else None
    if timings is None:
        missing.append("timings.jsonl")
    mon = _read_json(E / "monitor_summary.json", missing, "monitor_summary.json")
    tok = _read_json(E / "tokens.json", missing, "tokens.json")
    checks = _read_json(E / "verify" / "checks.json", missing, "verify/checks.json")
    verdict = _read_json(E / "verify" / "verdict.json", missing, "verify/verdict.json")
    probe = _read_json(E / "probe.json", missing, "probe.json")
    return est, timings, mon, tok, checks, verdict, probe, missing


def _is_composite(E: Path) -> bool:
    """True when this run was rendered as the two-camera composite: E/cam_sync.json exists with ok true."""
    try:
        return json.loads((E / "cam_sync.json").read_text()).get("ok") is True
    except (OSError, ValueError, AttributeError):
        return False


def build_line(run_id: str, E: Path) -> dict:
    est, timings, mon, tok, checks, verdict, probe, missing = _gather(E)
    # actual per-step
    steps: dict = {}
    for row in timings or []:
        s = step_of(str(row.get("name", "")))
        if not s:
            continue
        d = steps.setdefault(s, {"wall_s": 0.0, "max_rss_mb": None})
        d["wall_s"] += float(row.get("wall_s") or 0)
        r = row.get("max_rss_mb")
        if isinstance(r, (int, float)):
            d["max_rss_mb"] = max(d["max_rss_mb"] or 0, r)
    for d in steps.values():
        d["wall_s"] = round(d["wall_s"], 2)
    # an outer "claude" row covers the whole headless session: it is the total, never added to the step sum
    outer = [float(r.get("wall_s") or 0) for r in timings or [] if r.get("name") == "claude"]
    if outer:
        wall_all = round(max(outer), 2)
    else:
        wall_all = round(sum(float(r.get("wall_s") or 0) for r in timings), 2) if timings else None
    wall_modelled = round(sum(d["wall_s"] for d in steps.values()), 2) if steps else None
    peak = (mon or {}).get("peak_extra_mb")
    if peak is None:
        rss = [d["max_rss_mb"] for d in steps.values() if d["max_rss_mb"]]
        peak = max(rss) if rss else None
    tokens = (tok or {}).get("total")
    if tokens is None and tok:
        tokens = (_tok_sum(tok.get("main")) or 0) + (_tok_sum(tok.get("subagents")) or 0)
    out_s = None
    for c in (checks or {}).get("checks", []):
        if c.get("name") == "duration":
            out_s = (c.get("data") or {}).get("final_s")
    if out_s is None and checks is not None:
        pass
    if out_s is None:
        try:
            out_s = json.loads((E / "edl.json").read_text()).get("total_duration_s")
        except (OSError, ValueError):
            out_s = None
    # predicted (mids)
    pred = {"wall_s": None, "tokens": None, "peak_extra_ram_mb": None, "steps": {}}
    if est:
        t = est.get("totals", {})
        pred["wall_s"] = (t.get("wall_s") or [None] * 3)[1]
        pred["tokens"] = ((t.get("tokens") or {}).get("total") or [None] * 3)[1]
        pred["peak_extra_ram_mb"] = (t.get("peak_extra_ram_mb") or [None] * 3)[1]
        pred["steps"] = {k: v["wall_s"][1] for k, v in est.get("steps", {}).items() if v.get("wall_s")}
    actual = {"wall_s": wall_modelled, "wall_s_all_steps": wall_all, "tokens": tokens, "peak_extra_ram_mb": peak,
              "output_s": out_s, "swap_growth_mb": (mon or {}).get("swap_growth_mb"), "steps": steps}
    errs = {k: _err(pred.get(k), actual.get(k)) for k in ("wall_s", "tokens", "peak_extra_ram_mb")}

    # observed coefficient ratios
    obs: dict = {}
    drv = (est or {}).get("drivers") or {}
    used = ((est or {}).get("calibration") or {}).get("coefficients") or {}
    src_min, out_min, mp = drv.get("source_min"), None, drv.get("megapixels")
    if out_s:
        out_min = out_s / 60
    elif drv.get("output_min"):
        out_min = drv["output_min"]
    w = lambda s: steps.get(s, {}).get("wall_s")  # noqa: E731
    if src_min:
        if w("transcribe"):
            obs["transcribe_s_per_source_min"] = w("transcribe") / src_min
        if w("detect"):
            obs["detect_s_per_source_min"] = w("detect") / src_min
        if out_s:
            obs["keep_ratio"] = out_s / 60 / src_min
    composite = _is_composite(E)
    if composite and src_min and w("sync"):
        obs["sync_s_per_source_hour"] = w("sync") / (src_min / 60)
        if steps["sync"]["max_rss_mb"]:
            obs["sync_peak_rss_mb"] = steps["sync"]["max_rss_mb"]
    if composite and src_min and w("slides"):
        obs["slides_s_per_source_min"] = w("slides") / src_min
    if composite and out_min and w("face_track"):
        obs["face_track_s_per_output_min"] = w("face_track") / out_min
    if composite and w("layout"):
        obs["layout_wall_s"] = w("layout")
    if out_min and mp and w("render"):
        obs["composite_render_s_per_output_megapixel_min" if composite else "render_s_per_output_megapixel_min"] = w("render") / (out_min * mp)
    if out_min and w("verify"):
        obs["verify_s_per_output_min"] = w("verify") / out_min
    if drv.get("chunks") and w("edl"):
        obs["llm_wall_s_per_chunk"] = w("edl") / drv["chunks"]
    if w("verifier"):
        obs["verifier_wall_s"] = w("verifier")
    rss_r = steps.get("render", {}).get("max_rss_mb")
    if rss_r and mp:
        obs[("composite_render_peak_rss_mb_per_megapixel" if composite else "render_peak_rss_mb_per_megapixel")] = rss_r / mp
    # tokens.json source "claude-json" has main only (subagents 0): the split is unknown, so don't calibrate tokens
    split_known = bool(tok) and (_tok_sum(tok.get("subagents")) or 0) > 0
    if tok and est and split_known:
        sub_pred = (est.get("totals", {}).get("tokens") or {}).get("subagents")
        sub_act = _tok_sum(tok.get("subagents"))
        if sub_pred and sub_act:
            k = sub_act / sub_pred
            for n in ("tokens_per_source_word_editor", "tokens_per_chunk_overhead", "tokens_verifier"):
                if used.get(n):
                    obs[n] = used[n] * k
        main_act = _tok_sum(tok.get("main"))
        if main_act:
            obs["tokens_orchestrator"] = main_act
    obs = {k: round(v, 4) for k, v in obs.items() if v and v > 0}

    # outcome
    cstat = (checks or {}).get("overall")
    vstat = (verdict or {}).get("verdict")
    problems = len((verdict or {}).get("problems") or [])
    if cstat is None and vstat is None:
        status = "UNKNOWN"
    else:
        status = "FAIL" if "FAIL" in (cstat, vstat) else "PASS"
        # The verifier judges the first render only; after the one fix pass, checks.json is the
        # re-verify of the fixed render. Checks passing after a fix pass = FIXED, not FAIL.
        if status == "FAIL" and cstat == "PASS" and vstat == "FAIL" and (E / "edl.before_fix.json").exists():
            status = "FIXED"
    src = (est or {}).get("source") or ({"duration_s": probe.get("duration_s"), "width": probe.get("width"),
                                         "height": probe.get("height"), "fps": probe.get("avg_fps"),
                                         "vfr": probe.get("vfr")} if probe else None)
    if src is not None and probe and probe.get("clips") and "clips" not in src:
        src = {**src, "clips": len(probe["clips"])}
    return {"run_id": run_id, "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source": src, "predicted": pred, "actual": actual, "error_pct": errs,
            "observed": {"coefficients": obs},
            "outcome": {"status": status, "checks": cstat, "verdict": vstat, "problems": problems},
            "missing": missing, "feedback_ingested": [], "system": "rio2", "composite": composite}


# ------------------------------------------------------------ lessons
def _failure_lessons(run_id: str, E: Path) -> list[str]:
    out = []
    try:
        v = json.loads((E / "verify" / "verdict.json").read_text())
        if v.get("verdict") == "FAIL":
            for p in v.get("problems") or []:
                fx = p.get("fix") or {}
                out.append(f"- [{run_id}] verifier #{p.get('rank', '?')} @{p.get('output_tc', '?')}: "
                           f"{p.get('evidence', '').strip()} (suggested fix: {fx.get('op', 'n/a')})")
    except (OSError, ValueError):
        pass
    try:
        c = json.loads((E / "verify" / "checks.json").read_text())
        for ch in c.get("checks", []):
            if ch.get("status") == "FAIL" and not (ch.get("data") or {}).get("advisory"):
                out.append(f"- [{run_id}] check {ch.get('name')} FAILED: {ch.get('detail', '')}")
    except (OSError, ValueError):
        pass
    return out


def feedback_items(text: str) -> list[tuple[str, str]]:
    """(heading, item) for each filled-in bullet in a feedback.md written from docs/feedback-template.md."""
    items, head = [], ""
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("#"):
            head = s.lstrip("# ").strip()
            continue
        m = re.match(r"^[-*]\s*(.*)$", s)
        if not m:
            continue
        t = m.group(1).strip()
        low = t.lower()
        if (not t or low in ("none", "n/a", "-") or low.startswith("mm:ss") or re.fullmatch(r"score:\s*", low)
                or low.startswith("[ ]") or "<" in t and ">" in t):
            continue
        items.append((head, t))
    return items


def _feedback_lessons(runs_dir: Path, current: str):
    lessons, ingested = [], []
    if not runs_dir.is_dir():
        return lessons, ingested
    for d in sorted(runs_dir.iterdir()):
        fb = d / "feedback.md"
        if d.name == current or not fb.is_file() or (d / ".ingested").exists():
            continue
        items = feedback_items(fb.read_text())
        if not items:
            continue
        for head, t in items:
            if head.lower().startswith("overall"):
                continue
            lessons.append(f"- [{d.name} feedback] {head}: {t}")
        ingested.append(d.name)
    return lessons, ingested


def append_proposed(lessons_path: Path, new: list[str]) -> int:
    """Append to the end of '## Proposed' (creating file/section if needed). Never touches '## Approved'."""
    if lessons_path.exists():
        text = lessons_path.read_text()
    else:
        text = "# Lessons\n\n## Approved\n\n## Proposed\n"
    lines = text.splitlines()
    if not any(l.strip() == "## Proposed" for l in lines):
        lines += ["", "## Proposed"]
    existing = set(l.strip() for l in lines)
    new = [n for n in new if n.strip() not in existing]
    if not new:
        if not lessons_path.exists():
            lessons_path.write_text("\n".join(lines) + "\n")
        return 0
    pi = next(i for i, l in enumerate(lines) if l.strip() == "## Proposed")
    end = next((i for i in range(pi + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    while end > pi + 1 and not lines[end - 1].strip():
        end -= 1
    lines[end:end] = new
    lessons_path.parent.mkdir(parents=True, exist_ok=True)
    lessons_path.write_text("\n".join(lines) + "\n")
    return len(new)


# ------------------------------------------------------------ run log
def _f(v, nd=0, unit=""):
    return "n/a" if v is None else f"{v:,.{nd}f}{unit}"


def log_section(line: dict) -> str:
    p, a, e = line["predicted"], line["actual"], line["error_pct"]
    src = line.get("source") or {}
    rows = [("wall time (s)", p["wall_s"], a["wall_s"], e["wall_s"]),
            ("tokens", p["tokens"], a["tokens"], e["tokens"]),
            ("peak extra RAM (MB)", p["peak_extra_ram_mb"], a["peak_extra_ram_mb"], e["peak_extra_ram_mb"])]
    L = [f"\n## {line['run_id']}", "",
         f"Recorded {line['recorded_at']}. Source {_f((src.get('duration_s') or 0) / 60 if src.get('duration_s') else None, 1)} min "
         f"{src.get('width', '?')}x{src.get('height', '?')}. Outcome: {line['outcome']['status']} "
         f"(checks {line['outcome']['checks']}, verifier {line['outcome']['verdict']}, {line['outcome']['problems']} problem(s)). "
         f"Output {_f(a.get('output_s'), 1)} s.", "",
         "| metric | predicted (mid) | actual | error % |", "|---|---|---|---|"]
    for n, pv, av, ev in rows:
        L.append(f"| {n} | {_f(pv)} | {_f(av)} | {_f(ev, 1)} |")
    for s in STEPS:
        pv = p["steps"].get(s)
        av = (a["steps"].get(s) or {}).get("wall_s")
        if pv is not None or av is not None:
            L.append(f"| step {s} wall (s) | {_f(pv)} | {_f(av)} | {_f(_err(pv, av), 1)} |")
    if line["missing"]:
        L += ["", "Missing inputs (recorded as null): " + ", ".join(line["missing"]) + "."]
    return "\n".join(L) + "\n"


def record(run_dir: Path, history_dir: Path, runs_dir: Path | None = None, force: bool = False) -> dict:
    run_dir = Path(run_dir).resolve()
    history_dir = Path(history_dir)
    runs_dir = Path(runs_dir).resolve() if runs_dir else run_dir.parent
    if not run_dir.is_dir():
        raise SystemExit(f"run dir not found: {run_dir}")
    E = run_dir / "edit"
    run_id = run_dir.name
    history_dir.mkdir(parents=True, exist_ok=True)
    runs_p = history_dir / "runs.jsonl"
    existing = _read_jsonl(runs_p)
    if any(r.get("run_id") == run_id for r in existing):
        if not force:
            print(f"{run_id} already recorded; use --force to replace")
            return next(r for r in existing if r.get("run_id") == run_id)
        existing = [r for r in existing if r.get("run_id") != run_id]
    line = build_line(run_id, E)
    lessons = _failure_lessons(run_id, E)
    fb_lessons, ingested = _feedback_lessons(runs_dir, run_id)
    line["feedback_ingested"] = ingested
    added = append_proposed(history_dir / "LESSONS.md", lessons + fb_lessons)
    for rid in ingested:
        (runs_dir / rid / ".ingested").write_text(datetime.now(timezone.utc).isoformat(timespec="seconds") + "\n")
    runs_p.write_text("".join(json.dumps(r) + "\n" for r in existing + [line]))
    log_p = history_dir / "RUN_LOG.md"
    if not log_p.exists():
        log_p.write_text("# Run log\n")
    with log_p.open("a") as fh:
        fh.write(log_section(line))
    line["_lessons_added"] = added
    return line


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Record a finished run into history/")
    ap.add_argument("--run-dir", required=True, type=Path)
    ap.add_argument("--history-dir", type=Path, default=PROJECT_ROOT / "history")
    ap.add_argument("--runs-dir", type=Path, default=None, help="folder scanned for other runs' feedback.md (default: parent of run-dir)")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)
    line = record(a.run_dir, a.history_dir, a.runs_dir, a.force)
    if line.get("missing"):
        print("missing inputs (recorded as null): " + ", ".join(line["missing"]))
    e = line["error_pct"]
    print(f"recorded {line['run_id']}: outcome {line['outcome']['status']}; error % wall {e['wall_s']} tokens {e['tokens']} "
          f"ram {e['peak_extra_ram_mb']}; lessons added {line.get('_lessons_added', 0)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
