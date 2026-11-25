#!/usr/bin/env python3
"""Session token usage for The Studio's Session panel (stdlib only; a copy of the Davis office reader,
using the project's rio1/helpers/tokens.py for the slug and usage-field mapping when it is importable).

Reads Claude Code transcripts incrementally and splits tokens by agent:
  main thread  = ~/.claude/projects/<slug>/<session_id>.jsonl
  sub-agents   = ~/.claude/projects/<slug>/<session_id>/subagents/agent-<id>.jsonl (+ .meta.json)
  <slug>       = the cwd with every non-alphanumeric character replaced by "-"
Each API response is written once per content block with the same message.id, so
usage is deduped by message.id. A byte-offset cache means each check-in only reads
the lines added since the last one.

The 5-hour limit %: Claude Code does NOT write it to any file. It is only exposed
  - on the status line's stdin JSON: rate_limits.five_hour.used_percentage / resets_at
  - as a `rate_limit_event` in `claude -p --output-format stream-json --verbose` output
This module keeps the latest reading in a small snapshot file:
  usage.py statusline            use as (or inside) your statusLine command: saves the snapshot, prints a short line
  usage.py ratelimit FILE        pull the latest rate_limit_event out of a stream-json log
  usage.py probe                 one tiny haiku call to read it (costs ~40k mostly-cached tokens of quota; opt-in)
  usage.py show [--session-id ID] [--cwd PATH]   print the computed usage as JSON
"""
import json, os, re, subprocess, sys, time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
OFFICE = os.path.join(os.path.dirname(HERE), "floor")
# STUDIO_USAGE_DIR moves the caches and the 5-hour snapshot elsewhere (the demo uses it so its fake
# reading never reaches the real board).
_UDIR = os.environ.get("STUDIO_USAGE_DIR") or OFFICE
CACHE = os.path.join(_UDIR, ".usage-cache.json")
STREAM_CACHE = os.path.join(_UDIR, ".stream-cache.json")
RL_SNAPSHOT = os.path.join(_UDIR, ".ratelimit.json")
# The 5-hour limit is per account, so a fresh reading saved by Davis's office counts too (read-only).
RL_SHARED = (RL_SNAPSHOT,) if os.environ.get("STUDIO_USAGE_DIR") else (
    RL_SNAPSHOT, os.path.join(os.path.expanduser("~"), ".claude", "skills", "davis", "office", ".ratelimit.json"))
WINDOW_S = 5 * 3600
KEYS = ("input", "output", "cache_read", "cache_write")
SRC = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def projects_dir():
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    return os.path.join(base, "projects")


def slug(cwd):
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


try:  # reuse the project's token helpers (Editing Agent/rio1/helpers/tokens.py) when present
    sys.path.insert(0, os.path.join(os.path.dirname(HERE), "..", "..", "..", "rio1", "helpers"))
    import tokens as _tokens
    slug = _tokens.project_slug
    SRC = tuple(_tokens.USAGE_MAP[k] for k in ("input", "output", "cache_read", "cache_creation"))
except Exception:
    pass
finally:
    sys.path.pop(0)


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds") if epoch else None


def epoch(ts):
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def find_main(session_id=None, cwd=None):
    """Path of the main transcript: explicit id > $CLAUDE_CODE_SESSION_ID > newest in the cwd's slug."""
    pdir = projects_dir()
    cwd = os.path.abspath(cwd or os.getcwd())
    sid = session_id or os.environ.get("CLAUDE_CODE_SESSION_ID")
    if sid:
        p = os.path.join(pdir, slug(cwd), sid + ".jsonl")
        if os.path.exists(p):
            return p
        if os.path.isdir(pdir):
            for d in os.listdir(pdir):
                p = os.path.join(pdir, d, sid + ".jsonl")
                if os.path.exists(p):
                    return p
        if session_id:
            return None
    d = os.path.join(pdir, slug(cwd))
    if not os.path.isdir(d):
        return None
    files = [os.path.join(d, f) for f in os.listdir(d) if f.endswith(".jsonl")]
    return max(files, key=os.path.getmtime) if files else None


