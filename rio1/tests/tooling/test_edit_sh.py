import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]  # this system's code (rio1/ or rio2/)
PROJECT = ROOT.parent  # shared .venv/, .claude/
EDIT = ROOT / "edit.sh"


@pytest.fixture
def dirs(tmp_path):
    d = {k: tmp_path / k for k in ("input", "runs", "output")}
    for p in d.values():
        p.mkdir()
    return d


def run(dirs, *args, extra_env=None, timeout=60):
    # Default the Studio to a missing script so tests never touch the real board.
    env = dict(os.environ, EDITOR_INPUT_DIR=str(dirs["input"]), EDITOR_RUNS_DIR=str(dirs["runs"]),
               EDITOR_OUTPUT_DIR=str(dirs["output"]),
               EDITOR_STUDIO_SCRIPT=str(dirs["input"].parent / "no-studio.py"))
    env.update(extra_env or {})
    return subprocess.run(["bash", str(EDIT), *args], capture_output=True, text=True, env=env, timeout=timeout)


def test_syntax_clean():
    assert subprocess.run(["bash", "-n", str(EDIT)]).returncode == 0


def test_refuses_zero_videos(dirs):
    (dirs["input"] / ".gitkeep").touch()
    (dirs["input"] / "notes.txt").write_text("x")
    p = run(dirs)
    assert p.returncode == 1
    assert "no video" in p.stderr
    assert list(dirs["runs"].iterdir()) == []


def test_refuses_pdf_and_two_plan_docs_and_duplicate_stems(dirs):
    (dirs["input"] / "a.mp4").write_bytes(b"x")
    (dirs["input"] / "plan.pdf").write_bytes(b"x")
    p = run(dirs)
    assert p.returncode == 1 and "PDF" in p.stderr
    (dirs["input"] / "plan.pdf").unlink()
    (dirs["input"] / "p1.md").write_text("x"); (dirs["input"] / "p2.txt").write_text("x")
    p = run(dirs)
    assert p.returncode == 1 and "more than one plan doc" in p.stderr
    (dirs["input"] / "p2.txt").unlink(); (dirs["input"] / "p1.md").unlink()
    (dirs["input"] / "A.mov").write_bytes(b"x")
    p = run(dirs)
    assert p.returncode == 1 and "share the name" in p.stderr
    assert sorted(x.name for x in dirs["input"].iterdir()) == ["A.mov", "a.mp4"]  # nothing moved
    assert list(dirs["runs"].iterdir()) == []


def test_estimate_mode_also_refuses_zero(dirs):
    p = run(dirs, "--estimate")
    assert p.returncode == 1 and "no video" in p.stderr


def test_unknown_option(dirs):
    p = run(dirs, "--bogus")
    assert p.returncode == 1 and "unknown option" in p.stderr


@pytest.mark.skipif(not (ROOT / "helpers" / "estimate.py").exists() or not shutil.which("ffmpeg"),
                    reason="needs helpers/estimate.py (Natalie) and ffmpeg")
def make_clip(dirs, name="My Test Clip.mp4", dur=2):
    clip = dirs["input"] / name
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"testsrc=size=320x180:rate=30:duration={dur}",
                    "-f", "lavfi", "-i", f"sine=frequency=440:duration={dur}", "-shortest",
                    "-c:v", "libx264", "-c:a", "aac", str(clip)], check=True)
    return clip


def test_dry_run_plumbing(dirs):
    clip = make_clip(dirs)
    p = run(dirs, "--dry-run", extra_env={"EDITOR_SKIP_PREFLIGHT": "1", "EDITOR_NO_RECORD": "1",
                                          "MONITOR_INTERVAL_S": "0.2"}, timeout=180)
    out = p.stdout + p.stderr
    assert p.returncode == 0, out
    (run_dir,) = list(dirs["runs"].iterdir())
    assert run_dir.name.endswith("_my-test-clip")
    assert (run_dir / clip.name).exists() and not clip.exists()  # source moved in
    e = run_dir / "edit"
    for f in ("estimate.json", "timings.jsonl", "monitor.jsonl", "monitor_summary.json", "run.log"):
        assert (e / f).exists(), f
    assert not (e / "monitor.pid").exists()
    assert "predicted vs actual" in out
    assert json.loads((e / "timings.jsonl").read_text().splitlines()[-1])["name"] == "dry-run-noop"


@pytest.mark.skipif(not (ROOT / "helpers" / "estimate.py").exists() or not shutil.which("ffmpeg"),
                    reason="needs helpers/estimate.py (Natalie) and ffmpeg")
