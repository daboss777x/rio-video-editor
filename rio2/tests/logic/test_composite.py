"""Rio 2.0 two-camera 4K mode: history tagging, composite estimate, cam_sync / composite_gaps / resolution checks.
Synthetic JSON only; no media."""
import json

import pytest

from helpers import estimate as est
from helpers import record_run as rr
from helpers import verify_edit as ve
from conftest import make_edit
from test_estimate_record import SRC, make_run
from test_verify_edit import SENTS, ACTIVE, by_name  # noqa: F401

CANVAS_MP = 3840 * 2160 / 1e6


# ------------------------------------------------------------ estimate
def test_normal_estimate_unchanged(profile):
    e = est.estimate(profile, SRC, [])
    assert "sync" not in e["steps"] and e["composite"]["on"] is False
    assert e["drivers"]["megapixels"] == pytest.approx(2880 * 1800 / 1e6, abs=0.001)


def test_composite_adds_sync_and_canvas_render(profile):
    n = est.estimate(profile, SRC, [])
    c = est.estimate(profile, SRC, [], composite=True, n_cams=1)
    assert list(c["steps"])[:2] == ["transcribe", "sync"]
    assert c["steps"]["sync"]["wall_s"][1] == pytest.approx(60.0, rel=0.01)  # 1 source hour -> 60 s
    assert c["steps"]["sync"]["peak_rss_mb"] == 400
    assert c["steps"]["render"]["peak_rss_mb"] == pytest.approx(1200, abs=1)
    assert c["totals"]["peak_extra_ram_mb"][1] >= 1200
    assert c["drivers"]["megapixels"] == pytest.approx(CANVAS_MP, abs=0.001)
    # 3x the normal per-output-minute render cost at screen size
    assert c["steps"]["render"]["wall_s"][1] == pytest.approx(3 * n["steps"]["render"]["wall_s"][1], rel=0.01)
    assert c["composite"]["uncalibrated"] is True and c["composite"]["canvas"] == [3840, 2160]
    assert "COMPOSITE" in est.format_table(c)


def test_canvas_comes_from_profile_layout(profile):
    profile["layout"] = {"canvas": [1920, 1080]}
    c = est.estimate(profile, SRC, [], composite=True, n_cams=1)
    assert c["drivers"]["megapixels"] == pytest.approx(1920 * 1080 / 1e6, abs=0.001)


def probe_stub(monkeypatch, durs):
    monkeypatch.setattr(est, "probe_source", lambda p: {"duration_s": durs[p.stem], "width": 2880, "height": 1800,
                                                        "fps": "30/1", "vfr": False})


def test_camera_excluded_from_transcription_cost(tmp_path, monkeypatch, profile):
    probe_stub(monkeypatch, {"main1": 1800.0, "main2": 1800.0})  # a cam stem is never probed
    base = ["--profile", "x"]
    prof = tmp_path / "p.json"
    prof.write_text(json.dumps(profile))
    hist = tmp_path / "none.jsonl"
    out1, out2 = tmp_path / "a.json", tmp_path / "b.json"
    A = tmp_path
    rc1 = est.main(["--source", str(A / "main1.mp4"), "--source", str(A / "main2.mp4"),
                    "--out", str(out1), "--profile", str(prof), "--history", str(hist)])
    rc2 = est.main(["--source", str(A / "main1.mp4"), "--source", str(A / "TDevSetupCam.MOV"), "--source", str(A / "main2.mp4"),
                    "--out", str(out2), "--profile", str(prof), "--history", str(hist)])
    assert rc1 == 0 and rc2 == 0
    a, b = json.loads(out1.read_text()), json.loads(out2.read_text())
    assert b["composite"]["on"] and b["composite"]["cams"] == 1 and not a["composite"]["on"]
    assert b["source"]["duration_s"] == a["source"]["duration_s"] == 3600.0
    assert b["source"]["clip_count"] == 2
    assert b["totals"]["scribe_usd"] == a["totals"]["scribe_usd"]
    assert b["drivers"]["chunks"] == a["drivers"]["chunks"] and b["drivers"]["words"] == a["drivers"]["words"]
    assert b["steps"]["transcribe"]["wall_s"] == a["steps"]["transcribe"]["wall_s"]
    assert "sync" in b["steps"]


def test_composite_flag_and_cam_option(tmp_path, monkeypatch, profile):
    probe_stub(monkeypatch, {"m": 600.0})
    prof = tmp_path / "p.json"
    prof.write_text(json.dumps(profile))
    for extra in (["--composite"], ["--cam", str(tmp_path / "x.mov")]):
        out = tmp_path / "o.json"
        est.main(["--source", str(tmp_path / "m.mp4"), "--out", str(out), "--profile", str(prof),
                  "--history", str(tmp_path / "h.jsonl")] + extra)
        assert json.loads(out.read_text())["composite"]["on"]