def scan(path, entry):
    """Read only the new complete lines of `path` into entry {"off", "ids", "first"}."""
    size = os.path.getsize(path)
    if size < entry.get("off", 0):  # rewritten: start over
        entry.clear()
    entry.setdefault("off", 0)
    entry.setdefault("ids", {})
    entry.setdefault("first", None)
    if size == entry["off"]:
        return entry
    with open(path, "rb") as f:
        f.seek(entry["off"])
        chunk = f.read(size - entry["off"])
    end = chunk.rfind(b"\n")
    if end < 0:
        return entry
    entry["off"] += end + 1
    for raw in chunk[:end].split(b"\n"):
        if entry["first"] is None and b'"timestamp"' in raw:
            try:
                entry["first"] = epoch(json.loads(raw).get("timestamp"))
            except ValueError:
                pass
        if b'"usage"' not in raw:
            continue
        try:
            o = json.loads(raw)
        except ValueError:
            continue
        m = o.get("message")
        if o.get("type") != "assistant" or not isinstance(m, dict) or not isinstance(m.get("usage"), dict):
            continue
        u = m["usage"]
        row = [int(u.get(k) or 0) for k in SRC] + [epoch(o.get("timestamp")) or 0]
        entry["ids"][m.get("id") or "anon-%d" % len(entry["ids"])] = row  # last line for an id wins
    return entry


def totals(rows, since=None):
    t = dict.fromkeys(KEYS, 0)
    for r in rows:
        if since and r[4] and r[4] < since:
            continue
        for i, k in enumerate(KEYS):
            t[k] += r[i]
    t["total"] = sum(t[k] for k in KEYS)
    return t


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, obj):
    tmp = path + ".tmp%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, separators=(",", ":"))
    os.replace(tmp, path)


def rate_limit(snapshot_paths=(RL_SNAPSHOT,)):
    """Newest still-valid 5-hour reading from any snapshot file, or None."""
    best = None
    for p in snapshot_paths:
        s = read_json(p)
        fh = (s or {}).get("five_hour") or {}
        if fh.get("resets_at") and fh["resets_at"] > time.time() and fh.get("pct") is not None:
            if not best or s.get("at", 0) > best.get("at", 0):
                best = s
    return best


def save_rate_limit(five=None, seven=None, source="", path=RL_SNAPSHOT):
    if not five:
        return None
    snap = {"five_hour": five, "seven_day": seven, "source": source, "at": time.time()}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    write_json(path, snap)
    return snap


def _pct(v):
    v = float(v or 0)
    return round(v * 100 if v <= 1.5 else v, 1)  # utilization is 0..1; tolerate an already-% value


def parse_rate_limit(o):
    """(five, seven) from one rate_limit_event object. Two shapes are accepted:
    {"rate_limit_info": {"unifiedWindows": {"five_hour": {"utilization", "resetsAt"}, "seven_day": {...}}}}
    {"rate_limit_info": {"rateLimitType": "five_hour", "utilization", "resetsAt"}}"""
    info = o.get("rate_limit_info") or {}
    five = seven = None
    w = info.get("unifiedWindows") or {}
    for name, val in w.items():
        if isinstance(val, dict) and val.get("utilization") is not None:
            r = {"pct": _pct(val["utilization"]), "resets_at": val.get("resetsAt")}
            if name == "five_hour":
                five = r
            elif name == "seven_day":
                seven = r
    kind = info.get("rateLimitType")
    if kind and info.get("utilization") is not None:
        r = {"pct": _pct(info["utilization"]), "resets_at": info.get("resetsAt")}
        if kind == "five_hour":
            five = five or r
        elif kind.startswith("seven_day"):
            seven = seven or r
    return five, seven