def test_dry_run_drives_the_studio(dirs, tmp_path):
    make_clip(dirs)
    log = tmp_path / "studio_calls.jsonl"
    fake = tmp_path / "fake_studio.py"
    fake.write_text(f"import json,sys\nopen({str(log)!r},'a').write(json.dumps(sys.argv[1:])+'\\n')\nsys.exit(3)\n")
    p = run(dirs, "--dry-run", extra_env={"EDITOR_SKIP_PREFLIGHT": "1", "EDITOR_NO_RECORD": "1",
                                          "MONITOR_INTERVAL_S": "0.2", "EDITOR_STUDIO_SCRIPT": str(fake)},
            timeout=180)
    assert p.returncode == 0, p.stdout + p.stderr   # a failing studio never fails the run
    calls = [json.loads(l) for l in log.read_text().splitlines()]
    cmds = [c[0] for c in calls]
    assert calls[0][:2] == ["reset", "My Test Clip.mp4"] and cmds[1] == "open"
    assert ["busy", "producer", "Preflight checks"] in calls
    # forecast is tried first; this fake studio fails every command, so edit.sh falls back to reset.
    fc = calls.index(next(c for c in calls if c[0] == "forecast"))
    assert calls[fc][1].endswith("/edit/estimate.json")
    assert calls[fc + 1][0] == "reset" and calls[fc + 1][-2:] == ["--estimate", calls[fc][1]]
    assert not [c for c in calls if c[0] == "usage"]  # no claude, no stream, no usage calls
    assert ["stage", "archive", "skipped", "EDITOR_NO_RECORD"] in calls
    assert cmds[-1] == "done" and "--actual-json" in calls[-1]


RUN_SETTINGS = Path(os.path.realpath(ROOT)) / "config" / "run-settings.json"


def claude_argv(prompt="<prompt>"):
    return ["claude", "-p", "--model", "sonnet", "--permission-mode", "dontAsk", "--setting-sources", "project",
            "--strict-mcp-config", "--settings", str(RUN_SETTINGS), "--output-format", "stream-json",
            "--verbose", prompt]


def step_accepts_launch():
    """True when Leah's step.py allowlist accepts edit.sh's claude launch shape."""
    import sys
    sys.path.insert(0, str(ROOT))
    from helpers import step
    env_cc = os.environ.pop("CLAUDECODE", None)   # edit.sh unsets it too
    try:
        step.check_command(["caffeinate", "-i", *claude_argv()])
        return True
    except step.Rejected:
        return False
    finally:
        if env_cc is not None:
            os.environ["CLAUDECODE"] = env_cc


@pytest.mark.skipif(RUN_SETTINGS.exists(), reason="config/run-settings.json exists (Leah); refusal path untestable")
def test_run_refuses_without_run_settings(dirs):
    (dirs["input"] / "a.mp4").write_bytes(b"x")
    p = run(dirs, extra_env={"EDITOR_SKIP_PREFLIGHT": "1", "EDITOR_NO_RECORD": "1"})
    assert p.returncode == 1 and "run-settings.json missing" in p.stderr
    assert (dirs["input"] / "a.mp4").exists() and list(dirs["runs"].iterdir()) == []


@pytest.mark.skipif(not (ROOT / "helpers" / "estimate.py").exists() or not shutil.which("ffmpeg"),
                    reason="needs helpers/estimate.py (Natalie) and ffmpeg")