def test_run_dir_reads_cams_from_sources_json(tmp_path, monkeypatch, profile):
    probe_stub(monkeypatch, {"m": 600.0})
    run = tmp_path / "run"
    E = run / "edit"
    E.mkdir(parents=True)
    (E / "sources.json").write_text(json.dumps({"order": ["m"], "files": {"m": "m.mp4"}, "cams": {"cam1": "cam1.mov"}}))
    prof = tmp_path / "p.json"
    prof.write_text(json.dumps(profile))
    out = tmp_path / "o.json"
    est.main(["--run-dir", str(run), "--out", str(out), "--profile", str(prof), "--history", str(tmp_path / "h")])
    r = json.loads(out.read_text())
    assert r["composite"]["on"] and r["source"]["clip_count"] == 1


# ------------------------------------------------------------ record + calibration
def record_composite(tmp_path, profile, name, factor, composite):
    e = est.estimate(profile, SRC, [], composite=composite, n_cams=1 if composite else 0)
    run = make_run(tmp_path / "runs", name, e, factor=factor)
    if composite:
        (run / "edit" / "cam_sync.json").write_text(json.dumps({"ok": True, "cams": []}))
    return rr.record(run, tmp_path / "history")


def test_lines_tagged_rio2_and_composite(tmp_path, profile):
    n = record_composite(tmp_path, profile, "2026-01-01_0000_n", 2.0, False)
    c = record_composite(tmp_path, profile, "2026-01-02_0000_c", 2.0, True)
    assert n["system"] == "rio2" and n["composite"] is False
    assert c["system"] == "rio2" and c["composite"] is True
    assert "composite_render_s_per_output_megapixel_min" in c["observed"]["coefficients"]
    assert "render_s_per_output_megapixel_min" not in c["observed"]["coefficients"]
    assert c["observed"]["coefficients"]["sync_s_per_source_hour"] > 0
    assert "render_s_per_output_megapixel_min" in n["observed"]["coefficients"]


def test_cam_sync_not_ok_is_not_composite(tmp_path, profile):
    e = est.estimate(profile, SRC, [])
    run = make_run(tmp_path / "runs", "r", e)
    (run / "edit" / "cam_sync.json").write_text(json.dumps({"ok": False, "cams": []}))
    assert rr.record(run, tmp_path / "history")["composite"] is False


def test_history_filtering_by_composite(profile):
    pri = profile["estimator_priors"]
    base = pri["render_s_per_output_megapixel_min"]
    runs = [
        {"composite": True, "observed": {"coefficients": {"composite_render_s_per_output_megapixel_min": 30.0,
                                                            "render_s_per_output_megapixel_min": 999.0,
                                                            "sync_s_per_source_hour": 90.0, "detect_s_per_source_min": 5.0}}},
        {"system": "rio2", "composite": False, "observed": {"coefficients": {"render_s_per_output_megapixel_min": 8.0}}},
        {"observed": {"coefficients": {"render_s_per_output_megapixel_min": 10.0, "detect_s_per_source_min": 7.0,
                                       "composite_render_s_per_output_megapixel_min": 777.0}}},  # untagged = rio1
    ]
    coeffs, n = est.calibrate(pri, runs)
    assert coeffs["render_s_per_output_megapixel_min"] == pytest.approx(9.0)  # median(8, 10); composite line ignored
    assert n["render_s_per_output_megapixel_min"] == 2
    assert coeffs["composite_render_s_per_output_megapixel_min"] == pytest.approx(30.0)  # only the composite line
    assert coeffs["sync_s_per_source_hour"] == 90.0
    assert coeffs["detect_s_per_source_min"] == pytest.approx(6.0)  # every non-render step counts all lines
    e = est.estimate(profile, SRC, runs, composite=True, n_cams=1)
    assert e["composite"]["uncalibrated"] is False
    assert base != 9.0


# ------------------------------------------------------------ verify
@pytest.fixture
def profile(profile):
    profile["edit"]["silence_policy"] = "keep_active"
    return profile


def setup(tmp_path, monkeypatch, size=(3840, 2160), cam_sync=None, cams=None, notes=None, profile=None):
    E, _ = make_edit(tmp_path, SENTS, [ACTIVE], duration=30.0)
    ranges = [(0.9, 11.7)]
    (E / "edl.json").write_text(json.dumps({"version": 1, "sources": {"clip": "../clip.mp4"}, "grade": "none", "overlays": [],
                                           "ranges": [{"source": "clip", "start": s, "end": e} for s, e in ranges]}))
    monkeypatch.setattr(ve, "probe", lambda p: {"duration_s": 10.8, "width": size[0], "height": size[1]})
    monkeypatch.setattr(ve, "loudness", lambda p: {"integrated_lufs": -14.0, "true_peak_dbtp": -1.5})
    monkeypatch.setattr(ve, "av_durations", lambda p: {"video_s": 10.8, "audio_s": 10.8})
    if cam_sync is not None:
        (E / "cam_sync.json").write_text(json.dumps(cam_sync))
    if cams is not None:
        (E / "sources.json").write_text(json.dumps({"order": ["clip"], "files": {"clip": "clip.mp4"}, "cams": cams}))
    if notes is not None:
        (E / "composite_notes.json").write_text(json.dumps(notes))
    return E


