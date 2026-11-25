import json
import statistics

import pytest

from helpers import estimate as est
from helpers import record_run as rr

SRC = {"duration_s": 3600.0, "width": 2880, "height": 1800, "fps": "30/1", "vfr": False}


def test_estimate_shape_and_ranges(profile):
    e = est.estimate(profile, SRC, [])
    assert set(e["steps"]) == {"transcribe", "detect", "edl", "blur", "render", "verify", "verifier"}
    lo, mid, hi = e["totals"]["wall_s"]
    assert lo < mid < hi and hi == pytest.approx(mid * 1.35, rel=1e-3)
    assert e["calibration"]["runs_used"] == 0
    assert e["drivers"]["chunks"] == 4 and e["drivers"]["words_source"] == "estimate"
    assert e["totals"]["scribe_usd"] == pytest.approx(profile["estimator_priors"]["scribe_usd_per_hour"])  # 1 h source


def test_real_word_count_overrides_prior(profile):
    a = est.estimate(profile, SRC, [])
    b = est.estimate(profile, SRC, [], words=60000)
    assert b["drivers"]["words_source"] == "transcript" and b["totals"]["tokens"]["subagents"] > a["totals"]["tokens"]["subagents"]


def test_spread_shrinks_with_runs(profile):
    runs = [{"observed": {"coefficients": {"detect_s_per_source_min": 3.0}}}] * 8
    assert est.estimate(profile, SRC, runs)["calibration"]["spread"] < est.estimate(profile, SRC, [])["calibration"]["spread"]


def test_median_of_last_five(profile):
    vals = [100, 100, 1, 2, 3, 4, 5]
    runs = [{"observed": {"coefficients": {"detect_s_per_source_min": v}}} for v in vals]
    c, n = est.calibrate(profile["estimator_priors"], runs)
    assert c["detect_s_per_source_min"] == statistics.median([1, 2, 3, 4, 5]) and n["detect_s_per_source_min"] == 5


def test_gate_ram_and_tokens(profile):
    e = est.estimate(profile, SRC, [])
    assert est.gate(e, {"max_extra_ram_mb": 2000, "token_budget_per_window": None})["ok"]
    g = est.gate(e, {"max_extra_ram_mb": 100, "token_budget_per_window": None})
    assert not g["ok"] and any("RAM" in r for r in g["reasons"])
    g = est.gate(e, {"max_extra_ram_mb": 2000, "token_budget_per_window": 1000})
    assert not g["ok"] and any("tokens" in r for r in g["reasons"])
    g = est.gate(e, {"max_extra_ram_mb": 2000, "token_budget_per_window": None})
    assert any(r.startswith("WARN") for r in g["reasons"])