def from_stream(path):
    """Latest rate_limit_event in a `claude -p --output-format stream-json --verbose` log."""
    e = scan_stream(path, {})
    return e.get("five"), e.get("seven")


_SID = re.compile(rb'"session_id"\s*:\s*"([^"]+)"')
_RESULT = re.compile(rb'"type"\s*:\s*"result"')


def scan_stream(path, entry):
    """Read only the new complete lines of a stream-json log into entry
    {"off", "ino", "session_id", "five", "seven", "cost_usd", "result"}. Huge assistant/tool lines
    are skipped by a byte test before any JSON parsing, so a 30-second check-in stays cheap."""
    st = os.stat(path)
    if st.st_size < entry.get("off", 0) or entry.get("ino") not in (None, st.st_ino):
        entry.clear()  # rewritten or replaced: start over
    entry["ino"] = st.st_ino
    off = entry.setdefault("off", 0)
    if st.st_size == off:
        return entry
    with open(path, "rb") as f:
        f.seek(off)
        chunk = f.read(st.st_size - off)
    end = chunk.rfind(b"\n")
    if end < 0:
        return entry
    entry["off"] = off + end + 1
    for raw in chunk[:end].split(b"\n"):
        if not entry.get("session_id"):
            m = _SID.search(raw[:4000])
            if m:
                entry["session_id"] = m.group(1).decode("utf-8", "replace")
        if len(raw) < 20000 and b'"rate_limit_event"' in raw:
            try:
                five, seven = parse_rate_limit(json.loads(raw))
            except (ValueError, AttributeError):
                continue
            if five:
                entry["five"] = five
            if seven:
                entry["seven"] = seven
        elif _RESULT.search(raw[:300]):
            try:
                o = json.loads(raw)
            except ValueError:
                continue
            if o.get("type") == "result":
                entry["result"] = True
                if o.get("session_id"):
                    entry["session_id"] = o["session_id"]
                if o.get("total_cost_usd") is not None:
                    entry["cost_usd"] = o["total_cost_usd"]
    return entry


