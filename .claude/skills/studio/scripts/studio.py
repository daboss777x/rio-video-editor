#!/usr/bin/env python3
"""The Studio board: a live pixel-art view of the tutorial-edit pipeline.
Writes floor/state.json + floor/state.js, which floor/index.html polls (works over file://).

  studio.py reset "<video name>" [--estimate PATH]   new run: crew on standby, stages pending, forecast loaded
  studio.py open                                     open the page (macOS `open`; no-op elsewhere)
  studio.py lead "<doing>"                           Rio's status line
  studio.py busy <id> "<task>"                       a crew member sits at the table and works
  studio.py idle <id> ["result"]                     they get up and take a break
  studio.py stage <stage> pending|running|done|failed|skipped ["note"]
  studio.py log "<msg>"                              add a line to the activity feed
  studio.py done ["summary"] [--actual-json PATH] [--wall-s N] [--tokens N] [--ram-mb N]
  studio.py fail "<reason>"                          alarm light; Rio stops the run
  studio.py show                                     print the board
  studio.py describe "<what this run is>"            the "Working on" line of the Session panel
  studio.py forecast <estimate.json>                 load or refresh the forecast without a reset
  studio.py usage [--session-id ID] [--cwd DIR] [--stream FILE] [--result FILE] [--cost USD]
                                                     measure the run's tokens, 5-hour limit % and share per crew member
  studio.py budget <tokens>|off                      token budget for the 5-hour window (used when no limit % is known)

Crew ids: lead producer transcriber watcher story render qc archivist (or their names, e.g. "kit").
Stages:  forecast transcribe screen edit render qc fix report archive

It never breaks the caller: any problem prints a warning to stderr and exits 0.
Set STUDIO_OFF=1 to make every command a silent no-op (tests, CI).
"""
import json, os, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
FLOOR = os.path.join(os.path.dirname(HERE), "floor")
STATE_JSON = os.path.join(FLOOR, "state.json")
STATE_JS = os.path.join(FLOOR, "state.js")
LOCK = os.path.join(FLOOR, ".state.lock")
CAST_JS = os.path.join(FLOOR, "cast.js")

STAGES = [
    ("forecast", "Forecast"), ("transcribe", "Transcribe"), ("screen", "Screen check"),
    ("edit", "Edit"), ("render", "Render"), ("qc", "QC"), ("fix", "Fix pass"),
    ("report", "Report"), ("archive", "Archive"),
]
STAGE_ALIASES = {
    "estimate": "forecast", "preflight": "forecast", "probe": "forecast",
    "transcription": "transcribe", "scribe": "transcribe",
    "screen-check": "screen", "screen_check": "screen", "detect": "screen", "activity": "screen",
    "edl": "edit", "story": "edit", "verify": "qc", "verifier": "qc", "check": "qc",
    "fix-pass": "fix", "fix_pass": "fix", "record": "archive",
}
STATUSES = {"pending", "running", "done", "failed", "skipped"}
STATUS_ALIASES = {"start": "running", "started": "running", "busy": "running", "ok": "done",
                  "finished": "done", "pass": "done", "error": "failed", "fail": "failed", "skip": "skipped"}
# estimate.json step keys -> stages (CONTRACTS.md, "Estimate and history")
STEP_TO_STAGE = {"transcribe": "transcribe", "detect": "screen", "edl": "edit", "render": "render",
                 "verify": "qc", "verifier": "qc"}
MAX_LOG = 60


class Warn(Exception):
    pass


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def parse_iso(s):
    try:
        import calendar
        return calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ"))
    except Exception:
        return None


IDLE_CLOCK_OUT_S = 600  # idle this long (not busy) -> clocked out; computed on read, nothing runs in the background


WAIT_STAGES = {"transcriber": ["transcribe"], "watcher": ["screen"], "story": ["edit"], "render": ["render"],
               "qc": ["qc"], "archivist": ["report", "archive"]}
WAIT_FIX = ("story", "render", "qc")  # the fix pass can call these back until the fix stage is done / skipped