GOOD = {"ok": True, "cams": [{"cam": "cam1", "screen": "clip", "residual_ms": 6.1, "drift": 4.8e-05}]}


def test_cam_sync_skip_without_camera(tmp_path, monkeypatch, profile):
    res = ve.run_checks(setup(tmp_path, monkeypatch), profile)
    c = by_name(res)
    assert c["cam_sync"]["status"] == "SKIP"
    assert "resolution" not in c and "composite_gaps" not in c  # non-composite output unchanged
    assert res["overall"] == "PASS"


def test_cam_sync_pass_reports_residual_and_ppm(tmp_path, monkeypatch, profile):
    c = by_name(ve.run_checks(setup(tmp_path, monkeypatch, cam_sync=GOOD, cams={"cam1": "cam1.mov"}), profile))["cam_sync"]
    assert c["status"] == "PASS"
    assert c["data"]["cams"][0]["residual_ms"] == 6.1 and c["data"]["cams"][0]["drift_ppm"] == pytest.approx(48.0)
    assert "6.1 ms" in c["detail"] and "48" in c["detail"]


def test_cam_sync_fail_when_not_ok_or_missing(tmp_path, monkeypatch, profile):
    bad = {"ok": False, "cams": [{"cam": "cam1", "residual_ms": 35.0, "drift": 1e-4}]}
    res = ve.run_checks(setup(tmp_path, monkeypatch, size=(2880, 1800), cam_sync=bad, cams={"cam1": "c.mov"}), profile)
    assert by_name(res)["cam_sync"]["status"] == "FAIL" and res["overall"] == "FAIL"
    assert "resolution" not in by_name(res)  # no composite render happened
    tmp2 = tmp_path / "two"
    tmp2.mkdir()
    res = ve.run_checks(setup(tmp2, monkeypatch, cams={"cam1": "c.mov"}), profile)  # cam listed, no sync file
    assert by_name(res)["cam_sync"]["status"] == "FAIL"


def test_composite_resolution_is_canvas_not_source(tmp_path, monkeypatch, profile):
    c = by_name(ve.run_checks(setup(tmp_path, monkeypatch, cam_sync=GOOD, cams={"cam1": "c.mov"}), profile))
    assert c["resolution"]["status"] == "PASS"
    t2 = tmp_path / "b"
    t2.mkdir()
    c = by_name(ve.run_checks(setup(t2, monkeypatch, size=(2880, 1800), cam_sync=GOOD, cams={"cam1": "c.mov"}), profile))
    assert c["resolution"]["status"] == "FAIL"
    t3 = tmp_path / "c"
    t3.mkdir()
    profile["layout"] = {"canvas": [2560, 1440]}
    c = by_name(ve.run_checks(setup(t3, monkeypatch, size=(2560, 1440), cam_sync=GOOD, cams={"cam1": "c.mov"}), profile))
    assert c["resolution"]["status"] == "PASS"


def test_composite_gaps_advisory(tmp_path, monkeypatch, profile):
    E = setup(tmp_path, monkeypatch, cam_sync=GOOD, cams={"cam1": "c.mov"},
              notes={"no_camera": [{"start": 1700.0, "end": 1712.5, "source": "clip"}]})
    res = ve.run_checks(E, profile)
    g = by_name(res)["composite_gaps"]
    assert g["status"] == "FAIL" and g["data"]["advisory"] and "1700.00-1712.50" in g["detail"]
    assert res["overall"] == "PASS"  # advisory
    t2 = tmp_path / "n"
    t2.mkdir()
    ok = by_name(ve.run_checks(setup(t2, monkeypatch, cam_sync=GOOD, cams={"cam1": "c.mov"}, notes=[]), profile))["composite_gaps"]
    assert ok["status"] == "PASS"


def test_composite_gaps_real_render_notes_no_gaps(tmp_path, monkeypatch, profile):
    """render_tutorial's real notes: empty no_camera plus an int range count must read as no gaps."""
    notes = {"composite": True, "canvas": [3840, 2160], "fps": "60/1", "ranges": 48, "no_camera": []}
    E = setup(tmp_path, monkeypatch, cam_sync=GOOD, cams={"cam1": "c.mov"}, notes=notes)
    g = by_name(ve.run_checks(E, profile))["composite_gaps"]
    assert g["status"] == "PASS" and g["data"]["gaps"] == []