def collect(namer, main_name, session_id=None, cwd=None, cache_path=CACHE,
            budget=None, snapshot_paths=RL_SHARED, cost_usd=None):
    """Usage dict for the state file, or {"error": ...}.

    namer(agent_id, meta) -> (key, display name) for a sub-agent transcript.
    main_name = (key, display name) for the main thread.
    """
    main = find_main(session_id, cwd)
    if not main:
        return {"error": "no session transcript found for %s" % os.path.abspath(cwd or os.getcwd()),
                "computed_at": iso(time.time())}
    sid = os.path.basename(main)[:-6]
    cache = read_json(cache_path, {}) or {}
    if cache.get("session") != sid:
        cache = {"session": sid, "files": {}}
    files = cache.setdefault("files", {})
    groups = {}  # key -> {"name", "rows", "runs"}

    def add(key, name, path):
        e = scan(path, files.setdefault(path, {}))
        g = groups.setdefault(key, {"name": name, "rows": [], "runs": 0})
        g["rows"].extend(e["ids"].values())
        g["runs"] += 1
        return e

    first = add(main_name[0], main_name[1], main)["first"]
    sub = os.path.join(os.path.dirname(main), sid, "subagents")
    if os.path.isdir(sub):
        for f in sorted(os.listdir(sub)):
            if not (f.startswith("agent-") and f.endswith(".jsonl")):
                continue
            aid = f[len("agent-"):-len(".jsonl")]
            meta = read_json(os.path.join(sub, "agent-%s.meta.json" % aid), {}) or {}
            key, name = namer(aid, meta)
            add(key, name, os.path.join(sub, f))
    try:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        write_json(cache_path, cache)
    except OSError:
        pass

    now = time.time()
    rl = rate_limit(snapshot_paths)
    if rl:
        reset = rl["five_hour"]["resets_at"]
        start, wsrc = reset - WINDOW_S, "rate_limits"
    else:  # estimate: 5-hour blocks counted from the session's first message
        first = first or now
        start = first + int((now - first) // WINDOW_S) * WINDOW_S
        reset, wsrc = start + WINDOW_S, "estimate"

    every = [r for g in groups.values() for r in g["rows"]]
    tot = totals(every)
    agents = []
    for key, g in groups.items():
        t = totals(g["rows"])
        agents.append({"key": key, "name": g["name"], "runs": g["runs"], "tokens": t, "total": t["total"],
                       "pct": round(100.0 * t["total"] / tot["total"], 1) if tot["total"] else 0.0})
    agents.sort(key=lambda a: (a["key"] != main_name[0], -a["total"]))
    in_window = totals(every, since=start)["total"]
    out = {
        "session_id": sid,
        "computed_at": iso(now),
        "session_started": iso(first),
        "totals": tot,
        "cost_usd": cost_usd,
        "agents": agents,
        "window": {"start": iso(start), "resets_at": iso(reset), "source": wsrc, "tokens": in_window},
        "limit": None,
        "budget": None,
    }
    if rl:
        out["limit"] = {"pct": rl["five_hour"]["pct"], "source": rl.get("source", ""), "as_of": iso(rl.get("at")),
                        "seven_day_pct": (rl.get("seven_day") or {}).get("pct")}
    if budget:
        out["budget"] = {"tokens": int(budget), "pct": round(100.0 * in_window / int(budget), 1)}
    if not rl:
        out["note"] = "limit % not exposed by Claude Code"
    return out


# ---------------------------------------------------------------- CLI
def _statusline():
    try:
        d = json.load(sys.stdin)
    except ValueError:
        d = {}
    rl = d.get("rate_limits") or {}
    conv = lambda w: {"pct": w.get("used_percentage"), "resets_at": w.get("resets_at")} if w else None
    if rl.get("five_hour"):
        save_rate_limit(conv(rl["five_hour"]), conv(rl.get("seven_day")), "statusline")
    model = (d.get("model") or {}).get("display_name", "")
    ctx = (d.get("context_window") or {}).get("used_percentage")
    bits = [model] if model else []
    if rl.get("five_hour", {}).get("used_percentage") is not None:
        bits.append("5h %s%%" % round(rl["five_hour"]["used_percentage"]))
    if ctx is not None:
        bits.append("ctx %s%%" % round(ctx))
    print(" · ".join(bits))


def _probe():
    p = subprocess.run(["claude", "-p", "Reply with: ok", "--model", "haiku", "--max-turns", "1",
                        "--output-format", "stream-json", "--verbose"],
                       stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)
    tmp = RL_SNAPSHOT + ".probe"
    with open(tmp, "w") as f:
        f.write(p.stdout)
    five, seven = from_stream(tmp)
    os.remove(tmp)
    return save_rate_limit(five, seven, "probe")


def main(argv):
    cmd = argv[0] if argv else "show"
    try:
        if cmd == "statusline":
            _statusline()
        elif cmd == "ratelimit" and len(argv) > 1:
            five, seven = from_stream(argv[1])
            print(json.dumps(save_rate_limit(five, seven, "stream") or {"error": "no rate_limit_event in file"}))
        elif cmd == "probe":
            print(json.dumps(_probe() or {"error": "no rate_limit_event in the probe output"}))
        elif cmd == "show":
            a = list(argv[1:])
            sid = a[a.index("--session-id") + 1] if "--session-id" in a else None
            cwd = a[a.index("--cwd") + 1] if "--cwd" in a else None
            namer = lambda aid, m: (m.get("agentType") or aid, (m.get("description") or "").split(" — ")[0] or aid)
            print(json.dumps(collect(namer, ("main", "Main"), sid, cwd), indent=2))
        else:
            print(__doc__)
    except Exception as ex:  # never break a status line or a check-in
        sys.stderr.write("usage.py: %s: %s\n" % (type(ex).__name__, ex))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