@pytest.mark.skipif(not RUN_SETTINGS.exists(), reason="config/run-settings.json not written yet (Leah)")
@pytest.mark.skipif(not step_accepts_launch(), reason="step.py does not accept the new claude launch yet (Leah)")
def test_run_launches_claude_with_lockdown_flags(dirs, tmp_path):
    make_clip(dirs)
    argv_log, studio_log = tmp_path / "claude_argv.json", tmp_path / "studio_calls.jsonl"
    result = {"type": "result", "subtype": "success", "num_turns": 3, "session_id": "fake-session",
              "total_cost_usd": 0.01, "usage": {"input_tokens": 10, "output_tokens": 5}}
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "claude"
    fake.write_text(
        "#!/usr/bin/env python3\nimport json,os,sys,time\n"
        f"json.dump({{'argv': sys.argv[1:], 'env': sorted(k for k in os.environ if k in "
        f"('CLAUDECODE','ELEVENLABS_API_KEY','ASSEMBLYAI_API_KEY'))}}, open({str(argv_log)!r},'w'))\n"
        "print(json.dumps({'type':'system','subtype':'init','session_id':'fake-session'}), flush=True)\n"
        "time.sleep(1.2)\n"
        "print(json.dumps({'type':'assistant','message':{'id':'m1'}}))\n"
        f"print(json.dumps({result!r}))\n")
    fake.chmod(0o755)
    studio = tmp_path / "fake_studio.py"
    studio.write_text(f"import json,sys\nopen({str(studio_log)!r},'a').write(json.dumps(sys.argv[1:])+'\\n')\n")
    p = run(dirs, extra_env={"EDITOR_SKIP_PREFLIGHT": "1", "EDITOR_NO_RECORD": "1", "MONITOR_INTERVAL_S": "0.2",
                             "EDITOR_USAGE_INTERVAL_S": "0.3", "EDITOR_STUDIO_SCRIPT": str(studio),
                             "PATH": f"{bindir}:{os.environ['PATH']}", "CLAUDECODE": "1",
                             "ELEVENLABS_API_KEY": "should-not-leak", "ASSEMBLYAI_API_KEY": "should-not-leak", "CLAUDE_CONFIG_DIR": str(tmp_path / "cc")},
            timeout=180)
    out = p.stdout + p.stderr
    assert p.returncode == 0, out
    got = json.loads(argv_log.read_text())
    assert got["env"] == []                                  # both unset before launch
    assert ["claude", *got["argv"][:-1]] == claude_argv()[:-1]
    assert "tutorial-edit" in got["argv"][-1]
    (run_dir,) = list(dirs["runs"].iterdir())
    e = run_dir / "edit"
    assert len((e / "claude_stream.jsonl").read_text().splitlines()) == 3
    assert json.loads((e / "claude_result.json").read_text()) == result
    assert json.loads((e / "tokens.json").read_text())["total"] == 15
    assert json.loads((e / "timings.jsonl").read_text().splitlines()[-1])["name"] == "claude"
    calls = [json.loads(l) for l in studio_log.read_text().splitlines()]
    stream = str(e / "claude_stream.jsonl")
    assert ["forecast", str(e / "estimate.json")] in calls
    assert not [c for c in calls if c[0] == "reset" and "--estimate" in c]   # forecast worked: no 2nd reset
    assert ["usage", "--stream", stream] in calls                            # live refresh while claude ran
    final = ["usage", "--result", str(e / "claude_result.json"), "--stream", stream]
    assert final in calls and calls.index(final) < len(calls) - 1
    assert calls[-1][0] == "fail"          # exit 0 but the fake made no final.mp4
    n = len(calls)
    import time
    time.sleep(0.8)
    assert len(studio_log.read_text().splitlines()) == n     # usage loop was killed in finalize


def scrub_source():
    text = EDIT.read_text()
    block = text[text.index("# BEGIN SCRUB"):text.index("# END SCRUB")]
    return block[block.index("<<'PYEOF'\n") + len("<<'PYEOF'\n"):block.rindex("PYEOF")]


