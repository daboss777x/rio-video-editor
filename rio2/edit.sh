#!/usr/bin/env bash
# edit.sh: take the clip(s) in input/ to ONE video in output/, measuring everything.
# This file is rio1/edit.sh (Rio) or rio2/edit.sh (Rio 2.0); run it via ./edit.sh or ./edit2.sh.
#
#   ./edit.sh             full run (headless Claude, tutorial-edit skill)
#   ./edit.sh --estimate  print the prediction only (nothing moved, no Claude)
#   ./edit.sh --dry-run   full plumbing except the Claude call (moves the source!)
#   ./edit.sh --prepare   /rio: preflight, move the clips into a run, forecast, start the
#                         monitor, print RIO_RUN=<run dir>; the chat session then edits as Rio
#   ./edit.sh --finish RUN [EXIT]  /rio: the end of a run (monitor stop, scrub, forecast vs
#                         actual, record_run, cache, copy to output/) for the run --prepare made
#   ./edit.sh --revise TARGET NOTES   re-edit only the noted spots of a finished run and render
#                         output/<name>_edited_vN.mp4 + <name>_REVISION_vN.md (headless Claude:
#                         Rosa only, no Ines). TARGET = run dir | run name | output name.
#   ./edit.sh --revise-prepare TARGET NOTES / --revise-finish RUN N   the same, split for
#                         /rio revise (the chat session runs Rosa in between)
#
# One run = one output video. input/ holds one or more clips (intro* first, then natural
# order main1 < main2 < main10, outro* last) plus at most one plan doc (.docx .doc .rtf
# .txt .md). See docs/CONTRACTS.md, "Multi-clip runs and the video plan".
# Rio 2.0 two-camera mode: a video whose name contains "cam" (e.g. tdevsetupcam.MOV) is the
# camera. It moves into the run with the clips, is never transcribed, and only the renderer
# uses it (4K composite). See docs/CONTRACTS.md, "Rio 2.0: two-camera 4K mode".
# No queueing, no automatic resume.
#
# Env overrides (for tests; defaults are the project folders):
#   EDITOR_INPUT_DIR, EDITOR_RUNS_DIR, EDITOR_OUTPUT_DIR
#   EDITOR_SKIP_PREFLIGHT=1  skip AC/disk/.env/dev-server checks (tests only)
#   EDITOR_NO_RECORD=1       don't call record_run.py (keeps history/ clean in tests)
#   MONITOR_INTERVAL_S       sampler period (default 5)
#   EDITOR_USAGE_INTERVAL_S  Studio token-usage refresh period while Claude runs (default 30)
#   EDITOR_CACHE_DIR         transcript cache (default <project>/cache/transcripts)
#   EDITOR_LESSONS_FILE      where a revision appends its Proposed lessons (default history/LESSONS.md;
#                            EDITOR_NO_RECORD=1 skips it)
#   EDITOR_REVISE_RESOLVE_ONLY=1  --revise: print which run/version it would revise, then stop
set -euo pipefail
# The key never lives in this shell (helpers read .env themselves), and CLAUDECODE would
# make step.py refuse the claude launch (it means "already inside a Claude session").
unset ASSEMBLYAI_API_KEY ELEVENLABS_API_KEY CLAUDECODE

# Two systems share one project: this script's folder is the system's CODE (rio1/ = Rio,
# rio2/ = Rio 2.0: helpers/, profiles/, config/, docs/); its parent is the PROJECT with the
# shared data (.venv, .env, vendor/, input/, output/, runs/, history/, .claude/). Commands
# run from PROJECT, so helper paths are "$SYS/helpers/..." (what the allowlists match).
CODE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT="$(dirname "$CODE")"
SYS="$(basename "$CODE")"
case "$SYS" in
  rio2) SKILL=tutorial-edit-2; WRAP=./edit2.sh ;;
  *)    SKILL=tutorial-edit;   WRAP=./edit.sh ;;
esac
cd "$PROJECT"
PY="$PROJECT/.venv/bin/python"
PROFILE="$CODE/profiles/tutorial.json"
HISTORY="$PROJECT/history/runs.jsonl"
INPUT_DIR="${EDITOR_INPUT_DIR:-$PROJECT/input}"
RUNS_DIR="${EDITOR_RUNS_DIR:-$PROJECT/runs}"
OUTPUT_DIR="${EDITOR_OUTPUT_DIR:-$PROJECT/output}"
# Reports go to Reports/ so output/ holds only finished videos (user, 2026-11-03).
# Tests that override EDITOR_OUTPUT_DIR keep reports next to the video unless EDITOR_REPORTS_DIR is set.
REPORTS_DIR="${EDITOR_REPORTS_DIR:-${EDITOR_OUTPUT_DIR:-$PROJECT/Reports}}"
CACHE_DIR="${EDITOR_CACHE_DIR:-$PROJECT/cache/transcripts}"
# intake.py (ordering, plan doc, sources.json, transcript cache) lives with the skill's scripts.
INTAKE=("$PY" "$PROJECT/.claude/skills/$SKILL/scripts/intake.py")

die() {
  echo "edit.sh: $*" >&2
  [ "${STUDIO_LIVE:-0}" = 1 ] && studio fail "$*"
  exit 1
}

