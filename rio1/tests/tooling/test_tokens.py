import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from helpers import tokens  # noqa: E402

TOK = ROOT / "helpers" / "tokens.py"
SID = "93df0e52-29cf-4f90-b30c-620e9a0c9bb3"

# Trimmed from a real `claude -p --model sonnet --output-format json "reply with OK"` (2.1.305).
SAMPLE = {
    "type": "result", "subtype": "success", "is_error": False, "num_turns": 1,
    "session_id": SID, "total_cost_usd": 0.0665263, "result": "OK",
    "usage": {"input_tokens": 2, "cache_creation_input_tokens": 16055,
              "cache_read_input_tokens": 21379, "output_tokens": 4},
    "modelUsage": {"claude-sonnet-5-5": {"inputTokens": 2, "outputTokens": 4}},
    "subagent_stats": {"spawned": 0},
}


def run(tmp_path, result, projects, cwd="/some/project dir"):
    rp = tmp_path / "claude_result.json"
    rp.write_text(json.dumps(result))
    return subprocess.run([sys.executable, str(TOK), "--result", str(rp), "--edit-dir", str(tmp_path),
                           "--cwd", cwd, "--projects-dir", str(projects)], capture_output=True, text=True)


def assistant(mid, inp, out, cr=0, cc=0, block=0):
    return json.dumps({"type": "assistant", "apiBlockIndex": block, "message": {
        "id": mid, "usage": {"input_tokens": inp, "output_tokens": out,
                             "cache_read_input_tokens": cr, "cache_creation_input_tokens": cc}}}) + "\n"


def test_slug():
    assert tokens.project_slug("/Users/a/Claude Code/Editing Agent") == "-Users-a-Claude-Code-Editing-Agent"


def test_parses_result_json_without_transcripts(tmp_path):
    p = run(tmp_path, SAMPLE, tmp_path / "no-projects")
    assert p.returncode == 0, p.stderr
    t = json.loads((tmp_path / "tokens.json").read_text())
    assert t["main"] == {"input": 2, "output": 4, "cache_read": 21379, "cache_creation": 16055}
    assert t["subagents"] == {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}
    assert t["total"] == 2 + 4 + 21379 + 16055
    assert t["num_turns"] == 1
    assert t["cost_usd_equiv"] == 0.0665
    assert t["source"] == "claude-json"


def test_sums_transcripts_and_subagents(tmp_path):
    cwd = "/some/project dir"
    proj = tmp_path / "projects" / tokens.project_slug(cwd)
    (proj / SID / "subagents").mkdir(parents=True)
    # Same message id on two lines (one per content block) must count once.
    (proj / f"{SID}.jsonl").write_text(
        json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n"
        + assistant("m1", 10, 5, 100, 50, 0) + assistant("m1", 10, 5, 100, 50, 1)
        + assistant("m2", 1, 2, 3, 4) + "not json\n")
    (proj / SID / "subagents" / "agent-a1.jsonl").write_text(assistant("s1", 1000, 200, 0, 0))
    (proj / SID / "subagents" / "agent-a2.jsonl").write_text(assistant("s2", 1, 1, 1, 1))
    p = run(tmp_path, SAMPLE, tmp_path / "projects", cwd)
    assert p.returncode == 0, p.stderr
    t = json.loads((tmp_path / "tokens.json").read_text())
    assert t["main"] == {"input": 11, "output": 7, "cache_read": 103, "cache_creation": 54}
    assert t["subagents"] == {"input": 1001, "output": 201, "cache_read": 1, "cache_creation": 1}
    assert t["total"] == 175 + 1204
    assert t["source"] == "transcripts"


def test_bad_result_fails(tmp_path):
    rp = tmp_path / "claude_result.json"
    rp.write_text("not json")
    p = subprocess.run([sys.executable, str(TOK), "--result", str(rp), "--edit-dir", str(tmp_path)],
                       capture_output=True, text=True)
    assert p.returncode == 1 and "cannot read result JSON" in p.stderr


def test_reads_the_stream_json_file(tmp_path):
    """edit.sh runs claude with --output-format stream-json; the final result line counts."""
    stream = tmp_path / "claude_stream.jsonl"
    early = dict(SAMPLE, num_turns=99)
    stream.write_text("\n".join([json.dumps({"type": "system", "subtype": "init", "session_id": SID}),
                                 json.dumps({"type": "assistant", "message": {"id": "m1"}}),
                                 "not json (a stray line)",
                                 json.dumps(early), json.dumps(SAMPLE)]) + "\n")
    p = subprocess.run([sys.executable, str(TOK), "--result", str(stream), "--edit-dir", str(tmp_path),
                        "--projects-dir", str(tmp_path / "none")], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    t = json.loads((tmp_path / "tokens.json").read_text())
    assert t["num_turns"] == 1 and t["total"] == 2 + 16055 + 21379 + 4 and t["source"] == "claude-json"


def test_stream_without_result_fails(tmp_path):
    stream = tmp_path / "claude_stream.jsonl"
    stream.write_text(json.dumps({"type": "system"}) + "\n" + json.dumps({"type": "assistant"}) + "\n")
    p = subprocess.run([sys.executable, str(TOK), "--result", str(stream), "--edit-dir", str(tmp_path)],
                       capture_output=True, text=True)
    assert p.returncode == 1 and "no result object" in p.stderr