def test_scrub_removes_key_from_text_files_only(tmp_path):
    key = "sk_test_0123456789abcdef"
    proj, e = tmp_path / "proj", tmp_path / "proj" / "runs" / "r" / "edit"
    (e / "verify").mkdir(parents=True)
    (proj / ".env").write_text(f'export ELEVENLABS_API_KEY="{key}"\n')
    (e / "claude_stream.jsonl").write_text(json.dumps({"text": f"key={key}"}) + "\n")
    (e / "verify" / "notes.txt").write_text(f"{key} and {key}")
    (e / "clean.txt").write_text("nothing here")
    binary = b"\0\0video" + key.encode()
    (e / "final.mp4").write_bytes(binary)
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"log": [key]}))
    p = subprocess.run([str(PROJECT / ".venv" / "bin" / "python"), "-", str(proj), str(e), str(state),
                        str(tmp_path / "missing.js")], input=scrub_source(), capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    assert "3 file(s)" in p.stdout
    for f in (e / "claude_stream.jsonl", e / "verify" / "notes.txt", state):
        assert key not in f.read_text() and "[REDACTED]" in f.read_text()
    assert (e / "verify" / "notes.txt").read_text() == "[REDACTED] and [REDACTED]"
    assert (e / "final.mp4").read_bytes() == binary and (e / "clean.txt").read_text() == "nothing here"


def test_scrub_without_key_is_a_noop(tmp_path):
    (tmp_path / "e").mkdir()
    (tmp_path / "e" / "a.txt").write_text("x")
    p = subprocess.run([str(PROJECT / ".venv" / "bin" / "python"), "-", str(tmp_path), str(tmp_path / "e")],
                       input=scrub_source(), capture_output=True, text=True)
    assert p.returncode == 0 and "nothing to scrub" in p.stdout



@pytest.mark.skipif(not (ROOT / "helpers" / "estimate.py").exists() or not shutil.which("ffmpeg"),
                    reason="needs helpers/estimate.py and ffmpeg")
def test_multi_clip_dry_run_with_plan_and_cache(dirs, tmp_path):
    for i, n in enumerate(("outro.mp4", "main10.mp4", "main2.mp4", "intro.mp4")):
        make_clip(dirs, n, dur=2 + i)   # different bytes -> different cache keys
    (dirs["input"] / "Cooking Guide.md").write_text("# Plan\n1. intro\n2. steps\n")
    # pre-seed the transcript cache for main2 (same bytes -> same key as the moved file)
    cache = tmp_path / "cache"
    cache.mkdir()
    key = subprocess.run([str(PROJECT / ".venv/bin/python"), str(INTAKE), "key", str(dirs["input"] / "main2.mp4")],
                         capture_output=True, text=True).stdout.strip()
    (cache / f"{key}.json").write_text('{"words": []}')
    p = run(dirs, "--dry-run", extra_env={"EDITOR_SKIP_PREFLIGHT": "1", "EDITOR_NO_RECORD": "1",
                                          "MONITOR_INTERVAL_S": "0.2", "EDITOR_CACHE_DIR": str(cache)},
            timeout=300)
    out = p.stdout + p.stderr
    assert p.returncode == 0, out
    assert "order: intro.mp4 -> main2.mp4 -> main10.mp4 -> outro.mp4" in out
    (run_dir,) = list(dirs["runs"].iterdir())
    e = run_dir / "edit"
    src = json.loads((e / "sources.json").read_text())
    assert src["order"] == ["intro", "main2", "main10", "outro"] and src["plan"] == "plan.md"
    assert src["files"]["main2"] == "main2.mp4"
    assert (e / "plan.md").read_text().startswith("# Plan")
    assert (run_dir / "Cooking Guide.md").exists() and (run_dir / "intro.mp4").exists()
    assert list(dirs["input"].iterdir()) == []
    assert (e / "transcripts" / "main2.json").exists()          # cache hit copied
    assert not (e / "transcripts" / "intro.json").exists()
    est = json.loads((e / "estimate.json").read_text())
    assert est["source"]["duration_s"] > 10                       # 2+3+4+5 s summed
    assert run_dir.name.endswith("_cooking-guide")


INTAKE = PROJECT / ".claude" / "skills" / ("tutorial-edit-2" if ROOT.name == "rio2" else "tutorial-edit") / "scripts" / "intake.py"


def test_prepare_then_finish_for_rio(dirs):
    make_clip(dirs)
    env = {"EDITOR_SKIP_PREFLIGHT": "1", "EDITOR_NO_RECORD": "1", "MONITOR_INTERVAL_S": "0.2"}
    p = run(dirs, "--prepare", extra_env=env, timeout=180)
    out = p.stdout + p.stderr
    assert p.returncode == 0, out
    (run_dir,) = list(dirs["runs"].iterdir())
    e = run_dir / "edit"
    rio = json.loads((e / "rio.json").read_text())
    assert rio["stem"] and rio["clips"] and "RIO_RUN=" in p.stdout
    assert (e / "monitor.pid").exists()  # still sampling while the chat session edits
    assert not (e / "claude_stream.jsonl").exists()
    (e / "final.mp4").write_bytes(b"x")  # what the /rio session would render
    (e / "REPORT.md").write_text("# r\n\n## Forecast vs actual\n<!-- FORECAST_VS_ACTUAL: edit.sh fills this in after the run -->\n")
    p = run(dirs, "--finish", str(run_dir), "0", extra_env=env, timeout=120)
    out = p.stdout + p.stderr
    assert p.returncode == 0, out
    assert not (e / "monitor.pid").exists()
    assert "predicted vs actual" in out
    assert "| Wall time |" in (e / "REPORT.md").read_text()
    assert (dirs["output"] / f"{rio['stem']}_edited.mp4").exists()


def test_finish_also_writes_a_premiere_xml_next_to_the_video(dirs):
    """User, 2026-11-25: every finished edit also comes with output/<name>_edited.xml for Premiere."""
    make_clip(dirs, dur=3)
    env = {"EDITOR_SKIP_PREFLIGHT": "1", "EDITOR_NO_RECORD": "1", "MONITOR_INTERVAL_S": "0.2"}
    p = run(dirs, "--prepare", extra_env=env, timeout=180)
    assert p.returncode == 0, p.stdout + p.stderr
    (run_dir,) = list(dirs["runs"].iterdir())
    e = run_dir / "edit"
    rio = json.loads((e / "rio.json").read_text())
    (clip,) = [f for f in run_dir.iterdir() if f.suffix == ".mp4"]
    (e / "edl.json").write_text(json.dumps({"version": 1, "sources": {clip.stem: f"../{clip.name}"},
                                            "ranges": [{"source": clip.stem, "start": 0.5, "end": 2.0}],
                                            "total_duration_s": 1.5}))
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "0.5", "-i", str(clip), "-t", "1.5", str(e / "final.mp4")],
                   check=True)
    (e / "REPORT.md").write_text("# r\n\n## Forecast vs actual\n<!-- FORECAST_VS_ACTUAL: edit.sh fills this in after the run -->\n")
    p = run(dirs, "--finish", str(run_dir), "0", extra_env=env, timeout=120)
    assert p.returncode == 0, p.stdout + p.stderr
    xml = dirs["output"] / f"{rio['stem']}_edited.xml"
    assert xml.exists() and (dirs["output"] / f"{rio['stem']}_edited_audio.wav").exists()
    assert f"premiere xml: {xml}" in p.stdout