def make_run(root, name, e, factor=2.0, with_all=True):
    run = root / name
    E = run / "edit"
    (E / "verify").mkdir(parents=True)
    (E / "estimate.json").write_text(json.dumps(e))
    if not with_all:
        return run
    st = e["steps"]
    rows = [{"name": k, "wall_s": st[k]["wall_s"][1] * factor, "max_rss_mb": 900.0 if k == "render" else 100.0, "exit_code": 0}
            for k in st]
    (E / "timings.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    (E / "monitor_summary.json").write_text(json.dumps({"peak_extra_mb": 1200, "swap_growth_mb": 0}))
    sub = e["totals"]["tokens"]["subagents"]
    (E / "tokens.json").write_text(json.dumps({"main": {"input": 1000, "output": 1000, "cache_read": 0, "cache_creation": 0},
                                               "subagents": {"input": int(sub * factor), "output": 0, "cache_read": 0, "cache_creation": 0},
                                               "total": 0}))
    (E / "verify" / "checks.json").write_text(json.dumps({"overall": "PASS", "checks": [
        {"name": "duration", "status": "PASS", "detail": "", "data": {"final_s": 1200.0}}]}))
    return run


def test_estimate_moves_toward_actual_after_recorded_run(tmp_path, profile):
    e1 = est.estimate(profile, SRC, [])
    run = make_run(tmp_path / "runs", "2026-01-01_0000_a", e1, factor=2.0)
    hist = tmp_path / "history"
    line = rr.record(run, hist)
    # observed ratios are 2x the prior
    pri = profile["estimator_priors"]
    assert line["observed"]["coefficients"]["detect_s_per_source_min"] == pytest.approx(2 * pri["transcribe_s_per_source_min"] * 0 + 2 * pri["detect_s_per_source_min"], rel=0.01)
    runs = est.load_history(hist / "runs.jsonl")
    e2 = est.estimate(profile, SRC, runs)
    assert e2["calibration"]["runs_used"] == 1
    for step in ("transcribe", "detect", "edl", "verifier"):
        a, b = e1["steps"][step]["wall_s"][1], e2["steps"][step]["wall_s"][1]
        assert b == pytest.approx(2 * a, rel=0.02), step
    assert e2["totals"]["tokens"]["subagents"] > 1.9 * e1["totals"]["tokens"]["subagents"]
    # error % on the recorded run: predicted is half of actual -> -50%
    assert line["error_pct"]["wall_s"] == pytest.approx(-50.0, abs=1.0)
    assert (hist / "RUN_LOG.md").read_text().count("| wall time (s) |") == 1


def test_record_run_tolerates_missing_files(tmp_path):
    run = tmp_path / "runs" / "2026-01-02_0000_b"
    (run / "edit").mkdir(parents=True)
    hist = tmp_path / "history"
    line = rr.record(run, hist)
    assert line["actual"]["wall_s"] is None and line["error_pct"] == {"wall_s": None, "tokens": None, "peak_extra_ram_mb": None}
    assert "estimate.json" in line["missing"] and "timings.jsonl" in line["missing"]
    assert line["outcome"]["status"] == "UNKNOWN"
    assert "n/a" in (hist / "RUN_LOG.md").read_text()
    assert (hist / "LESSONS.md").read_text().count("## Approved") == 1
    assert rr.main(["--run-dir", str(run), "--history-dir", str(hist)]) == 0  # second time: no-op
    assert len((hist / "runs.jsonl").read_text().splitlines()) == 1


def test_lessons_from_failures_and_feedback_never_touch_approved(tmp_path, profile):
    runs = tmp_path / "runs"
    e1 = est.estimate(profile, SRC, [])
    run = make_run(runs, "2026-01-03_0000_c", e1)
    (run / "edit" / "verify" / "verdict.json").write_text(json.dumps({"verdict": "FAIL", "problems": [
        {"rank": 1, "output_tc": "03:12.4", "evidence": "clipped word 'save'", "fix": {"op": "extend"}}]}))
    old = runs / "2026-01-01_0000_old"
    old.mkdir(parents=True)
    tmpl = "# Feedback\n## Dropped steps\n- mm:ss - what is missing\n## Over-cuts\n- 05:10 - explanation of the API key was lost\n## Overall (1-5)\n- score: 3\n## Notes\n- \n"
    (old / "feedback.md").write_text(tmpl)
    blank = runs / "2026-01-02_0000_blank"
    blank.mkdir()
    (blank / "feedback.md").write_text("# F\n## Dropped steps\n- mm:ss - what is missing\n## Overall (1-5)\n- score: \n")
    hist = tmp_path / "history"
    hist.mkdir()
    (hist / "LESSONS.md").write_text("# Lessons\n\n## Approved\n- keep all typing tails\n\n## Proposed\n<!-- x -->\n")
    rr.record(run, hist)
    text = (hist / "LESSONS.md").read_text()
    approved, proposed = text.split("## Proposed")
    assert approved == "# Lessons\n\n## Approved\n- keep all typing tails\n\n"
    assert "clipped word 'save'" in proposed and "explanation of the API key was lost" in proposed
    assert "what is missing" not in proposed
    assert (old / ".ingested").exists() and not (blank / ".ingested").exists()
    # second run does not re-ingest
    run2 = make_run(runs, "2026-01-04_0000_d", e1)
    rr.record(run2, hist)
    assert (hist / "LESSONS.md").read_text().count("explanation of the API key was lost") == 1


def test_lessons_file_created_when_missing(tmp_path):
    run = tmp_path / "runs" / "r"
    (run / "edit").mkdir(parents=True)
    rr.record(run, tmp_path / "h")
    t = (tmp_path / "h" / "LESSONS.md").read_text()
    assert "## Approved" in t and "## Proposed" in t


def test_outer_claude_row_is_total_not_summed(tmp_path, profile):
    e1 = est.estimate(profile, SRC, [])
    run = make_run(tmp_path / "runs", "2026-01-05_0000_e", e1, factor=1.0)
    tp = run / "edit" / "timings.jsonl"
    tp.write_text(tp.read_text() + "\n" + json.dumps({"name": "claude", "wall_s": 99999.0, "max_rss_mb": 1, "exit_code": 0}))
    line = rr.record(run, tmp_path / "h")
    assert line["actual"]["wall_s_all_steps"] == 99999.0
    assert line["actual"]["wall_s"] == pytest.approx(sum(v["wall_s"][1] for v in e1["steps"].values()), rel=0.01)
    assert "claude" not in line["actual"]["steps"]


def test_claude_json_tokens_skip_token_calibration(tmp_path, profile):
    e1 = est.estimate(profile, SRC, [])
    run = make_run(tmp_path / "runs", "2026-01-06_0000_f", e1)
    (run / "edit" / "tokens.json").write_text(json.dumps({"main": {"input": 5, "output": 5}, "subagents": {}, "total": 10, "source": "claude-json"}))
    line = rr.record(run, tmp_path / "h")
    assert "tokens_orchestrator" not in line["observed"]["coefficients"]


# ------------------------------------------------------------ Rio 1 4K output + blur scan (2026-11-19)
OBS = {"duration_s": 3600.0, "width": 1920, "height": 1080, "fps": "60/1", "vfr": False}


def test_4k_render_is_sized_by_the_output_frames_not_the_recording(profile):
    e = est.estimate(profile, OBS, [])
    d, pri = e["drivers"], profile["estimator_priors"]
    assert d["render_mode"] == "4k" and d["output_megapixels"] == pytest.approx(3840 * 2160 / 1e6)
    gpx = d["output_min"] * 60 * 60 * (3840 * 2160 / 1e6) / 1000       # output frames x MP / 1000
    assert d["output_gigapixels"] == pytest.approx(gpx, rel=1e-3)
    assert e["steps"]["render"]["wall_s"][1] == pytest.approx(pri["render4k_s_per_output_gigapixel"] * gpx, rel=0.01)
    assert e["steps"]["render"]["peak_rss_mb"] == pri["render4k_peak_rss_mb"]
    e30 = est.estimate(profile, {**OBS, "fps": "30/1"}, [])                 # 30 fps: half the frames
    assert e30["steps"]["render"]["wall_s"][1] == pytest.approx(e["steps"]["render"]["wall_s"][1] / 2, rel=0.01)


def test_blur_scan_is_in_the_forecast_unless_the_user_said_no(profile):
    e, e_no = est.estimate(profile, OBS, []), est.estimate(profile, OBS, [], blur=False)
    b = e["steps"]["blur"]["wall_s"][1]
    assert b == pytest.approx(profile["estimator_priors"]["blur_s_per_output_min"] * e["drivers"]["output_min"], rel=0.01)
    assert "blur" not in e_no["steps"]
    assert e["totals"]["wall_s"][1] - e_no["totals"]["wall_s"][1] == pytest.approx(b, rel=0.01)
    assert "blur" in est.format_table(e) and "blur" not in est.format_table(e_no)


def test_a_recorded_4k_run_calibrates_the_4k_render_and_the_blur_scan(tmp_path, profile):
    e1 = est.estimate(profile, OBS, [])
    run = make_run(tmp_path / "runs", "2026-01-01_0000_a", e1, factor=2.0)   # real output: 20 min (checks)
    co = rr.record(run, tmp_path / "history")["observed"]["coefficients"]
    gpx_real = 20 * 60 * 60 * (3840 * 2160 / 1e6) / 1000
    assert co["render4k_s_per_output_gigapixel"] == pytest.approx(2 * e1["steps"]["render"]["wall_s"][1] / gpx_real, rel=0.01)
    assert co["render4k_peak_rss_mb"] == 900.0
    assert co["blur_s_per_output_min"] == pytest.approx(2 * e1["steps"]["blur"]["wall_s"][1] / 20, rel=0.01)
    assert "render_s_per_output_megapixel_min" not in co      # the old 1080p/x264 coefficient stays clean
    e2 = est.estimate(profile, OBS, est.load_history(tmp_path / "history" / "runs.jsonl"))
    assert e2["calibration"]["coefficients"]["blur_s_per_output_min"] == co["blur_s_per_output_min"]