def crew_waiting(s, k):
    """Mirror of crewWaiting() in floor/index.html: stays seated while a stage of theirs is still to come in this run."""
    lead = s.get("lead") or {}
    if s.get("ended_at") or lead.get("done_at") or lead.get("failed_at") or k not in WAIT_STAGES:
        return False
    st = lambda n: (s.get("stages", {}).get(n) or {}).get("status", "pending")
    if k == "archivist" and st("render") == "pending":
        return False
    return any(st(n) in ("pending", "running") for n in WAIT_STAGES[k]) or (k in WAIT_FIX and st("fix") == "pending")


def clocked_out(s, a, at=None, key=None):
    """True if crew member `a` is not busy and has been idle >= IDLE_CLOCK_OUT_S (since, else the run start)."""
    if a.get("status") == "busy" or (key and crew_waiting(s, key)):
        return False
    ref = parse_iso(a.get("since") or s.get("started_at") or "")
    return ref is not None and (time.time() if at is None else at) - ref >= IDLE_CLOCK_OUT_S


def cast():
    with open(CAST_JS, encoding="utf-8") as f:
        text = f.read()
    return json.loads(text.split("=", 1)[1].strip().rstrip(";"))


def blank(video=""):
    c = cast()
    t = now()
    return {
        "video": video,
        "started_at": t,
        "ended_at": None,
        "updated": t,
        "lead": {"doing": "Reading the brief", "done_at": None, "failed_at": None, "summary": "", "fail_reason": ""},
        "stage": None,
        "stages": {k: {"status": "pending", "note": "", "since": None, "ended": None} for k, _ in STAGES},
        "crew": {k: {"status": "idle", "task": "", "since": None, "last": ""} for k in c if k != "lead"},
        "estimate": None,
        "actual": None,
        "description": "",
        "usage": None,
        "usage_src": None,
        "budget": None,
        "log": [],
    }


def load():
    if not os.path.exists(STATE_JSON):
        return blank()
    with open(STATE_JSON, encoding="utf-8") as f:
        s = json.load(f)
    b = blank(s.get("video", ""))
    for k, v in b.items():
        s.setdefault(k, v)
    for k, v in b["crew"].items():
        s["crew"].setdefault(k, v)
    for k, v in b["stages"].items():
        s["stages"].setdefault(k, v)
    return s


def write_atomic(path, text):
    tmp = path + ".tmp%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def save(s):
    s["updated"] = now()
    s["log"] = s["log"][-MAX_LOG:]
    body = json.dumps(s, ensure_ascii=False)
    write_atomic(STATE_JSON, body)
    write_atomic(STATE_JS, "window.STUDIO_STATE = " + body + ";\n")


def log(s, who, msg):
    s["log"].append({"t": now(), "who": who, "msg": msg})


def crew_key(s, name):
    k = (name or "").lower().strip()
    if k in s["crew"] or k == "lead":
        return k
    for cid, v in cast().items():
        if v.get("name", "").lower() == k:
            return cid
    raise Warn("unknown crew id '%s' (crew: lead, %s)" % (name, ", ".join(s["crew"])))


def stage_key(name):
    k = (name or "").lower().strip()
    k = STAGE_ALIASES.get(k, k)
    if k not in dict(STAGES):
        raise Warn("unknown stage '%s' (stages: %s)" % (name, " ".join(k for k, _ in STAGES)))
    return k


def flag(args, name):
    if name in args:
        i = args.index(name)
        val = args[i + 1] if i + 1 < len(args) else None
        del args[i:i + 2]
        return val
    return None