def test_finish_refuses_a_run_not_prepared(dirs, tmp_path):
    d = tmp_path / "r" / "edit"
    d.mkdir(parents=True)
    p = run(dirs, "--finish", str(tmp_path / "r"))
    assert p.returncode == 1 and "rio.json missing" in p.stderr


# ------------------------------------------------------------ "any sensitive info?" (user, 2026-11-19)
PREP_ENV = {"EDITOR_SKIP_PREFLIGHT": "1", "EDITOR_NO_RECORD": "1", "MONITOR_INTERVAL_S": "0.2"}


def _stop_monitor(e):
    pid = e / "monitor.pid"
    if pid.exists():
        try:
            os.kill(int(pid.read_text().strip()), 15)
        except (ValueError, OSError):
            pass


@pytest.mark.parametrize("flag,answer,scan", [(["--sensitive", "no"], "no", False),
                                              (["--sensitive", "yes"], "yes", True),
                                              (["--sensitive=unsure"], "unsure", True),
                                              ([], "unsure", True)])   # no answer possible: Not sure
def test_prepare_records_the_sensitive_answer_and_no_skips_the_blur_forecast(dirs, flag, answer, scan):
    make_clip(dirs)
    p = run(dirs, "--prepare", *flag, extra_env=PREP_ENV, timeout=180)
    assert p.returncode == 0, p.stdout + p.stderr
    (run_dir,) = list(dirs["runs"].iterdir())
    e = run_dir / "edit"
    _stop_monitor(e)
    assert json.loads((e / "sensitive.json").read_text()) == {"answer": answer, "blur_scan": scan}
    assert f"RIO_SENSITIVE={answer}" in p.stdout
    assert ("blur" in json.loads((e / "estimate.json").read_text())["steps"]) is scan


def test_estimate_shows_the_blur_step_unless_the_answer_is_no(dirs):
    make_clip(dirs)
    with_blur, without = run(dirs, "--estimate"), run(dirs, "--estimate", "--sensitive", "no")
    assert with_blur.returncode == 0 and without.returncode == 0, with_blur.stderr + without.stderr
    lines = lambda p: [l for l in p.stdout.splitlines() if l.startswith("blur ")]  # noqa: E731
    assert len(lines(with_blur)) == 1 and lines(without) == []


def test_a_bad_sensitive_answer_is_refused(dirs):
    make_clip(dirs)
    p = run(dirs, "--estimate", "--sensitive", "maybe")
    assert p.returncode == 1 and "--sensitive" in p.stderr and "yes, no or unsure" in p.stderr


def test_a_run_by_hand_in_a_terminal_asks_the_question(dirs):
    import pty
    make_clip(dirs)
    master, slave = pty.openpty()
    os.write(master, b"n\n")
    env = dict(os.environ, EDITOR_INPUT_DIR=str(dirs["input"]), EDITOR_RUNS_DIR=str(dirs["runs"]),
               EDITOR_OUTPUT_DIR=str(dirs["output"]), EDITOR_STUDIO_SCRIPT=str(dirs["input"].parent / "x.py"),
               **PREP_ENV)
    p = subprocess.run(["bash", str(EDIT), "--dry-run"], stdin=slave, capture_output=True, text=True,
                       env=env, timeout=180)
    os.close(slave)
    os.close(master)
    assert p.returncode == 0, p.stdout + p.stderr
    assert "Any sensitive info on screen" in p.stdout + p.stderr
    (run_dir,) = list(dirs["runs"].iterdir())
    assert json.loads((run_dir / "edit" / "sensitive.json").read_text())["answer"] == "no"
