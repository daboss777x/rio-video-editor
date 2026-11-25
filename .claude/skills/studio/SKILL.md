---
name: studio
description: The Studio, a live pixel-art view of the tutorial-edit video pipeline while it runs. A crew of editors (Rio the Lead Editor plus Ola, Lulu, Tariq, Rosa, Kit, Ines and Theo) works at a round table in a post-production studio, with a timeline wall showing each stage, forecast vs actual time/tokens/RAM, and a "that's a wrap" chime when the run finishes. Use when the user asks to open, watch, show or check the studio (or "the edit floor", "watch the edit run", "show me the pipeline live"), and whenever the tutorial-edit pipeline runs, which reports every step to it through scripts/studio.py.
---

# The Studio

A live view of one `tutorial-edit` run. The pipeline writes progress with `scripts/studio.py`; the page `floor/index.html` polls `floor/state.js` every 2 s (works straight from `file://`, no server).

- Open it: `python3 .claude/skills/studio/scripts/studio.py open` (macOS), or open `floor/index.html` in a browser.
- Watch a fake ~60 s run: `.claude/skills/studio/scripts/demo.sh` (add `--fail` to see the alarm). This overwrites the current board.
- Click the page once to arm sound (browser rule).

## Cast: who stands for which pipeline step

Names and looks live in `floor/cast.js` only (rename there; ids stay fixed). Looks: `references/cast.md`. Floor plan: `references/floor-plan.md`.

| id | Name, role | Pipeline step | Seat |
|---|---|---|---|
| `lead` | Rio, Lead Editor | the orchestrator (the `tutorial-edit` skill itself) | own glass office |
| `producer` | Ola, Producer | preflight, `probe.py`, `estimate.py` forecast | S1 |
| `transcriber` | Lulu, Transcriber | Scribe `transcribe.py` + `pack_transcripts.py` | S2 |
| `watcher` | Tariq, Screen Watcher | `detect_screen_activity.py` | S3 |
| `story` | Rosa, Story Editor | the `edl-editor` agent per chunk; also the fix pass | S4 |
| `render` | Kit, Render Engineer | `render_tutorial.py` → `final.mp4` | S5 |
| `qc` | Ines, QC Inspector | `verify_edit.py` + the `edit-verifier` agent | S6 |
| `archivist` | Theo, Archivist | `record_run.py`, predicted vs actual, lessons | S7 |

Busy crew sit at the round table; idle crew wander between the screening couch, espresso bar, sound booth, skylight, timeline wall and their phones. Rio walks over to check on whoever is working. The RENDER sign and rack lights go hot while `render` is busy or the render stage is running.

Clock-out: crew who are not busy and have been idle 10 minutes (`IDLE_CLOCK_OUT_S = 600`, in `floor/index.html` and `studio.py`) leave the floor and show as "Clocked out"; `busy` brings them back. Computed at render time from `crew.<id>.since` (or the run start if never busy); Rio never clocks out.

Waiting: relay crew with a stage still to come stay seated, leaned back with a "Waiting for next call" pill, and never clock out. Rule (`crewWaiting` in `floor/index.html`, mirrored as `crew_waiting` in `studio.py`): run not ended and a stage of theirs is pending/running (transcriber: transcribe, watcher: screen, story: edit, render: render, qc: qc, archivist: report/archive once render has started), plus story/render/qc while `fix` is still pending. Ola (forecast) and Rio are unaffected. Zero extra calls; computed from the board.

## CLI contract (`scripts/studio.py`, stdlib only)

```
studio.py reset "<video name>" [--estimate PATH]
studio.py open
studio.py lead "<doing>"
studio.py busy <id> "<task>"
studio.py idle <id> ["result"]
studio.py stage <stage> pending|running|done|failed|skipped ["note"]
studio.py log "<msg>"
studio.py done ["summary"] [--actual-json PATH] [--wall-s N] [--tokens N] [--ram-mb N]
studio.py fail "<reason>"
studio.py show
```