# The Studio (live view, Sam's .claude/skills/studio). Best effort: silently skipped
# when the script is missing, and its output/exit code never reach this run.
# EDITOR_STUDIO_SCRIPT overrides the path (tests); STUDIO_OFF=1 mutes it.
STUDIO="${EDITOR_STUDIO_SCRIPT:-$PROJECT/.claude/skills/studio/scripts/studio.py}"
export EDITOR_STUDIO_SCRIPT="$STUDIO"   # step.py uses the same board
STUDIO_LIVE=0
# Absolute, symlink-free path: step.py compares the claude argv against exactly this.
RUN_SETTINGS="$(cd "$CODE" && pwd -P)/config/run-settings.json"
studio() {
  [ -f "$STUDIO" ] || return 0
  python3 "$STUDIO" "$@" </dev/null >/dev/null 2>&1 || true
}
# Premiere Pro XML (+ Rio's audio as a .wav) next to a finished video (user, 2026-11-25).
# Best effort: a missing edl.json or a failed export never fails the run. $1 = E, $2 = the .xml.
premiere_xml() {
  [ -f "$1/edl.json" ] && [ -f "$1/final.mp4" ] || { echo "premiere xml: skipped (no edl.json or final.mp4)"; return 0; }
  "$PY" "$CODE/helpers/media/premiere_xml.py" --edit-dir "$1" -o "$2" || echo "premiere xml: export failed (the video is fine)"
}
# Same, but returns the Studio's exit code (for "try the new command, else fall back").
studio_try() {
  [ -f "$STUDIO" ] || return 0
  python3 "$STUDIO" "$@" </dev/null >/dev/null 2>&1
}
# claude_result.json = the stream's final {"type":"result"} line (absent if none arrived).
# $1 = the folder holding claude_stream.jsonl (E for a run, E/revisions/vN for a revision).
extract_result() {
  [ -s "$1/claude_stream.jsonl" ] || return 0
  "$PY" - "$1/claude_stream.jsonl" "$1/claude_result.json" <<'PYEOF'
import json, sys
res = None
with open(sys.argv[1], errors="replace") as f:
    for line in f:
        try: o = json.loads(line)
        except ValueError: continue
        if isinstance(o, dict) and o.get("type") == "result": res = o
if res is None:
    print("claude: no result line in claude_stream.jsonl")
else:
    open(sys.argv[2], "w").write(json.dumps(res, indent=2) + "\n")
PYEOF
}

MODE=run
case "${1:-}" in
  "") ;;
  --estimate) MODE=estimate ;;
  --dry-run) MODE=dry ;;
  --prepare) MODE=prepare ;;
  --finish) MODE=finish ;;
  --revise) MODE=revise ;;
  --revise-prepare) MODE=revise_prepare ;;
  --revise-finish) MODE=revise_finish ;;
  -h|--help) sed -n '2,35p' "$0"; exit 0 ;;
  *) die "unknown option '$1' (use --estimate, --dry-run, --prepare, --finish or --revise)" ;;
