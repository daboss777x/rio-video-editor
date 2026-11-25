#!/usr/bin/env bash
# Plays a ~60 s fake pipeline run on The Studio so you can watch it.
#   .claude/skills/studio/scripts/demo.sh           full run, ends with "that's a wrap"
#   .claude/skills/studio/scripts/demo.sh --fail    stops at render with the alarm
#   .claude/skills/studio/scripts/demo.sh --no-open don't open the page
# Note: this overwrites the current board (floor/state.js). Click the page once to arm sound.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
S() { python3 "$HERE/studio.py" "$@"; }
# The Session panel reads a fake headless run (stream + transcripts) written to a temp dir, so the
# demo's 5-hour reading never reaches the real board's snapshot.
U="$(mktemp -d "${TMPDIR:-/tmp}/studio-demo.XXXXXX")"
export CLAUDE_CONFIG_DIR="$U/claude" STUDIO_USAGE_DIR="$U"
trap 'rm -rf "$U"' EXIT
P() { python3 "$HERE/demo_usage.py" "$U" "$1"; S usage --cwd /studio-demo --stream "$U/claude_stream.jsonl"; }
FAIL=0; OPEN=1
for a in "$@"; do case "$a" in --fail) FAIL=1 ;; --no-open) OPEN=0 ;; esac; done

S reset "Demo: Build an n8n webhook in 20 minutes" --estimate "$HERE/../demo/estimate.json"
S describe "Demo: cut a 1-hour n8n tutorial down to about 20 minutes"
S usage --cwd /studio-demo --stream "$U/claude_stream.jsonl"
[ "$OPEN" = 1 ] && S open
sleep 3
P 1

S lead "Reading brief.md and the Approved lessons"
S stage forecast running; S busy producer "Probing the source and forecasting time, tokens and RAM"; sleep 5
S idle producer "Forecast: 52m-1h35m, 480k-820k tokens, peak 1.1-1.7 GB"; S stage forecast done

S lead "Transcribing with Scribe"
S stage transcribe running; S busy transcriber "Scribe transcription (cached) + packing takes"; sleep 6
S idle transcriber "takes_packed.md: 9,412 words"; S stage transcribe done

S lead "Classifying silent stretches"
S stage screen running; S busy watcher "Scanning for static / typing / active screens"; sleep 5
S idle watcher "activity.json: 41 static, 17 typing, 63 active"; S stage screen done

S lead "Editing, one chunk at a time"
for n in 1 2 3 4; do
  S stage edit running "chunk $n/4"; S busy story "edl-editor: chunk $n/4"; sleep 4
done
S idle story "edl.json merged and validated"; S stage edit done "4/4"; P 2

S lead "Rendering final.mp4"
S stage render running; S busy render "Extracting segments, concat, two-pass loudnorm"; sleep 4
if [ "$FAIL" = 1 ]; then
  S fail "ffmpeg exited 1 during concat (demo)"
  exit 0
fi
sleep 4
S idle render "final.mp4: 18m 40s, -14.1 LUFS"; S stage render done

S lead "Checking the cut"
S stage qc running; S busy qc "verify_edit.py checks + edit-verifier agent"; sleep 6
S idle qc "2 WARN, 0 FAIL"; S stage qc done "2 warn"; P 3

S stage fix running; S busy story "Fix pass: tighten 2 joins"; sleep 4
S idle story "Fix pass applied"; S stage fix done

S stage report running; S lead "Writing REPORT.md"; sleep 3; S stage report done

S stage archive running; S busy archivist "record_run.py: predicted vs actual, lessons"; sleep 4
S idle archivist "Run logged: wall +7% vs mid"; S stage archive done
python3 "$HERE/demo_usage.py" "$U" 4
S usage --result "$U/claude_result.json" --stream "$U/claude_stream.jsonl"

S done "18m 40s cut from 1h 00m, 0 verifier FAILs" --actual-json "$HERE/../demo/actual.json"
S show