- **Never breaks the caller.** Every problem (bad id, unknown stage, missing file, unwritable folder, even a crash) prints `studio: warning: ...` to stderr and exits **0**. Success prints nothing (except `show`).
- **Cheap and safe to spam.** No heavy imports; a file lock serialises concurrent calls; writes are atomic (temp file + rename), so the page never reads half a file.
- `STUDIO_OFF=1` turns every command into a silent no-op (use it in tests/CI).
- Ids accept the crew name too (`busy kit ...` = `busy render ...`). Stages: `forecast transcribe screen edit render qc fix report archive` (aliases: `estimate`, `detect`, `screen-check`, `edl`, `verify`, `fix-pass`, `record`). Status aliases: `start`→running, `ok`→done, `error`→failed, `skip`→skipped.
- `stage ... running` also makes that stage the "current stage" in the header. A `n/N` in the note (e.g. `"chunk 2/4"`) fills the stage's progress bar on the timeline wall.
- `reset --estimate` reads `estimate.json` as defined in `docs/CONTRACTS.md` ("Estimate and history"): `totals.wall_s`, `totals.tokens.total`, `totals.peak_extra_ram_mb` (each `[lo, mid, hi]`), `totals.scribe_usd`, `source.duration_s`, `gate`. A missing or bad file just means no forecast panel (warning, exit 0).
- `done` marks any running stage done and any busy crew idle, stamps the actual wall time (reset → done), and reads actuals from `--actual-json` if given. Accepted keys, at the top level or under `actual` / `actuals` / `totals`: `wall_s`, `tokens` (number or `{"total": n}`), `peak_extra_ram_mb` / `peak_ram_mb` / `peak_rss_mb`. `--wall-s/--tokens/--ram-mb` override them.
- `fail` marks the running stage failed, sends the crew on a break, and lights the ALERT sign.
- Any `busy` after `done`/`fail` clears the wrap/alarm (a resumed run).

## When the pipeline should call it (for wiring into edit.sh, helpers/step.py and the tutorial-edit skill)

Call through `python3 .claude/skills/studio/scripts/studio.py` (system python is fine; no venv needed). In the order the pipeline runs:

| Moment | Calls |
|---|---|
| `edit.sh` start, after `estimate.py` has written `E/estimate.json` | `reset "<video file name>" --estimate E/estimate.json`, then `open` (optional; skip when headless/overnight if you prefer) |
| Skill step 1: reading brief / lessons / project.md | `lead "Reading the brief"` |
| Step 2: probe + forecast (if the skill runs it) | `stage forecast running` · `busy producer "Probing and forecasting"` … `idle producer "<one-line forecast>"` · `stage forecast done` |
| Step 3: transcribe + pack | `stage transcribe running` · `busy transcriber "Scribe + packing"` … `idle transcriber "<n> words"` · `stage transcribe done` |
| Step 4: screen activity | `stage screen running` · `busy watcher "Detecting screen activity"` … `idle watcher "<counts>"` · `stage screen done` |
| Step 5: each edl-editor chunk k of N | `stage edit running "chunk k/N"` · `busy story "edl-editor chunk k/N"`; after merge: `idle story "edl.json merged"` · `stage edit done "N/N"` |
| Step 6: render | `stage render running` · `busy render "Rendering final.mp4"` … `idle render "<duration, LUFS>"` · `stage render done` |
| Step 7: checks + verifier | `stage qc running` · `busy qc "verify_edit.py + edit-verifier"` … `idle qc "<n FAIL, n WARN>"` · `stage qc done` |
| Step 8: fix pass (max one) | `stage fix running` · `busy story "Fix pass"` … `idle story` · `stage fix done`; no fix needed: `stage fix skipped "no issues"` |
| Step 9: REPORT.md + project.md | `stage report running` · `lead "Writing REPORT.md"` · `stage report done` |
| `edit.sh` after the skill: `record_run.py` | `stage archive running` · `busy archivist "Recording the run"` … `idle archivist` · `stage archive done` |
| End of `edit.sh`, success | `done "<one-line summary>" --actual-json <file with actual totals>` (or `--wall-s/--tokens/--ram-mb`) |
| Any fatal error / non-zero exit / trap | `fail "<step>: <reason>"` |

`helpers/step.py` is the natural single place for the busy/idle + stage pairs: wrap each heavy step as `stage X running` + `busy <id>` before, and `idle <id>` + `stage X done|failed` after. Use `lead "<doing>"` and `log "<msg>"` freely; they are cheap.

## Files

- `scripts/studio.py`: the CLI. `scripts/demo.sh`: fake run. `scripts/check_floor.js`: `node` check that every seat and spot is free and reachable.
- `floor/index.html`: the page. `floor/cast.js`: cast (names, looks, seats). `floor/map.js`: grid, furniture footprints, seats, spots, pathfinding (shared by the page and the check).
- `floor/state.json` / `floor/state.js`: written by the CLI; don't edit by hand.
- `demo/estimate.json`, `demo/actual.json`: sample inputs for the demo.