esac
if [ "$MODE" = finish ]; then
  [ $# -ge 2 ] && [ $# -le 3 ] || die "usage: $WRAP --finish <run dir> [exit code]"
elif [ "$MODE" = revise ] || [ "$MODE" = revise_prepare ]; then
  [ $# -eq 3 ] || die "usage: $WRAP $1 <run dir | run name | output name> <notes file>"
elif [ "$MODE" = revise_finish ]; then
  [ $# -eq 3 ] || die "usage: $WRAP --revise-finish <run dir> <version N>"
else
  [ $# -le 1 ] || die "too many arguments"
fi

# ---------- --revise / --revise-prepare / --revise-finish (docs/CONTRACTS.md "Revise loop") ----------
# A revision re-edits only the noted spots of a finished run. Its measurements (timings, monitor,
# Claude stream, tokens) live in E/revisions/vN/, so the original run's numbers (and the
# estimator's history) stay as they were. No transcription, so no .env check and no key scrub.
case "$MODE" in revise|revise_prepare|revise_finish)
[ -x "$PY" ] || die ".venv missing (expected $PY)"
LESSONS="${EDITOR_LESSONS_FILE:-$PROJECT/history/LESSONS.md}"
FINALIZED=0
USAGE_PID=""
VN=""
BASE="?"
relpath() {  # the permission allowlist matches paths relative to the project root
  "$PY" -c 'import os,sys
r,p=os.path.realpath(sys.argv[1]),os.path.realpath(sys.argv[2])
print(os.path.relpath(r,p) if (r+os.sep).startswith(p+os.sep) else r)' "$1" "$PROJECT"
}
rev_json() { "$PY" -c 'import json,sys;print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$VN/rev.json" "$1"; }

if [ "$MODE" = revise_finish ]; then
  RUN_DIR="$(cd "$2" 2>/dev/null && pwd)" || die "no run dir $2"
  N="$3"
  case "$N" in ''|*[!0-9]*) die "version must be a number (the N of vN)" ;; esac
  E="$RUN_DIR/edit"
  VN="$E/revisions/v$N"
  [ -f "$VN/rev.json" ] || die "$VN/rev.json missing: --revise-finish only ends a revision started with --revise-prepare"
  STEM="$(rev_json stem)"; BASE="$(rev_json of)"; T_START="$(rev_json t_start)"
  NAME="$STEM revision v$N"
  STUDIO_LIVE=1
  exec > >(tee -a "$VN/revise.log") 2>&1
  echo "== edit.sh revise-finish  run=$(basename "$RUN_DIR")  v$N (interactive /rio revise)"
else
  NOTES="$3"
  [ -f "$NOTES" ] && [ -s "$NOTES" ] || die "notes file not found or empty: $NOTES"
  RES="$("$PY" "$PROJECT/.claude/skills/$SKILL/scripts/revise_target.py" "$2" --runs-dir "$RUNS_DIR")" \
    || exit 1   # revise_target.py already printed "edit.sh: <reason>"
  { IFS= read -r RUN_DIR; IFS= read -r STEM; IFS= read -r LATEST; } <<<"$RES"
  E="$RUN_DIR/edit"
  if [ "${EDITOR_REVISE_RESOLVE_ONLY:-}" = 1 ]; then
    printf 'run=%s\nstem=%s\nlatest=v%s\nnext=v%s\n' "$RUN_DIR" "$STEM" "$LATEST" "$((LATEST + 1))"
    exit 0
  fi
  # Preflight: the machine checks of a normal run, minus .env (a revision transcribes nothing).
  [ -d "$PROJECT/vendor/video-use/helpers" ] || die "vendor/video-use missing (clone it into vendor/video-use)"
  [ "$MODE" = revise_prepare ] || [ -f "$RUN_SETTINGS" ] \
    || die "config/run-settings.json missing; refusing to launch Claude without the run lockdown."
  if [ "${EDITOR_SKIP_PREFLIGHT:-}" = 1 ]; then
    echo "edit.sh: WARNING preflight checks skipped (EDITOR_SKIP_PREFLIGHT=1)" >&2
  else
    # No charger check for revisions (user, 2026-11-03): they only re-cut a few spots. The charger rule
    # stays for full runs (first drafts and big re-edits), in the main preflight below.
    NEED_GB="$("$PY" -c 'import json,sys;print(json.load(open(sys.argv[1]))["budget"]["min_free_disk_gb"])' "$PROFILE")"
    AVAIL_GB="$(df -k "$PROJECT" | awk 'NR==2 {printf "%d", $4/1048576}')"
    [ "$AVAIL_GB" -ge "$NEED_GB" ] || die "only ${AVAIL_GB} GB free on disk; need at least ${NEED_GB} GB."
    if pgrep -f 'next-server|next dev|npm run dev' >/dev/null 2>&1; then
      die "a dev server (next-server / npm run dev) is running; stop it first to free RAM."
    fi
    [ "$MODE" = revise_prepare ] || command -v claude >/dev/null 2>&1 || die "claude CLI not found on PATH"
  fi
  T_START="$(date +%s)"
  # The notes are copied into the run: step.py only lets helpers read paths inside the project.
  mkdir -p "$E/revisions"
  cp "$NOTES" "$E/revisions/incoming_notes.txt"
  studio reset "$STEM revision"
  studio open
  STUDIO_LIVE=1
  studio lead "Revision of $STEM: reading your notes"
  studio stage forecast skipped "revision"
  studio stage transcribe skipped "cached"
  studio stage screen skipped "unchanged"
  set +e
  START_OUT="$("$PY" "$SYS"/helpers/revise.py start --edit-dir "$E" --notes "$E/revisions/incoming_notes.txt" 2>&1)"
  START_RC=$?
  set -e
  [ "$START_RC" = 0 ] || die "revise.py start failed: $(printf '%s\n' "$START_OUT" | tail -n 1)"
  VLINE="$(printf '%s\n' "$START_OUT" | grep -E '^revision v[0-9]+ \(of v[0-9]+\)' | head -n 1 || true)"
  [ -n "$VLINE" ] || die "revise.py start printed no version line: $(printf '%s\n' "$START_OUT" | head -n 1)"
  N="$(printf '%s' "$VLINE" | sed -E 's/^revision v([0-9]+) \(of v([0-9]+)\).*/\1/')"
  BASE="$(printf '%s' "$VLINE" | sed -E 's/^revision v([0-9]+) \(of v([0-9]+)\).*/\2/')"
  VN="$E/revisions/v$N"
  # An earlier failed attempt at this version may have left replacements behind; Rosa rewrites them all.
  find "$VN" -maxdepth 1 -name 'window_*.edl.json' -type f -delete
  NAME="$STEM revision v$N"
  "$PY" -c 'import json,sys
p,stem,n,base,t0,rel=sys.argv[1:]
open(p,"w").write(json.dumps({"stem":stem,"n":int(n),"of":int(base),"t_start":int(t0),"run":rel})+"\n")' \
    "$VN/rev.json" "$STEM" "$N" "$BASE" "$T_START" "$(relpath "$RUN_DIR")"
  exec > >(tee -a "$VN/revise.log") 2>&1
  echo "== edit.sh $1  run=$(basename "$RUN_DIR")  v$N of v$BASE  started $(date '+%Y-%m-%d %H:%M:%S')"
  printf '%s\n' "$START_OUT"
  studio stage edit running "v$N: $(printf '%s\n' "$START_OUT" | grep -c '^  window_' || true) window(s)"
fi
REL_RUN="$(relpath "$RUN_DIR")"
REL_E="$REL_RUN/edit"
REL_V="$REL_E/revisions/v$N"
OUT_MP4="$OUTPUT_DIR/${STEM}_edited_v$N.mp4"
OUT_MD="$REPORTS_DIR/${STEM}_REVISION_v$N.md"

rev_finalize() {
  local rc=$?
  [ "$FINALIZED" = 1 ] && return
  FINALIZED=1
  set +e
  echo "== finalizing revision v$N (exit $rc)"
  if [ -n "$USAGE_PID" ]; then
    pkill -P "$USAGE_PID" 2>/dev/null; kill "$USAGE_PID" 2>/dev/null; wait "$USAGE_PID" 2>/dev/null
  fi
  "$PY" "$SYS"/helpers/monitor.py stop --edit-dir "$VN"
  "$PY" "$SYS"/helpers/monitor.py summary --edit-dir "$VN" >/dev/null
  extract_result "$VN"
  if [ -s "$VN/claude_result.json" ]; then
    "$PY" "$SYS"/helpers/tokens.py --result "$VN/claude_result.json" --edit-dir "$VN" --cwd "$PROJECT"
    studio usage --result "$VN/claude_result.json" --stream "$VN/claude_stream.jsonl"
  fi
  local line
  line="$("$PY" - "$VN" "$(( $(date +%s) - T_START ))" <<'PYEOF'
import json, sys
from pathlib import Path
V, wall = Path(sys.argv[1]), int(sys.argv[2])
def load(n):
    try: return json.loads((V / n).read_text())
    except Exception: return {}
tok, mon = load("tokens.json"), load("monitor_summary.json")
act = {"wall_s": wall, "tokens": tok.get("total"), "peak_extra_ram_mb": mon.get("peak_extra_mb")}
try: (V / "actual.json").write_text(json.dumps(act) + "\n")
except OSError: pass
t, r = act["tokens"], act["peak_extra_ram_mb"]
print(f"revision: wall {wall/60:.0f}m | tokens {'?' if t is None else f'{t/1000:.0f}k'} | "
      f"peak extra RAM {'?' if r is None else f'{r:.0f}MB'}")
PYEOF
)"
  echo "$line"
  # Not recorded in history/runs.jsonl: record_run.py models whole runs, not revisions.
  studio stage archive skipped "revision: not recorded in history"
  if [ "$rc" = 0 ] && [ -f "$OUT_MP4" ]; then
    studio done "$NAME: $line" --actual-json "$VN/actual.json"
  else
    studio fail "revision v$N ended with exit $rc (see revise.log)"
  fi
  echo "== done $(date '+%Y-%m-%d %H:%M:%S')  log: $VN/revise.log"
}

# The notes could not be applied: say so in the REVISION report and change nothing else.
rev_not_applied() {
  "$PY" - "$VN" "$N" "$BASE" <<'PYEOF'
import sys
from pathlib import Path
V, n, base = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
def read(name):
    try: return (V / name).read_text().splitlines()
    except OSError: return []
notes = [l for l in read("notes.txt") if l.strip() and not l.lstrip().startswith("#")
         and not l.strip().lower().startswith("version:")]
errs = [l.strip().removeprefix("- ") for l in read("apply_errors.txt") if l.strip()]
md = [f"# Revision v{n} (from v{base}): not applied", "",
      "The changes for these notes did not pass the safety checks, so nothing was changed: "
      f"the edit and the video are still v{base}.", "", "## Your notes"]
md += [f"- {l}" for l in notes] or ["- (none readable)"]
md += ["", "## Why it failed"]
md += [f"- {e}" for e in errs] or ["- the editor did not write the replacement ranges (see revise.log)"]
md += ["", "Reword or move the note(s) above and run the revision again."]
(V / "REVISION.md").write_text("\n".join(md) + "\n")
PYEOF
  mkdir -p "$REPORTS_DIR"
  cp "$VN/REVISION.md" "$OUT_MD" && echo "report: $OUT_MD"
}

# After Rosa: make sure the vN EDL exists (apply), render (cached), verify, report, copy.
rev_finish_steps() {
  if [ ! -f "$VN/edl.json" ]; then
    # The session ended without a successful apply (or stopped before it): apply once here.
    echo "apply: no v$N EDL yet; running revise.py apply"
    set +e
    "$PY" "$SYS"/helpers/step.py --edit-dir "$VN" --name edl_rev_apply_final -- \
      py "$SYS"/helpers/revise.py apply --edit-dir "$REL_E" --version "$N" 2>"$VN/apply_errors.txt"
    set -e
    if [ -s "$VN/apply_errors.txt" ]; then cat "$VN/apply_errors.txt" >&2; fi
  fi
  if [ ! -f "$VN/edl.json" ]; then
    studio stage edit failed "notes not applied"
    rev_not_applied
    die "revision v$N not applied; E/edl.json untouched (reasons in $OUT_MD)"
  fi
  studio stage edit done
  studio stage fix skipped "you review revisions"
  local rc=0
  # A run-only look (e.g. the user picked a camera brightness/centring for this video only)
  # lives in E/profile_override.json; revisions keep it so the look and the segment cache stay.
  local RPROF=()
  if [ -f "$REL_E/profile_override.json" ]; then
    RPROF=(--profile "$REL_E/profile_override.json")
    echo "render_rev: using this run's profile_override.json"
  fi
  "$PY" "$SYS"/helpers/step.py --edit-dir "$VN" --name render_rev --studio render --stage render \
    --lead "Re-rendering v$N (unchanged segments come from the cache)" -- \
    py "$SYS"/helpers/media/render_tutorial.py "$REL_E/edl.json" -o "$REL_E/final.mp4" ${RPROF[@]+"${RPROF[@]}"} || rc=$?
  if [ "$rc" != 0 ]; then
    echo "render_rev failed (exit $rc); retrying once"
    "$PY" "$SYS"/helpers/step.py --edit-dir "$VN" --name render_rev_retry --studio render --stage render -- \
      py "$SYS"/helpers/media/render_tutorial.py "$REL_E/edl.json" -o "$REL_E/final.mp4" ${RPROF[@]+"${RPROF[@]}"} \
      || die "render failed twice; E/edl.json is v$N but final.mp4 is not (retry: $WRAP --revise-finish \"$REL_RUN\" $N)"
  fi
  rc=0
  "$PY" "$SYS"/helpers/step.py --edit-dir "$VN" --name verify_rev --studio qc --stage qc --task "Checks on v$N" -- \
    py "$SYS"/helpers/verify_edit.py --edit-dir "$REL_E" ${RPROF[@]+"${RPROF[@]}"} || rc=$?
  [ "$rc" = 0 ] || echo "verify_rev exited $rc (the report says which checks failed)"
  "$PY" "$SYS"/helpers/step.py --edit-dir "$VN" --name verify_hear_rev --studio qc --task "Listening back to v$N" -- \
    py "$SYS"/helpers/hear_check.py --edit-dir "$REL_E" || echo "listen-back skipped"
  studio stage report running
  local lessons=(--lessons "$LESSONS")
  [ "${EDITOR_NO_RECORD:-}" = 1 ] && lessons=()
  "$PY" "$SYS"/helpers/revise.py report --edit-dir "$E" --version "$N" ${lessons[@]+"${lessons[@]}"} >/dev/null \
    || echo "revise.py report failed"
  studio stage report done
  mkdir -p "$OUTPUT_DIR" "$REPORTS_DIR"
  cp "$E/final.mp4" "$OUT_MP4" && echo "output: $OUT_MP4"
  premiere_xml "$E" "${OUT_MP4%.mp4}.xml"
  if [ -f "$VN/REVISION.md" ] && [ -f "$E/verify/heard.md" ]; then
    { echo; sed 's/^# /## /' "$E/verify/heard.md"; } >> "$VN/REVISION.md"   # the listen-back, for you to eyeball
  fi
  if [ -f "$VN/REVISION.md" ]; then cp "$VN/REVISION.md" "$OUT_MD" && echo "report: $OUT_MD"; fi
  return 0
}

trap rev_finalize EXIT
trap 'exit 130' INT TERM
[ "$MODE" = revise_finish ] || "$PY" "$SYS"/helpers/monitor.py start --edit-dir "$VN"

if [ "$MODE" = revise_prepare ]; then
  studio lead "Rio is revising in the chat session"
  # Studio spend panel: a chat run has no stream, so point the Studio at this chat's own transcript;
  # studio.py then refreshes it on every update (Claude Code sets CLAUDE_CODE_SESSION_ID for its shell).
  if [ -n "${CLAUDE_CODE_SESSION_ID:-}" ]; then
    studio usage --session-id "$CLAUDE_CODE_SESSION_ID" --cwd "$PROJECT"
  fi
  echo "REV_RUN=$REL_RUN"
  echo "REV_E=$REL_E"
  echo "REV_N=$N"
  echo "REV_V=$REL_V"
  echo "REV_NEXT: Rosa per .claude/skills/$SKILL/REVISE.md steps 1-3, then: $WRAP --revise-finish \"$REL_RUN\" $N"
  FINALIZED=1   # the revision is not over: --revise-finish finalizes it
  exit 0
fi

if [ "$MODE" = revise ]; then
  PROMPT="Revise a finished tutorial edit (instructions: .claude/skills/$SKILL/REVISE.md). \
RUN=\"$REL_RUN\", E=\"$REL_E\", N=$N, V=\"$REL_V\". The user's notes are authoritative and pre-approved: \
do not ask questions, do REVISE.md steps 1-3 to the end, and finish with its last line."
  # The same locked-down launch as a normal run; step.py accepts exactly this argv.
  CLAUDE_CMD=(claude -p --model sonnet --permission-mode dontAsk --setting-sources project
    --strict-mcp-config --settings "$RUN_SETTINGS" --output-format stream-json --verbose "$PROMPT")
  studio lead "Handing the notes to Rosa (headless Claude, Sonnet)"
  if [ -f "$STUDIO" ]; then
    ( while sleep "${EDITOR_USAGE_INTERVAL_S:-30}"; do
        studio usage --stream "$VN/claude_stream.jsonl"
      done ) </dev/null >/dev/null 2>&1 &
    USAGE_PID=$!
  fi
  set +e
  "$PY" "$SYS"/helpers/step.py --edit-dir "$VN" --name claude_rev -- \
    caffeinate -i "${CLAUDE_CMD[@]}" < /dev/null > "$VN/claude_stream.jsonl"
  echo "claude exited $?"
  set -e
fi
rev_finish_steps
exit 0
;;
esac

if [ "$MODE" = finish ]; then
# ---------- --finish: pick up the run --prepare made (the /rio session did the edit) ----------
RUN_DIR="$(cd "$2" 2>/dev/null && pwd)" || die "no run dir $2"
E="$RUN_DIR/edit"
[ -f "$E/rio.json" ] || die "$E/rio.json missing: --finish only ends a run started with --prepare"
RIO_EXIT="${3:-0}"
case "$RIO_EXIT" in ''|*[!0-9]*) die "exit code must be a number" ;; esac
RUN_ID="$(basename "$RUN_DIR")"
NAME="$("$PY" -c 'import json,sys;print(json.load(open(sys.argv[1]))["name"])' "$E/rio.json")"
STEM="$("$PY" -c 'import json,sys;print(json.load(open(sys.argv[1]))["stem"])' "$E/rio.json")"
T_START="$("$PY" -c 'import json,sys;print(json.load(open(sys.argv[1]))["t_start"])' "$E/rio.json")"
SCAN_TMP="$(mktemp -d)"
LOG="$E/run.log"
STUDIO_LIVE=1
exec > >(tee -a "$LOG") 2>&1
echo "== edit.sh finish  run=$RUN_ID  (interactive /rio run, exit $RIO_EXIT)"
else
# (the normal path: everything down to the estimate gate; not indented, to keep the diff small)

# ---------- find the clips + the optional plan doc ----------
# Refuses (one-line reason, nothing moved) on: no video, a PDF, 2+ plan docs, duplicate stems.
[ -x "$PY" ] || die ".venv missing (expected $PY)"
SCAN_TMP="$(mktemp -d)"
trap 'rm -rf "$SCAN_TMP"' EXIT
"${INTAKE[@]}" scan "$INPUT_DIR" --json "$SCAN_TMP/scan.json" --output-dir "$OUTPUT_DIR" >"$SCAN_TMP/scan.txt" \
  || exit 1   # intake.py already printed "edit.sh: <reason>" on stderr
ORDER_TEXT="$(cat "$SCAN_TMP/scan.txt")"
NAME="$("${INTAKE[@]}" get "$SCAN_TMP/scan.json" label)"       # Studio label, e.g. "intro + 3 clips"
STEM="$("${INTAKE[@]}" get "$SCAN_TMP/scan.json" out_stem)"    # output file name base
N_CLIPS="$("${INTAKE[@]}" get "$SCAN_TMP/scan.json" n)"
CLIP_LIST="$("${INTAKE[@]}" get "$SCAN_TMP/scan.json" order_line)"   # screen clips only
N_CAMS="$("${INTAKE[@]}" get "$SCAN_TMP/scan.json" n_cams)"          # camera files (0 = screen-only run)
CAM_LIST="$("${INTAKE[@]}" get "$SCAN_TMP/scan.json" cam_line)"
CAM_BYTES="$("${INTAKE[@]}" get "$SCAN_TMP/scan.json" cam_bytes)"
N_INSERTS="$("${INTAKE[@]}" get "$SCAN_TMP/scan.json" n_inserts)"    # insert clips (testimonials), placed full-screen
INSERT_LIST="$("${INTAKE[@]}" get "$SCAN_TMP/scan.json" insert_line)"

# The estimator models a composite (camera) render when the run has a camera: each camera file
# goes in as --cam (so the forecast shows the camera count); --composite is the fallback for an
# estimate.py without --cam. $1 = intake command (campaths|runcampaths), $2 = its folder.
add_cam_est_args() {
  [ "${N_CAMS:-0}" -gt 0 ] || return 0
  local est="$CODE/helpers/estimate.py" f
  if grep -q -- '"--cam"' "$est"; then
    while IFS= read -r -d '' f; do EST_ARGS+=(--cam "$f"); done < <("${INTAKE[@]}" "$1" "$2")
  elif grep -q -- '"--composite"' "$est"; then
    EST_ARGS+=(--composite)
  else
    echo "estimate: camera found, but estimate.py has no --cam/--composite; this forecast is screen-only"
  fi
}

# ---------- --estimate: probe + predict only ----------
if [ "$MODE" = estimate ]; then
  [ -f "$CODE/helpers/estimate.py" ] || die "helpers/estimate.py is missing"
  TMP="$SCAN_TMP"
  EST_ARGS=(--out "$TMP/estimate.json" --profile "$PROFILE")
  while IFS= read -r -d '' f; do EST_ARGS+=(--source "$f"); done < <("${INTAKE[@]}" paths "$INPUT_DIR")
  echo "$ORDER_TEXT"
  add_cam_est_args campaths "$INPUT_DIR"
  [ -f "$HISTORY" ] && EST_ARGS+=(--history "$HISTORY")
  "$PY" "$SYS"/helpers/estimate.py "${EST_ARGS[@]}" || die "estimate.py failed"
  exit 0
fi

# ---------- preflight ----------
preflight() {
  [ -x "$PY" ] || die ".venv missing (expected $PY)"
  [ -d "$PROJECT/vendor/video-use/helpers" ] || die "vendor/video-use missing (clone it into vendor/video-use)"
  # The headless run's lockdown (permissions, no MCP, no hooks) lives only in this file;
  # never launch Claude without it, even when the machine checks below are skipped.
  [ "$MODE" = dry ] || [ -f "$RUN_SETTINGS" ] || die "config/run-settings.json missing; refusing to launch Claude without the run lockdown."
  if [ "${EDITOR_SKIP_PREFLIGHT:-}" = 1 ]; then
    echo "edit.sh: WARNING preflight checks skipped (EDITOR_SKIP_PREFLIGHT=1)" >&2
    return 0
  fi
  pmset -g batt | grep -q "AC Power" || die "not on AC power. Plug in the charger (overnight runs must not die on battery)."
  local need avail_gb
  need="$("$PY" -c 'import json,sys;print(json.load(open(sys.argv[1]))["budget"]["min_free_disk_gb"])' "$PROFILE")"
  # A camera run also needs room for 1.5x the camera files (4K composite segments + final.mp4).
  need=$(( need + (${CAM_BYTES:-0} * 3 / 2 + 1073741823) / 1073741824 ))
  avail_gb="$(df -k "$PROJECT" | awk 'NR==2 {printf "%d", $4/1048576}')"
  [ "$avail_gb" -ge "$need" ] || die "only ${avail_gb} GB free on disk; need at least ${need} GB$([ "${N_CAMS:-0}" -gt 0 ] && echo ' (camera included)')."
  [ -f "$PROJECT/.env" ] || die ".env missing (needs ASSEMBLYAI_API_KEY=...)"
  # -q: the key is tested, never printed.
  grep -qE '^[[:space:]]*(export[[:space:]]+)?ASSEMBLYAI_API_KEY=[^[:space:]]+' "$PROJECT/.env" \
    || die ".env has no ASSEMBLYAI_API_KEY value"
  if pgrep -f 'next-server|next dev|npm run dev' >/dev/null 2>&1; then
    die "a dev server (next-server / npm run dev) is running; stop it first to free RAM."
  fi
  command -v claude >/dev/null 2>&1 || [ "$MODE" = dry ] || die "claude CLI not found on PATH"
  # No workspace-trust check: the run's allows come from config/run-settings.json via
  # --settings, which Claude Code applies even in an untrusted folder (proved by Leah).
}
studio reset "$NAME"
studio open
STUDIO_LIVE=1
studio lead "New video: $NAME"
studio stage forecast running
studio busy producer "Preflight checks"
preflight

# ---------- run dir + move source ----------
SLUG="$(printf '%s' "$STEM" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+/-/g; s/^-+//; s/-+$//' | cut -c1-40)"
[ -n "$SLUG" ] || SLUG=video
RUN_ID="$(date +%Y-%m-%d_%H%M)_$SLUG"
RUN_DIR="$RUNS_DIR/$RUN_ID"
n=2
while [ -e "$RUN_DIR" ]; do RUN_DIR="$RUNS_DIR/${RUN_ID}-$n"; n=$((n + 1)); done
RUN_ID="$(basename "$RUN_DIR")"
E="$RUN_DIR/edit"
mkdir -p "$E"
# Moves every clip + the plan doc into the run, converts the plan to E/plan.md, writes
# E/sources.json and copies cached transcripts to E/transcripts/ (a clip is never paid for twice).
"${INTAKE[@]}" prepare "$INPUT_DIR" "$RUN_DIR" --cache "$CACHE_DIR" \
  || { rm -rf "$RUN_DIR"; die "intake failed; nothing was moved"; }
LOG="$E/run.log"

# Everything from here on is logged. The redactor strips the AssemblyAI/ElevenLabs key values
# in case any tool ever echoes it (it reads .env itself; the key never enters this shell).
REDACT='import sys,re,os
keys=[]
try:
    for l in open(os.path.join(sys.argv[1],".env")):
        m=re.match(r"\s*(?:export\s+)?(?:ASSEMBLYAI|ELEVENLABS)_API_KEY=(.*)",l)
        if m:
            k=m.group(1).strip().strip("\"'"'"'")
            if len(k)>=8: keys.append(k)
except OSError: pass
for line in sys.stdin:
    for key in keys: line=line.replace(key,"[REDACTED]")
    sys.stdout.write(line); sys.stdout.flush()'
exec > >("$PY" -u -c "$REDACT" "$PROJECT" | tee -a "$LOG") 2>&1

echo "== edit.sh $MODE  run=$RUN_ID  source=$NAME  clips=$N_CLIPS  started $(date '+%Y-%m-%d %H:%M:%S')"

# ---------- estimate + gate ----------
[ -f "$CODE/helpers/estimate.py" ] || die "helpers/estimate.py is missing; clips left in $RUN_DIR"
studio busy producer "Forecasting time, tokens and RAM"
echo "$ORDER_TEXT"
EST_ARGS=(--out "$E/estimate.json" --profile "$PROFILE")
while IFS= read -r -d '' f; do EST_ARGS+=(--source "$f"); done < <("${INTAKE[@]}" runpaths "$RUN_DIR")
add_cam_est_args runcampaths "$RUN_DIR"
[ -f "$HISTORY" ] && EST_ARGS+=(--history "$HISTORY")
"$PY" "$SYS"/helpers/estimate.py "${EST_ARGS[@]}" || die "estimate.py failed; clips left in $RUN_DIR"
GATE="$("$PY" -c 'import json,sys
g=json.load(open(sys.argv[1])).get("gate",{})
print("ok" if g.get("ok") else "; ".join(g.get("reasons") or ["gate not ok"]))' "$E/estimate.json")"
if [ "$GATE" != ok ]; then
  echo "edit.sh: estimate gate refused this run: $GATE" >&2
  echo "edit.sh: the clips are in $RUN_DIR. To retry: move it back with" >&2
  echo "  find \"$RUN_DIR\" -maxdepth 1 -type f -exec mv {} \"$INPUT_DIR/\" \\; && rm -rf \"$RUN_DIR\" && $WRAP" >&2
  studio stage forecast failed "gate refused"
  studio fail "estimate gate refused: $GATE"
  exit 1
fi
# Hand the board the forecast; older studio.py builds lack `forecast`, so fall back
# to a second reset (the only other command that takes --estimate).
studio_try forecast "$E/estimate.json" || studio reset "$NAME" --estimate "$E/estimate.json"
studio stage forecast done
studio idle producer "Forecast ready, gate ok"
fi  # end of the normal path (--finish skips it)

# ---------- monitor + finalize trap ----------
T_START="${T_START:-$(date +%s)}"
FINALIZED=0
USAGE_PID=""
finalize() {
  local rc=$?
  [ "$FINALIZED" = 1 ] && return
  FINALIZED=1
  set +e
  echo "== finalizing (exit $rc)"
  rm -rf "$SCAN_TMP"
  if [ -n "$USAGE_PID" ]; then
    pkill -P "$USAGE_PID" 2>/dev/null; kill "$USAGE_PID" 2>/dev/null; wait "$USAGE_PID" 2>/dev/null
  fi
  "$PY" "$SYS"/helpers/monitor.py stop --edit-dir "$E"
  "$PY" "$SYS"/helpers/monitor.py summary --edit-dir "$E" >/dev/null
  extract_result "$E"
  # Scrub the AssemblyAI and ElevenLabs key values from every text file of the run and the Studio state.
  # The key is read from .env inside python and never enters this shell.
  # BEGIN SCRUB (tests extract this block)
  "$PY" - "$PROJECT" "$E" "$PROJECT/.claude/skills/studio/floor/state.json" \
      "$PROJECT/.claude/skills/studio/floor/state.js" <<'PYEOF'
import os, re, sys
proj, edit, *extra = sys.argv[1:]
keys = []
try:
    for l in open(os.path.join(proj, ".env"), errors="replace"):
        m = re.match(r"\s*(?:export\s+)?(?:ASSEMBLYAI|ELEVENLABS)_API_KEY=(.*)", l)
        if m and len(m.group(1).strip().strip("\"'")) >= 8:  # shorter = placeholder, unsafe to replace
            keys.append(m.group(1).strip().strip("\"'").encode())
except OSError:
    pass
if not keys:
    print("scrub: no API key in .env; nothing to scrub"); sys.exit(0)
hits = 0
paths = [os.path.join(d, n) for d, _, ns in os.walk(edit) for n in ns] + [p for p in extra if os.path.isfile(p)]
for p in paths:
    try:
        with open(p, "rb") as f:
            if b"\0" in f.read(8192): continue  # binary (video/audio): skip without loading it
            f.seek(0); data = f.read()
        if not any(kb in data for kb in keys): continue
        for kb in keys: data = data.replace(kb, b"[REDACTED]")
        with open(p, "r+b") as f:  # in place, so run.log's tee keeps appending to the same file
            f.write(data); f.truncate()
        hits += 1
    except OSError as ex:
        print(f"scrub: could not check {p}: {ex.strerror}")
print(f"scrub: key removed from {hits} file(s)")
PYEOF
  # END SCRUB
  if [ -s "$E/claude_result.json" ]; then
    "$PY" "$SYS"/helpers/tokens.py --result "$E/claude_result.json" --edit-dir "$E" --cwd "$PROJECT"
  else
    echo "tokens: no claude_result.json; tokens.json not written"
  fi
  # Forecast vs actual: printed here, filled into REPORT.md's placeholder, and
  # handed to the Studio as actual.json (before record_run so the report is final).
  FVA="$("$PY" - "$E" "$(( $(date +%s) - T_START ))" <<'PYEOF'
import json, sys
from pathlib import Path
E, wall = Path(sys.argv[1]), int(sys.argv[2])
def load(n):
    try: return json.loads((E / n).read_text())
    except Exception: return {}
est, tok, mon = load("estimate.json").get("totals", {}), load("tokens.json"), load("monitor_summary.json")
def mid(v): return v[1] if isinstance(v, list) and len(v) == 3 else v
def mins(s): return "?" if s in (None, "") else f"{s/60:.0f}m"
def k(n): return "?" if n in (None, "") else f"{n/1000:.0f}k"
def mb(n): return "?" if n in (None, "") else f"{n:.0f}MB"
pt = mid(est.get("tokens", {}).get("total"))
line = (f"predicted vs actual: wall {mins(mid(est.get('wall_s')))} vs {mins(wall)} | "
        f"tokens {k(pt)} vs {k(tok.get('total'))} | "
        f"peak extra RAM {mb(mid(est.get('peak_extra_ram_mb')))} vs {mb(mon.get('peak_extra_mb'))}")
try:
    (E / "actual.json").write_text(json.dumps({"wall_s": wall, "tokens": tok.get("total"),
                                               "peak_extra_ram_mb": mon.get("peak_extra_mb")}) + "\n")
except OSError:
    pass
rep = E / "REPORT.md"
if rep.exists():
    table = ("| Metric | Forecast (mid) | Actual |\n|---|---|---|\n"
             f"| Wall time | {mins(mid(est.get('wall_s')))} | {mins(wall)} |\n"
             f"| Tokens | {k(pt)} | {k(tok.get('total'))} |\n"
             f"| Peak extra RAM | {mb(mid(est.get('peak_extra_ram_mb')))} | {mb(mon.get('peak_extra_mb'))} |")
    t = rep.read_text()
    marker = "<!-- FORECAST_VS_ACTUAL: edit.sh fills this in after the run -->"
    t = t.replace(marker, table) if marker in t else t.rstrip() + "\n\n## Forecast vs actual\n" + table + "\n"
    rep.write_text(t)
print(line)
PYEOF
)"
  echo "$FVA"
  [ -f "$E/claude_stream.jsonl" ] && studio usage --result "$E/claude_result.json" --stream "$E/claude_stream.jsonl"
  if [ "${EDITOR_NO_RECORD:-}" = 1 ]; then
    echo "record_run: skipped (EDITOR_NO_RECORD=1)"
    studio stage archive skipped "EDITOR_NO_RECORD"
  elif [ -f "$CODE/helpers/record_run.py" ]; then
    studio stage archive running
    studio busy archivist "Recording the run in history/"
    if "$PY" "$SYS"/helpers/record_run.py --run-dir "$RUN_DIR"; then
      studio idle archivist "Logged to history/"
      studio stage archive done
    else
      echo "record_run.py failed (exit $?)"
      studio idle archivist "record_run failed"
      studio stage archive failed
    fi
  else
    echo "record_run: helpers/record_run.py missing"
  fi
  # New transcripts go into the cache (after the scrub above, so nothing secret is stored).
  "${INTAKE[@]}" store "$RUN_DIR" --cache "$CACHE_DIR" || echo "transcript cache: store failed"
  mkdir -p "$OUTPUT_DIR" "$REPORTS_DIR"
  [ -f "$E/final.mp4" ] && cp "$E/final.mp4" "$OUTPUT_DIR/${STEM}_edited.mp4" && echo "output: $OUTPUT_DIR/${STEM}_edited.mp4"
  premiere_xml "$E" "$OUTPUT_DIR/${STEM}_edited.xml"
  [ -f "$E/REPORT.md" ] && cp "$E/REPORT.md" "$REPORTS_DIR/${STEM}_REPORT.md"
  # Two-camera runs: the layout stills (layout_preview.py) go next to the report for approval.
  local png i=0
  for png in "$E"/layout/*.png; do
    [ -f "$png" ] || continue
    i=$((i + 1))
    cp "$png" "$REPORTS_DIR/${STEM}_layout_$i.png" && echo "layout still: $REPORTS_DIR/${STEM}_layout_$i.png"
  done
  [ -f "$CODE/docs/feedback-template.md" ] && cp "$CODE/docs/feedback-template.md" "$RUN_DIR/feedback.md"
  if [ -f "$REPORTS_DIR/${STEM}_REPORT.md" ]; then
    echo "report: $REPORTS_DIR/${STEM}_REPORT.md"
  else
    echo "report: none written (see $LOG)"
  fi
  if [ "$rc" = 0 ] && { [ "$MODE" = dry ] || [ -f "$E/final.mp4" ]; }; then
    studio done "$NAME: $FVA" --actual-json "$E/actual.json"
  else
    studio fail "run ended with exit $rc$([ -f "$E/final.mp4" ] || echo ', no final.mp4') (see run.log)"
  fi
  echo "== done $(date '+%Y-%m-%d %H:%M:%S')  log: $LOG"
}
trap finalize EXIT
trap 'exit 130' INT TERM

[ "$MODE" = finish ] && exit "$RIO_EXIT"   # finalize does the rest

"$PY" "$SYS"/helpers/monitor.py start --edit-dir "$E"

# ---------- the edit ----------
# The skill's permission allowlist matches relative paths from the project root,
# so hand Claude the run dir relative to $PROJECT when it is inside it.
REL_RUN="$("$PY" -c 'import os,sys
r,p=os.path.realpath(sys.argv[1]),os.path.realpath(sys.argv[2])
print(os.path.relpath(r,p) if (r+os.sep).startswith(p+os.sep) else r)' "$RUN_DIR" "$PROJECT")"
if [ "$MODE" = prepare ]; then
  # /rio: the chat session edits as Rio, then calls ./edit.sh --finish; the monitor keeps sampling.
  "$PY" - "$E/rio.json" "$NAME" "$STEM" "$T_START" "$REL_RUN" "$CLIP_LIST" <<'PYEOF'
import json, sys
p, name, stem, t0, rel, clips = sys.argv[1:]
open(p, "w").write(json.dumps({"name": name, "stem": stem, "t_start": int(t0), "run": rel, "clips": clips}) + "\n")
PYEOF
  studio lead "Rio is editing in the chat session"
  studio describe "Editing $NAME live in the chat"
  # Studio spend panel: a chat run has no stream, so point the Studio at this chat's own transcript;
  # studio.py then refreshes it on every update (Claude Code sets CLAUDE_CODE_SESSION_ID for its shell).
  if [ -n "${CLAUDE_CODE_SESSION_ID:-}" ]; then
    studio usage --session-id "$CLAUDE_CODE_SESSION_ID" --cwd "$PROJECT"
  fi
  echo "RIO_RUN=$REL_RUN"
  echo "RIO_E=$REL_RUN/edit"
  echo "RIO_CLIPS=$CLIP_LIST"
  [ "${N_CAMS:-0}" -gt 0 ] && echo "RIO_CAMS=$CAM_LIST"   # two-camera mode: the SKILL's camera steps apply
  [ "${N_INSERTS:-0}" -gt 0 ] && echo "RIO_INSERTS=$INSERT_LIST"   # insert clips: the SKILL's insert steps apply
  echo "RIO_NEXT: edit as Rio ($SKILL SKILL steps 0-8), then: $WRAP --finish \"$REL_RUN\" [exit code]"
  FINALIZED=1   # the run is not over: --finish finalizes it
  rm -rf "$SCAN_TMP"
  exit 0
fi
PROMPT="Run the $SKILL skill (instructions: .claude/skills/$SKILL/SKILL.md). \
RUN=\"$REL_RUN\", E=\"$REL_RUN/edit\", CLIPS=\"$CLIP_LIST\" (in this order; also in E/sources.json, plan in E/plan.md if present). \
The strategy is pre-approved: do not ask questions, work through every pipeline step to the end, and write REPORT.md in E."
if [ "${N_CAMS:-0}" -gt 0 ]; then
  PROMPT="$PROMPT Camera file(s): $CAM_LIST (in E/sources.json \"cams\"; two-camera mode: do the camera steps, never transcribe the camera)."
fi
if [ "${N_INSERTS:-0}" -gt 0 ]; then
  PROMPT="$PROMPT Insert clip(s): $INSERT_LIST (in E/sources.json \"inserts\"; do the insert steps: transcribe them, the editor places pieces full-screen)."
fi
# --permission-mode dontAsk: anything outside the allowlist is denied instead of prompting,
# so an unattended run can never hang. --setting-sources project + --settings: only the
# project's settings plus the strict run lockdown (config/run-settings.json) apply, never
# the user's; --strict-mcp-config: no MCP servers. step.py accepts exactly this argv.
CLAUDE_CMD=(claude -p --model sonnet --permission-mode dontAsk --setting-sources project
  --strict-mcp-config --settings "$RUN_SETTINGS" --output-format stream-json --verbose "$PROMPT")
studio lead "Handing over to the editor (headless Claude, Sonnet)"
if [ "$MODE" = dry ]; then
  echo "dry-run: skipping claude; would run: caffeinate -i ${CLAUDE_CMD[*]:0:${#CLAUDE_CMD[@]}-1} \"<prompt>\""
  "$PY" "$SYS"/helpers/step.py --edit-dir "$E" --name dry-run-noop -- true
  EXIT_CODE=0
else
  # Live token usage for the Studio while Claude works (best effort, killed in finalize).
  if [ -f "$STUDIO" ]; then
    ( while sleep "${EDITOR_USAGE_INTERVAL_S:-30}"; do
        studio usage --stream "$E/claude_stream.jsonl"
      done ) </dev/null >/dev/null 2>&1 &
    USAGE_PID=$!
  fi
  set +e
  # stdout = the stream-json events; stderr (progress/errors) goes to run.log via the redactor.
  "$PY" "$SYS"/helpers/step.py --edit-dir "$E" --name claude -- \
    caffeinate -i "${CLAUDE_CMD[@]}" < /dev/null > "$E/claude_stream.jsonl"
  EXIT_CODE=$?
  set -e
  echo "claude exited $EXIT_CODE"
fi
exit "$EXIT_CODE"