def num(v):
    """A number from a number, a [lo, mid, hi] list (-> mid) or {"total": ...}."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, (list, tuple)) and v:
        return num(v[len(v) // 2])
    if isinstance(v, dict):
        for k in ("total", "mid", "value"):
            if k in v:
                return num(v[k])
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return None
    return None


def triple(v):
    """[lo, mid, hi] from a list or a single number."""
    if isinstance(v, dict):
        v = v.get("total", v.get("mid"))
    if isinstance(v, (list, tuple)) and len(v) >= 3:
        try:
            return [float(v[0]), float(v[1]), float(v[2])]
        except (TypeError, ValueError):
            return None
    n = num(v)
    return [n, n, n] if n is not None else None


def read_estimate(path):
    with open(path, encoding="utf-8") as f:
        e = json.load(f)
    tot = e.get("totals", {}) or {}
    steps = {}
    for step, val in (e.get("steps") or {}).items():
        st = STEP_TO_STAGE.get(step)
        w = triple((val or {}).get("wall_s")) if isinstance(val, dict) else None
        if st and w:
            prev = steps.get(st)
            steps[st] = [a + b for a, b in zip(prev, w)] if prev else w
    src = e.get("source", {}) or {}
    gate = e.get("gate", {}) or {}
    return {
        "wall_s": triple(tot.get("wall_s")),
        "tokens": triple(tot.get("tokens")),
        "peak_ram_mb": triple(tot.get("peak_extra_ram_mb", tot.get("peak_ram_mb"))),
        "scribe_usd": num(tot.get("scribe_usd")),
        "source_s": num(src.get("duration_s")),
        "steps": steps,
        "gate_ok": gate.get("ok", True),
        "gate_reasons": gate.get("reasons", []) or [],
        "path": path,
    }


def read_actual(path):
    with open(path, encoding="utf-8") as f:
        a = json.load(f)
    out = {}
    pools = [a] + [a[k] for k in ("actual", "actuals", "totals") if isinstance(a.get(k), dict)]
    keys = {"wall_s": ("wall_s", "wall_time_s", "elapsed_s"),
            "tokens": ("tokens", "tokens_total", "total_tokens"),
            "peak_ram_mb": ("peak_extra_ram_mb", "peak_ram_mb", "peak_rss_mb", "max_rss_mb")}
    for out_key, names in keys.items():
        for pool in pools:
            for n in names:
                if n in pool and num(pool[n]) is not None:
                    out.setdefault(out_key, num(pool[n]))
    return out


AGENT_TO_CREW = {"edl-editor": "story", "edit-verifier": "qc"}


def crew_namer():
    """(agent_id, meta) -> (key, name): edl-editor = Rosa, edit-verifier = Ines, then a cast id or
    name, else the agent type itself."""
    c = cast()
    names = {v.get("name", "").lower(): k for k, v in c.items()}

    def namer(aid, meta):
        t = str(meta.get("agentType") or "").strip()
        low = t.lower()
        k = AGENT_TO_CREW.get(low) or (low if low in c else names.get(low))
        if k:
            return k, c[k].get("name", k)
        return (t or aid), (t or "agent " + aid[:8])
    return namer, ("lead", c.get("lead", {}).get("name", "Rio"))


def read_result(path):
    """session_id and total_cost_usd from `claude -p --output-format json` (or a stream-json log)."""
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    try:
        o = json.loads(text)
        objs = o if isinstance(o, list) else [o]
    except ValueError:
        objs = []
        for line in text.splitlines():
            if '"result"' in line[:200]:
                try:
                    objs.append(json.loads(line))
                except ValueError:
                    pass
    out = {}
    for o in objs:
        if isinstance(o, dict) and (o.get("type") in (None, "result")):
            if o.get("session_id"):
                out["session_id"] = o["session_id"]
            if o.get("total_cost_usd") is not None:
                out["cost_usd"] = o["total_cost_usd"]
    return out


def measure(s, opts):
    """Fill s["usage"]. Sources, in priority: flags > the stream/result files > what was remembered
    from the last call (so `done` and `budget` can refresh without arguments)."""
    import usage as U
    src = dict(s.get("usage_src") or {})
    for k in ("cwd", "stream", "result"):
        if opts.get(k):
            src[k] = os.path.abspath(opts[k])
    if opts.get("session-id"):
        src["session_id"] = opts["session-id"]
    problems = []
    rl = None
    if src.get("stream"):
        try:
            cache = U.read_json(U.STREAM_CACHE, {}) or {}
            e = cache.setdefault(src["stream"], {})
            U.scan_stream(src["stream"], e)
            U.write_json(U.STREAM_CACHE, {src["stream"]: e})  # only the current run's stream is kept
            if e.get("session_id") and not opts.get("session-id"):
                src["session_id"] = e["session_id"]
            if e.get("cost_usd") is not None:
                src["cost_usd"] = e["cost_usd"]
            if e.get("five"):
                rl = U.save_rate_limit(e["five"], e.get("seven"), "stream")
        except OSError as ex:
            problems.append("stream not readable yet (%s)" % ex.strerror)
    if src.get("result"):
        try:
            r = read_result(src["result"])
            if r.get("session_id") and not opts.get("session-id"):
                src["session_id"] = r["session_id"]
            if r.get("cost_usd") is not None:
                src["cost_usd"] = r["cost_usd"]
        except (OSError, ValueError) as ex:
            problems.append("result not readable (%s)" % (getattr(ex, "strerror", None) or ex))
    if num(opts.get("cost")) is not None:
        src["cost_usd"] = num(opts["cost"])
    s["usage_src"] = src
    if (src.get("stream") or src.get("result")) and not src.get("session_id"):
        # never guess the newest transcript here: during a run that could be someone else's session
        u = {"error": "waiting for the run's session id", "computed_at": U.iso(time.time())}
    else:
        namer, main = crew_namer()
        u = U.collect(namer, main, src.get("session_id"), src.get("cwd"), budget=s.get("budget"),
                      snapshot_paths=U.RL_SHARED, cost_usd=src.get("cost_usd"))
    if u.get("error"):
        rl = rl or U.rate_limit(U.RL_SHARED)
        if rl:  # the 5-hour % is account-wide, so it is still worth showing
            u["limit"] = {"pct": rl["five_hour"]["pct"], "source": rl.get("source", ""), "as_of": U.iso(rl.get("at")),
                          "seven_day_pct": (rl.get("seven_day") or {}).get("pct")}
        u["cost_usd"] = src.get("cost_usd")
    if problems:
        u["warnings"] = problems
    s["usage"] = u


CHAT_REFRESH_S = 20


def refresh_chat_usage(s):
    """A /rio run has no headless stream, so nothing calls `usage` every 30 s. Its session is
    registered once (edit.sh --prepare passes --session-id); after that every studio update
    (stage, busy, lead...) re-reads the chat transcript, at most every CHAT_REFRESH_S seconds.
    Headless runs (a stream is registered) keep their own 30 s loop and are left alone."""
    src = s.get("usage_src") or {}
    if not src.get("session_id") or src.get("stream") or src.get("result"):
        return
    last = parse_iso(((s.get("usage") or {}).get("computed_at")) or "")
    if last is not None and time.time() - last < CHAT_REFRESH_S:
        return
    probe_limit_if_stale()
    try:
        measure(s, {})
    except Exception as ex:  # usage is best effort; never fail a studio update over it
        warn("usage refresh failed: %s" % ex)


PROBE_EVERY_S = 600


def probe_limit_if_stale():
    """The 5-hour % is only exposed on the status line or in a `claude -p` stream. A chat run has
    neither when the status line isn't running (e.g. VS Code), so read it with one tiny background
    Haiku call (usage.py probe), at most every PROBE_EVERY_S, and only when no fresher reading exists.
    Detached: it never slows the studio call; the next refresh picks up the snapshot."""
    import subprocess
    import usage as U
    if os.environ.get("STUDIO_OFF") or os.environ.get("STUDIO_NO_PROBE"):
        return
    rl = U.rate_limit(U.RL_SHARED)
    if rl and time.time() - rl.get("at", 0) < PROBE_EVERY_S:
        return
    mark = U.RL_SNAPSHOT[:-len(".json")] + ".probe-started.json"
    try:
        if time.time() - os.path.getmtime(mark) < PROBE_EVERY_S:
            return
    except OSError:
        pass
    try:
        open(mark, "w").close()
        subprocess.Popen([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "usage.py"), "probe"],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError as ex:
        warn("5-hour probe not started: %s" % ex)


def finish_running(s, to):
    for st in s["stages"].values():
        if st["status"] == "running":
            st["status"] = to
            st["ended"] = now()
    for k, a in s["crew"].items():
        if a["status"] == "busy":
            a.update(status="idle", last=a["task"], task="", since=now())


def elapsed(s):
    a = parse_iso(s.get("started_at") or "")
    b = parse_iso(s.get("ended_at") or "") or time.time()
    return None if a is None else max(0, int(b - a))


def fmt_s(x):
    if x is None:
        return "-"
    x = int(x)
    return "%dh%02dm" % (x // 3600, x % 3600 // 60) if x >= 3600 else "%dm%02ds" % (x // 60, x % 60)


def show(s):
    st = s.get("stage")
    print("Video: %s   Stage: %s   Elapsed: %s" % (s.get("video") or "-", st or "-", fmt_s(elapsed(s))))
    print("Rio:   %s" % s["lead"]["doing"])
    print("Stages: " + "  ".join("%s:%s%s" % (k, v["status"], "(" + v["note"] + ")" if v["note"] else "")
                                for k, v in s["stages"].items()))
    for k, a in s["crew"].items():
        w = a["status"] != "busy" and crew_waiting(s, k)
        out = clocked_out(s, a, key=k)
        print("  %-12s %-5s %s" % (k, "OUT" if out else "WAIT" if w else a["status"],
                                  "clocked out" if out else "waiting for next call" if w else (a["task"] or a["last"])))
    u = s.get("usage")
    if u:
        lim = u.get("limit")
        print("Usage: %s%s%s" % (
            "5-hour %s%% · " % lim["pct"] if lim else "",
            "%s tokens" % (u.get("totals") or {}).get("total", "-") if not u.get("error") else "error: " + u["error"],
            " · " + ", ".join("%s %s%%" % (a["name"], a["pct"]) for a in u.get("agents") or []) if u.get("agents") else ""))
    e = s.get("estimate")
    if e and e.get("wall_s"):
        print("Forecast: wall %s-%s" % (fmt_s(e["wall_s"][0]), fmt_s(e["wall_s"][2])))


def run(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return
    cmd, args = argv[0], list(argv[1:])

    if cmd == "open":
        page = os.path.join(FLOOR, "index.html")
        if sys.platform == "darwin":
            import subprocess
            subprocess.Popen(["open", page], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            print(page)
        return

    os.makedirs(FLOOR, exist_ok=True)
    lockf = None
    try:
        try:
            import fcntl
            lockf = open(LOCK, "a")
            fcntl.flock(lockf, fcntl.LOCK_EX)
        except ImportError:
            pass
        try:
            s = load()
        except Exception as ex:  # corrupt board: start a fresh one rather than get stuck
            warn("state unreadable (%s), starting a fresh board" % ex)
            s = blank()
        if cmd == "show":
            show(s)
            return
        if cmd == "reset":
            est_path = flag(args, "--estimate")
            budget = s.get("budget")
            s = blank(args[0] if args else "")
            s["budget"] = budget
            log(s, "lead", "New run" + (": " + s["video"] if s["video"] else ""))
            if est_path:
                try:
                    s["estimate"] = read_estimate(est_path)
                    w = s["estimate"].get("wall_s")
                    if w:
                        log(s, "producer", "Forecast loaded: %s to %s" % (fmt_s(w[0]), fmt_s(w[2])))
                except Exception as ex:  # keep the reset, just without a forecast
                    warn("could not read estimate %s: %s" % (est_path, ex))
        elif cmd == "lead":
            s["lead"]["doing"] = " ".join(args) or s["lead"]["doing"]
            log(s, "lead", s["lead"]["doing"])
        elif cmd == "busy":
            if len(args) < 1:
                raise Warn('usage: studio.py busy <id> "task"')
            k = crew_key(s, args[0])
            task = " ".join(args[1:]) or "Working"
            if k == "lead":
                s["lead"]["doing"] = task
            else:
                s["crew"][k].update(status="busy", task=task, since=now())
            s["lead"]["done_at"] = s["lead"]["failed_at"] = None
            s["ended_at"] = None
            log(s, k, "started: " + task)
        elif cmd == "idle":
            if not args:
                raise Warn('usage: studio.py idle <id> ["result"]')
            k = crew_key(s, args[0])
            if k == "lead":
                return
            a = s["crew"][k]
            a.update(status="idle", last=" ".join(args[1:]) or a["task"] or a["last"], task="", since=now())
            log(s, k, "done: " + (a["last"] or "finished"))
        elif cmd == "stage":
            if len(args) < 2:
                raise Warn('usage: studio.py stage <stage> pending|running|done|failed|skipped ["note"]')
            k = stage_key(args[0])
            status = STATUS_ALIASES.get(args[1].lower(), args[1].lower())
            if status not in STATUSES:
                raise Warn("unknown stage status '%s' (use %s)" % (args[1], "|".join(sorted(STATUSES))))
            st = s["stages"][k]
            note = " ".join(args[2:])
            if status == "running" and st["status"] != "running":
                st["since"] = now()
                st["ended"] = None
            if status in ("done", "failed", "skipped"):
                st["ended"] = now()
            st["status"] = status
            if note or status == "pending":
                st["note"] = note
            if status == "running":
                s["stage"] = k
            label = dict(STAGES)[k]
            log(s, "lead", "%s: %s%s" % (label, status, " (" + note + ")" if note else ""))
            if status == "failed":
                s["lead"]["doing"] = "%s failed%s" % (label, ": " + note if note else "")
        elif cmd == "describe":
            s["description"] = " ".join(args).strip()
        elif cmd == "forecast":
            if not args:
                raise Warn("usage: studio.py forecast <estimate.json>")
            try:
                s["estimate"] = read_estimate(args[0])
            except Exception as ex:
                raise Warn("could not read estimate %s: %s" % (args[0], ex))
            w = s["estimate"].get("wall_s")
            log(s, "producer", "Forecast updated" + (": %s to %s" % (fmt_s(w[0]), fmt_s(w[2])) if w else ""))
        elif cmd == "budget":
            v = args[0].lower() if args else ""
            if v in ("off", "none", "0", "clear"):
                s["budget"] = None
            elif num(v) and num(v) > 0:
                s["budget"] = int(num(v))
            else:
                raise Warn("usage: studio.py budget <tokens>|off")
            if s.get("usage_src"):
                measure(s, {})
        elif cmd == "usage":
            opts = {k: flag(args, "--" + k) for k in ("session-id", "cwd", "stream", "result", "cost")}
            measure(s, opts)
        elif cmd == "log":
            log(s, "lead", " ".join(args))
        elif cmd == "done":
            act_path = flag(args, "--actual-json")
            overrides = {"wall_s": flag(args, "--wall-s"), "tokens": flag(args, "--tokens"), "peak_ram_mb": flag(args, "--ram-mb")}
            finish_running(s, "done")
            s["ended_at"] = now()
            actual = {"wall_s": elapsed(s)}
            if act_path:
                try:
                    actual.update(read_actual(act_path))
                except Exception as ex:
                    warn("could not read actuals %s: %s" % (act_path, ex))
            for k, v in overrides.items():
                if num(v) is not None:
                    actual[k] = num(v)
            s["actual"] = actual
            if s.get("usage_src"):
                try:
                    measure(s, {})
                except Exception as ex:
                    warn("usage refresh failed: %s" % ex)
            summary = " ".join(args)
            s["lead"].update(done_at=now(), failed_at=None, summary=summary,
                             doing="That's a wrap" + (": " + summary if summary else ""))
            s["stage"] = None
            log(s, "lead", s["lead"]["doing"])
        elif cmd == "fail":
            reason = " ".join(args) or "Run failed"
            finish_running(s, "failed")
            s["ended_at"] = now()
            s["actual"] = dict(s.get("actual") or {}, wall_s=elapsed(s))
            s["lead"].update(failed_at=now(), done_at=None, fail_reason=reason, doing="Run stopped: " + reason)
            log(s, "lead", "FAILED: " + reason)
        else:
            raise Warn("unknown command '%s' (try: studio.py help)" % cmd)
        if cmd not in ("usage", "done", "budget", "reset"):
            refresh_chat_usage(s)
        save(s)
    finally:
        if lockf:
            lockf.close()


def warn(msg):
    try:
        sys.stderr.write("studio: warning: %s\n" % msg)
    except Exception:
        pass


def main():
    if os.environ.get("STUDIO_OFF"):
        return 0
    try:
        run(sys.argv[1:])
    except Warn as w:
        warn(w)
    except BaseException as ex:  # never break the pipeline, whatever happens
        if isinstance(ex, KeyboardInterrupt):
            return 0
        warn("%s: %s" % (type(ex).__name__, ex))
    return 0


if __name__ == "__main__":
    sys.exit(main())
