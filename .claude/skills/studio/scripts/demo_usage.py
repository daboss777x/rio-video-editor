#!/usr/bin/env python3
"""Writes a fake headless run for the Session panel demo (and tests), one phase at a time:
  demo_usage.py DIR <1-4>
DIR gets claude_stream.jsonl (stream-json with system init, rate_limit_event and, at phase 4, the
result line), claude_result.json (phase 4) and claude/projects/-studio-demo/<session>.jsonl with
sub-agent transcripts. Files only grow, like a real run. Point studio.py at it with
  CLAUDE_CONFIG_DIR=DIR/claude STUDIO_USAGE_DIR=DIR studio.py usage --cwd /studio-demo --stream DIR/claude_stream.jsonl
"""
import json, os, sys, time
from datetime import datetime, timezone

SID = "5e55d300-0000-4000-8000-000000000001"
CWD = "/studio-demo"


def ts():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def append(path, objs):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for o in objs:
            f.write(json.dumps(o) + "\n")


def msgs(prefix, n, inp, out, cr, cw):
    rows = []
    for i in range(n):
        u = {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": cr, "cache_creation_input_tokens": cw}
        mid = "msg_%s_%d_%d" % (prefix, i, int(time.time() * 1000))
        for block in ("thinking", "text"):  # one line per content block, same id: must be counted once
            rows.append({"type": "assistant", "timestamp": ts(), "sessionId": SID,
                         "message": {"id": mid, "role": "assistant", "content": [{"type": block}], "usage": u}})
    return rows


def agent(base, aid, kind, desc, rows):
    sub = os.path.join(base, SID, "subagents")
    os.makedirs(sub, exist_ok=True)
    with open(os.path.join(sub, "agent-%s.meta.json" % aid), "w") as f:
        json.dump({"agentType": kind, "description": desc}, f)
    append(os.path.join(sub, "agent-%s.jsonl" % aid), rows)


def rate(pct, resets):
    return {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "unifiedWindows": {
        "five_hour": {"utilization": pct / 100.0, "resetsAt": resets},
        "seven_day": {"utilization": 0.38, "resetsAt": resets + 4 * 86400}}}, "session_id": SID}


def main(d, phase):
    base = os.path.join(d, "claude", "projects", "-studio-demo")
    main_t = os.path.join(base, SID + ".jsonl")
    stream = os.path.join(d, "claude_stream.jsonl")
    resets = int(time.time()) + 2 * 3600 + 17 * 60
    if phase == 1:
        append(stream, [{"type": "system", "subtype": "init", "session_id": SID, "cwd": CWD, "model": "claude-sonnet"},
                        rate(12, resets)])
        append(main_t, [{"type": "user", "timestamp": ts(), "sessionId": SID}] + msgs("main", 6, 3, 900, 38000, 6000))
    elif phase == 2:
        for k in range(1, 5):
            agent(base, "edl%d" % k, "edl-editor", "Cut chunk %d/4" % k, msgs("edl%d" % k, 9, 4, 2600, 52000, 9000))
        append(main_t, msgs("main2", 4, 2, 500, 41000, 1500))
        append(stream, [rate(31, resets)])
    elif phase == 3:
        agent(base, "ver1", "edit-verifier", "Verify final.mp4", msgs("ver", 8, 3, 1800, 47000, 7000))
        agent(base, "exp1", "Explore", "Find the loudnorm helper", msgs("exp", 2, 2, 300, 12000, 2000))
        append(stream, [rate(39, resets)])
    else:
        agent(base, "fix1", "edl-editor", "Fix pass", msgs("fix", 3, 2, 1200, 50000, 3000))
        append(main_t, msgs("main3", 3, 2, 700, 44000, 1000))
        res = {"type": "result", "subtype": "success", "is_error": False, "session_id": SID,
               "total_cost_usd": 4.87, "result": "REPORT.md written"}
        append(stream, [rate(44, resets), res])
        with open(os.path.join(d, "claude_result.json"), "w") as f:
            json.dump(res, f)


if __name__ == "__main__":
    try:
        main(sys.argv[1], int(sys.argv[2]))
    except Exception as ex:  # a demo helper must never stop the demo
        sys.stderr.write("demo_usage: %s\n" % ex)
