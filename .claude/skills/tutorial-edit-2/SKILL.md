---
name: tutorial-edit-2
description: Rio 2.0's orchestrator (code in rio2/; everything Rio's tutorial-edit does, plus the 4K two-camera mode). Unattended cut-and-join edit of one run (runs/<id>/) of one or more screen-recorded tutorial clips joined into one video, with an optional plan doc as a guide. Probe, transcribe (cached), detect screen activity, chunked EDL by the edl-editor sub-agent, render, verify, edit-verifier sub-agent, at most one fix pass, REPORT.md. Launched headless by edit2.sh (rio2/edit.sh) with `claude -p --model sonnet`.
---

# tutorial-edit-2 (Rio 2.0's orchestrator)

> **Revising a finished run** (`./edit2.sh --revise`, `/rio2 revise`): follow `REVISE.md` in this folder instead; it re-edits only the noted spots.

You run on **Sonnet**, headless, overnight. Your two sub-agents (the edl-editor Rosa and the
edit-verifier Ines) run on **Opus**, set in their own agent files; the editing judgement is theirs. Nobody is watching and nobody will answer.
**The editing strategy is pre-approved** (`brief.md` plus the `## Approved` lessons). **Never ask a
question, never wait for approval, never stop early.** When something is ambiguous, take the most
obvious reading, keep rather than cut, note it for the report, and carry on to the end.

## Inputs (from the edit.sh prompt)

- `RUN` = the run dir, e.g. `runs/2026-10-28_2300_my-video` (relative to the project root)
- `CLIPS` = the clip file names in join order (intro first, outro last). They sit in `RUN/`; names
  may contain spaces: always quote. `E/sources.json` has the same list: `{"order": [stems], "files":
  {stem: file name}, "plan": "plan.md"|null, "cams": {cam stem: file name}}`. Read it once (Read tool); STEM below = a clip's stem.
- **Two-camera mode** = `cams` is non-empty (a file whose name contains "cam", e.g. `tdevsetupcam.MOV`,
  sits in `RUN/`). The camera is NOT a clip: never probe, transcribe, silence-scan, detect or chunk it.
  Only steps 2b, 4b, 5 (render) and 8 (report) use it; everything else is exactly as without a camera.
  `cams` is `{}` (or missing) on a screen-only run: skip every camera step.
- `E` = `RUN/edit`. `E/plan.md` exists only when the user dropped a plan doc.
- One run makes ONE output video from all clips. A single clip is just the N=1 case.

## Shell rules (the permission allowlist depends on them)

- Every command runs **from the project root**. Never `cd`. Use **relative** paths (`runs/...`,
  `rio2/helpers/...`), double-quoted. This skill drives **Rio 2.0** (system code in `rio2/`; the original Rio is
  `rio1/` with the `tutorial-edit` skill: never mix the two). Only these command shapes are pre-allowed; anything else is
  auto-denied:
  - `.venv/bin/python rio2/helpers/...` (including `rio2/helpers/step.py ... -- <cmd>`; after `--` write `py`, never `.venv/bin/python`, or the call is denied)
  - `.venv/bin/python vendor/video-use/helpers/...`
  - `.venv/bin/python .claude/skills/tutorial-edit-2/scripts/edit_tools.py ...`
  - `python3 .claude/skills/studio/scripts/studio.py ...`
  - `ffprobe ...`, `jq ...` (no `-R`/`--rawfile`), `mkdir -p ...`, `ls ...`
  - `ffmpeg` is denied as a direct command; it only runs inside the helpers, through `step.py`. Use the Read tool, not `cat`.
- One command per Bash call. No `&&` chains, no pipes into other programs, no `&` background jobs.
- **Long steps (transcribe, detect, render, render_fix): always pass the Bash tool's maximum
  `timeout` (600000 ms).** A foreground call without it is cut off at 2 minutes, moved to the
  background and killed at 10 minutes, which loses the work. If a step still outlasts 10 minutes,
  run it with the Bash tool's `run_in_background: true` instead and wait for its completion notice
  (never poll with sleep loops).
- Every pipeline step runs through the step timer:
  `.venv/bin/python rio2/helpers/step.py --edit-dir "E" --name <step> [--studio <id> --stage <stage> --task "<text>"] -- <cmd...>`
  Step names must start with `transcribe|detect|sync|edl|render|verify|verifier` (suffix allowed,
  e.g. `edl_02`, `render_fix`, `sync_cam`). step.py passes the exit code through.
- Sub-agents are timed with the same tool: `step.py ... --name <step> --begin` before the Agent call,
  `step.py ... --name <step> --end --exit-code 0|1` after it.
- Every Agent call (edl-editor and edit-verifier, retries included) starts its prompt with
  `CODE=rio2` and `EDIT_TOOLS=.claude/skills/tutorial-edit-2/scripts/edit_tools.py`: the agents
  serve both systems and run only the code they are told.

## The Studio (live view; best effort)

Before each phase, post one status line. **Never spend a turn on the Studio alone** (each extra turn
re-reads your whole context): put the status line on the phase's first step.py call as
`--lead "<what you are doing>"`, and put any other studio.py call (stage running/done/skipped) in the
SAME Bash call as your next real command, joined with ` ; ` (e.g.
`python3 .claude/skills/studio/scripts/studio.py stage edit done ; .venv/bin/python rio2/helpers/step.py ... --lead "Rendering" -- py ...`).
Crew ids: `producer transcriber watcher story render qc archivist lead`. Stages:
`forecast transcribe screen edit render qc fix report archive`. The `--studio/--stage` flags on
step.py mark crew busy/idle and stages running/done/failed for you. Below, `studio.py <args>` and
`lead "<text>"` are shorthand for `python3 .claude/skills/studio/scripts/studio.py <args>` (and
`... studio.py lead "<text>"`); always type the full command. If a studio command errors or
the script is missing, ignore it and keep going (stop calling it after the first "No such file").
Studio problems never fail the run.

## Token rules

- **Never Read** `E/transcripts/*.json` or `E/activity/*.json` (or the old `E/activity.json`). Use `edit_tools.py activity`,
  the chunk files and the command summaries instead.
- Read `brief.md`, `E/project.md` and chunk files normally. `E/plan.md` is read once, by you, in step 0;
  editors only ever get `E/plan_brief.md`, and reading that once per chunk is fine. Read `E/takes_packed.md` only with
  `offset`/`limit` around a specific point, never in full.
- `timeline_view.py`: at most **10 per chunk** (editor) and about 15 for the verifier. You, the
  orchestrator, should not need it at all.
- **No parallel sub-agents.** Exactly one Agent call at a time. No critic agent, no other agents
  (the only others are Bea, `blur-judge`, in step 4a; she needs no CODE line).
- Keep your own messages short. Do not echo file contents back.

## Steps

### 0. Read the strategy
1. `lead "Reading the brief"`. Read `brief.md`.
2. `.venv/bin/python .claude/skills/tutorial-edit-2/scripts/edit_tools.py lessons` (the Approved
   lessons only; never read the Proposed ones). Apply them on top of the brief.
3. Read `E/project.md` if it exists (earlier sessions on this run).
4. `mkdir -p "E/chunks"` and `mkdir -p "E/verify/views"` and `mkdir -p "E/activity"`.
5. **Plan guide (only if `E/plan.md` exists)**: Read it once and Write `E/plan_brief.md`, at most
   **600 words**: the planned sections in order, mapped to clips (intro/main1/... by name or position)
   where obvious, with each section's key steps and points. Plain bullets, no commentary. Without a
   plan doc skip this; there is no `plan_brief.md`.

### 1. Probe (once, all clips in order)
`.venv/bin/python rio2/helpers/step.py --edit-dir "E" --name transcribe_probe --studio producer --task "Probing the clips" -- py rio2/helpers/media/probe.py "RUN/<clip 1 file>" "RUN/<clip 2 file>" ... -o "E/probe.json"`
(every clip, in `CLIPS` order). Then `jq -c '{duration_s,width,height,avg_fps,vfr}' "E/probe.json"`
(first clip sets the output format; `clips` holds one probe per stem). If `vfr` is true, note it for
the report (render_tutorial.py already forces a constant frame rate at the source's avg fps).

### 2. Transcribe (cached) and pack
- **Once per clip**, in order:
  `.venv/bin/python rio2/helpers/step.py --edit-dir "E" --name transcribe_STEM --studio transcriber --stage transcribe --task "AssemblyAI transcript STEM" -- py rio2/helpers/stt/transcribe.py "RUN/<clip file>" --edit-dir "E"`
  Then, right after it, the silence scan for that clip (chain with ` ; ` where allowed):
  `.venv/bin/python rio2/helpers/step.py --edit-dir "E" --name detect_silence_STEM -- py rio2/helpers/media/silences.py "RUN/<clip file>" --out "E/silences/STEM.json"`
  (`E/silences/STEM.json` = `{"source", "noise_db", "min_s", "silences": [[start, end], ...]}` in source seconds;
  it also writes `E/silences/STEM.envelope.json`, the loudness the merge uses to keep every word
  ending whole. Never Read the envelope file. Always run this step, even on a transcript cache hit.)
  It reads the AssemblyAI key from the project-root `.env` itself (cwd = project root). Never read,
  print or mention `.env`. If `E/transcripts/STEM.json` already exists it is a cache hit (edit.sh
  pre-fills it from the transcript cache; no spend). The upload is a ~3 MB speech file per 20 min
  and retries network drops by itself; it saves its progress in `E/transcripts/STEM.assemblyai.json`,
  so a retry resumes the same paid job instead of paying again. Transcription of a 40-min clip can
  take 5-15 min on the user's connection: run it with `run_in_background: true` and wait for the
  notice. If one fails, retry once (same command: it resumes); if it fails again, go to step 9 and
  report the failure.
- **Insert clips** (`jq -c '.inserts // {}' "E/sources.json"`; e.g. a testimonial): run the same two
  commands (transcribe + silences) once per insert clip too, after the clips, with STEM = the insert's
  stem and `RUN/<insert file>`. They are never probed, never chunked and never camera-synced.
- Then **once for all clips**: `jq -r '.edit.phrase_silence_s' rio2/profiles/tutorial.json` → `PS`, and
  `.venv/bin/python rio2/helpers/step.py --edit-dir "E" --name transcribe_pack --studio transcriber --task "Packing phrases" -- py vendor/video-use/helpers/pack_transcripts.py --edit-dir "E" --silence-threshold PS`

### 2b. Sync the camera (two-camera runs only; skip when `cams` is empty)
Lines up the camera's audio with the screen clips' audio (deterministic, no network, about 1 min per
hour of audio). With the maximum timeout:
`.venv/bin/python rio2/helpers/step.py --edit-dir "E" --name sync_cam --studio watcher --stage screen --task "Syncing the camera" -- py rio2/helpers/media/sync_cam.py --edit-dir "E"`
Then `jq -c '{ok, cams: [.cams[] | {cam, residual_ms, drift}]}' "E/cam_sync.json"`.
- `ok` true → note the numbers for the report and carry on.
- The step fails, `E/cam_sync.json` is missing, or `ok` is false → **stop the edit here**: never ship
  out-of-sync lips, and never fall back to a screen-only render. `jq -c '.notes' "E/cam_sync.json"`
  (if the file exists) for the reason, then go to step 9: the Summary says "the camera couldn't be
  synced", which camera, its `residual_ms` against the 20 ms limit (or "no confident match"), the
  notes, and that steps 3-8 were skipped so no Opus tokens were spent on an edit that can't render.

### 3. Screen activity (once per clip)
For each clip in order:
`.venv/bin/python rio2/helpers/step.py --edit-dir "E" --name detect_STEM --studio watcher --stage screen --task "Classifying silent gaps STEM" -- py rio2/helpers/media/detect_screen_activity.py --video "RUN/<clip file>" --transcript "E/transcripts/STEM.json" --out "E/activity/STEM.json" --profile rio2/profiles/tutorial.json`
Then `.venv/bin/python .claude/skills/tutorial-edit-2/scripts/edit_tools.py activity --edit-dir "E"`
for the all-clips summary (never Read the activity files; `--stem STEM --from S --to T` for a window).

### 3b. Slide stills (two-camera runs only; skip when `cams` is empty)
`.venv/bin/python rio2/helpers/step.py --edit-dir "E" --name detect_slides --studio watcher --stage screen --task "Slide stills" -- py rio2/helpers/media/slides.py --edit-dir "E"`
(one pass per screen clip, a small JPEG at every visual change → `E/slides/`; split then lists them
as `SLIDE t still=...` lines in each chunk, plus `NO CAMERA a-b` lines for stretches no camera
covers). Long: `run_in_background: true`. If it fails, note it and carry on (no SLIDE lines: the
editor then keeps everything split except what brief/plan says is face-only).

### 4. EDL: split, edit each chunk one at a time, merge
1. `studio.py stage edit running`, then
   `.venv/bin/python rio2/helpers/step.py --edit-dir "E" --name edl_split -- py rio2/helpers/chunk_edl.py split --edit-dir "E"`
   → `E/chunks/chunk_NN.md` + `chunk_NN.json`, numbered in clip order (intro's chunks first, outro's
   last; a chunk never spans two clips; its `source` is the clip). `ls "E/chunks"` to get N.
2. For each chunk n = 1..N, **sequentially** (NN is zero-padded as in the file names):
   - `.venv/bin/python rio2/helpers/step.py --edit-dir "E" --name edl_NN --studio story --task "chunk n/N" --begin`
   - Agent tool, `subagent_type: "edl-editor"`, with a prompt that gives exactly:
     **`CODE=rio2`** and **`EDIT_TOOLS=.claude/skills/tutorial-edit-2/scripts/edit_tools.py`** (its first line), chunk file `E/chunks/chunk_NN.md`, chunk meta `E/chunks/chunk_NN.json`, brief `brief.md`,
     glossary `glossary.md`, the edit dir `E` (for its read-through command),
     the clip name (stem and file) from chunk_NN.json's `source`, that clip's video `RUN/<file>` and
     transcript `E/transcripts/STEM.json` (for timeline_view only), `E/plan_brief.md` if it exists
     with the guidance "a guide to intent and order; the speaker doesn't follow it word for word; edit
     what was actually said" (never the full plan), views dir `E/chunks/views`, the Approved lessons text, "chunk n of N", and
     **the output path `E/chunks/edl_NN.json`**. Tell it to write only that file.
     **Two-camera runs** add: "LAYOUTS: set `layout` per range (`split` default, `face` = camera
     only, full screen) by the edl-editor.md Layouts rules; look at the SLIDE stills of this chunk
     (paths relative to E); NO CAMERA lines are stretches without the camera". **Runs with inserts**
     add: "INSERTS: `E/inserts_packed.md` holds the insert clip(s) (stem, file, transcript
     `E/transcripts/<stem>.json` for timeline_view); place pieces where the plan cues them, by the
     edl-editor.md Inserts rules". Pass any per-run layout/insert instruction from the user verbatim.
   - `jq length "E/chunks/edl_NN.json"`. If it fails (missing or not a JSON array), re-invoke the
     editor once with the error. Then
     `.venv/bin/python rio2/helpers/step.py --edit-dir "E" --name edl_NN --studio story --end --exit-code 0`
     (`--exit-code 1` if the file is still unusable; keep going with the other chunks).
3. Merge: `.venv/bin/python rio2/helpers/step.py --edit-dir "E" --name edl_merge -- py rio2/helpers/chunk_edl.py merge --edit-dir "E"`
   - Exit 0 → `E/edl.json` written. `studio.py stage edit done`.
   - Non-zero → it lists violations (a candidate is in `E/edl.rejected.json`). Map each violation's
     times to its chunk (chunk_NN.json start/end). Send **each violating chunk back to edl-editor
     ONCE** (`--name edl_NN_retry` with --begin/--end) with the violation lines quoted, then merge again.
   - Still failing → fix it yourself: Edit the offending `E/chunks/edl_NN.json` to resolve each listed
     violation (order, overlap, out-of-range times; with the default silence policy `cut_all` there are
     no must_keep spans to cover), merge again, and note every such change for the report.
   - The merge also cuts every pause (policy `cut_all`, see brief.md): its `note:` lines on stderr say
     which ranges were split or dropped as silence. That is expected, not an error.

### 4a. Camera look: face tracking and blurs (two-camera runs only; skip when `cams` is empty)
`.venv/bin/python rio2/helpers/step.py --edit-dir "E" --name face_track --studio watcher --stage screen --task "Finding the face" -- py rio2/helpers/media/face_track.py --edit-dir "E"`
It finds the face about once a second in every kept range (macOS Vision, built in) and writes
`E/face_track.json`; the render centres the camera bubble and the face-only crop on it, per cut.
Run it after every merge that changes the EDL. If it fails, note it for the report and carry on
(the render falls back to the fixed crop).
**Blurs (always, 2026-11-17).** Every two-camera run blurs sensitive screen text automatically:
emails, phone numbers, API keys/secrets/long IDs (also inside URLs), `.env` `KEY=value` lines, the
mail pane of an open Gmail page, and the whole screen inside a private Drive folder (the profile's
`blur.private_folders`). If the plan, brief or user also names a word to hide, or the speaker says
so (`grep -n -i "blur" E/takes_packed.md`), add each word as it appears on screen with `--text`.
Run it once, after face_track (with the maximum timeout, or `run_in_background: true` on a long edit):
`.venv/bin/python rio2/helpers/step.py --edit-dir "E" --name blur_find --studio watcher --stage screen --task "Blurring sensitive text" -- py rio2/helpers/media/blur_find.py --edit-dir "E" --sensitive [--text "<word>" ...]`
It reads the screen in every kept split range once a second (about 1 s per kept second) and writes
`E/blurs.json`, which the render applies (an unknown `source` in it stops the render).
**Blur judge, Bea** (2026-11-21; whenever the `--sensitive` scan ran; one Agent call at a time):
  1. `.venv/bin/python rio2/helpers/media/blur_find.py --edit-dir "E" --candidates` (the unique texts the
     scan blurred, plus near misses, in `E/blur_candidates.md`).
  2. `step.py ... --name blur_judge --begin`, then Agent tool, `subagent_type: "blur-judge"`, prompt:
     `LABEL`, the paths `E/blur_candidates.md` and `E/blur_labels.json`, then
     `step.py ... --name blur_judge --end --exit-code 0`.
  3. `.venv/bin/python rio2/helpers/media/blur_find.py --edit-dir "E" --apply-labels` (drops the blurs
     she called `keep`, adds the near misses she called `blur`; unlabelled texts keep the rules' call).
  4. The blur check below. Then `.venv/bin/python rio2/helpers/media/blur_find.py --edit-dir "E" --sheet`
     and Agent `blur-judge` again with `EYE CHECK` and the paths `E/blur_sheet.md`, `E/blur_sheets/`,
     `E/blur_labels.json`, `E/blur_review.md`. If her reply says labels changed, run `--apply-labels`
     once more. Put `E/blur_review.md` in the report's blur section. If Bea fails, carry on with the
     rules' blurs and say so in the report.
Then the **blur check** (2026-11-20): `.venv/bin/python rio2/helpers/step.py --edit-dir "E" --name blur_check --studio watcher --task "Checking the blurs" -- py rio2/helpers/media/blur_find.py --edit-dir "E" --check --fix`
reads the frames every 0.5 s with the blurs applied and adds a tight blur for anything sensitive still
readable. Blurs cover only the value (first to last character), never the `.env` field name, and a
secret seen once is blurred wherever it shows again. Blur notes in a revise: `--sensitive --around <out
time>` (0.1 s, 30 s either side, appended), then `--check --fix --around <out time>`. Hand-written
entries must use the clip **stem** as `source`. In the report's Camera section, list the blurs
(`jq -c '[.blurs[] | {text, start, end}]' "E/blurs.json"`: kind and source seconds) so the user can
check them. Skip it only when the profile's `blur.sensitive` is false and nobody named a word.

### 4b. Layout preview (two-camera runs only; skip when `cams` is empty)
`.venv/bin/python rio2/helpers/step.py --edit-dir "E" --name layout_preview --studio render --task "Layout stills" -- py rio2/helpers/media/layout_preview.py --edit-dir "E" -o "E/layout/"`
A few PNG stills of the 4K composite (no video). Then `ls "E/layout"` for their names (do not view
them). If it fails, note it for the report and carry on: the render does not depend on it.

### 5. Render
The command is the same with or without a camera: render_tutorial.py reads `E/cam_sync.json` itself
and renders the 4K composite when it holds `ok: true`.
`.venv/bin/python rio2/helpers/step.py --edit-dir "E" --name render --studio render --stage render --task "Rendering final.mp4" -- py rio2/helpers/media/render_tutorial.py "E/edl.json" -o "E/final.mp4"`
No preview render. If it fails, retry once; then go to step 9 and report.

### 6. Verify (deterministic, then the verifier agent)
1. `studio.py stage qc running`, then
   `.venv/bin/python rio2/helpers/step.py --edit-dir "E" --name verify --studio qc --task "Checks" -- py rio2/helpers/verify_edit.py --edit-dir "E"`
   then `jq -c '{overall, checks: [.checks[] | {name, status}]}' "E/verify/checks.json"`.
   (`cut_ratio` and `cut_in_sound` FAIL are advisory only: `cut_in_sound` is a review list for Ines.)
   Then listen back (transcribes final.mp4, about $0.02 per 5 output minutes, and diffs it against the
   EDL; never fails the run), with the maximum timeout:
   `.venv/bin/python rio2/helpers/step.py --edit-dir "E" --name verify_hear --studio qc --task "Listening back" -- py rio2/helpers/hear_check.py --edit-dir "E"`
2. `.venv/bin/python rio2/helpers/step.py --edit-dir "E" --name verifier --studio qc --task "Reviewing the cut" --begin`
3. Agent tool, `subagent_type: "edit-verifier"`, giving: **`CODE=rio2`** and
   **`EDIT_TOOLS=.claude/skills/tutorial-edit-2/scripts/edit_tools.py`** (its first line), `E/final.mp4`, `E/edl.json`,
   `E/takes_packed.md`, `glossary.md`, `E/activity/` (per-clip files, summaries via edit_tools only), `E/sources.json`,
   `E/plan_brief.md` if it exists (to spot planned steps that are missing, judged by what was said),
   `E/inserts_packed.md` if it exists (insert clips: check each planned clip is placed, whole sentences,
   no clipped words) and, two-camera runs, the `layouts` check (face where nothing visual is shown),
   `E/verify/checks.json`, the listen-back `E/verify/heard.md` (its real ears), the clip videos in `RUN/`, the views dir
   `E/verify/views`, and the output path `E/verify/verdict.json`. Do not tell it what the editor
   intended; it must judge the cut cold.
4. `.venv/bin/python rio2/helpers/step.py --edit-dir "E" --name verifier --studio qc --stage qc --end --exit-code 0`
5. `jq -c '{verdict, n: (.problems|length)}' "E/verify/verdict.json"`.
6. Every flagged edge must have Ines's call: `.venv/bin/python rio2/helpers/verify_edit.py --edit-dir "E" --coverage`.
   If it exits 1, call `edit-verifier` **once** more (same opening lines as step 3) with
   `EDGE COVERAGE MODE` and the listed ids, then run the coverage command again. Ids still without a
   call go into REPORT.md under the verdict as "unreviewed edges" (output times from `edit_tools.py edges`).

### 7. At most ONE fix pass
Only if the verdict is FAIL with fixes, or checks.json `overall` is FAIL on a fixable EDL problem.
Otherwise `studio.py stage fix skipped` and go to step 8.
1. `studio.py stage fix running`. Read `E/edl.json` and `E/verify/verdict.json`. Write the untouched
   EDL to `E/edl.before_fix.json` first, with the Write tool (never `cp`: it's denied).
2. Apply each fix to `E/edl.json` in **descending `range_index`** (so indices stay valid):
   `extend`/`move` → set that range's `start`/`end`; `add` → insert `{source, start, end, beat: "FIX",
   quote: "", reason: "verifier #rank"}` at that index; `remove` → delete that range. Keep ranges in
   time order with no overlaps, and update `total_duration_s` to the new sum. Never remove a range
   that holds spoken content the viewer still needs. `layout` (Rio 2.0) → set `"layout"` of ranges
   `range_index..range_last` to the given value (`split` = remove the key), nothing else. Keep any new range tight to its words (no pauses).
3. `.venv/bin/python rio2/helpers/step.py --edit-dir "E" --name edl_fix_validate -- py rio2/helpers/chunk_edl.py validate --edit-dir "E" --refit`.
   `--refit` re-cuts any pause your fixes put back (so you never revert a fix for adding silence)
   and rewrites edl.json. If it fails, revert the offending fix (or all of them from
   edl.before_fix.json) and note it. Decide everything BEFORE rendering.
4. Re-render ONCE: as step 5 with `--name render_fix`. Re-verify: as step 6.1 with `--name verify_fix`,
   and listen back again with `--name verify_hear_fix`.
   Do **not** call the verifier agent again. **Never render a third time** (no render_fix2): whatever
   verify_fix still reports goes into the report.
5. `studio.py stage fix done`. Whatever still fails goes into the report. **Never loop.**

### 8. Report
`lead "Writing the report"`, `studio.py stage report running`. Run
`.venv/bin/python .claude/skills/tutorial-edit-2/scripts/edit_tools.py stats --edit-dir "E"` and
`.venv/bin/python .claude/skills/tutorial-edit-2/scripts/edit_tools.py joins --edit-dir "E"`
(the joins list only; do not view them). Write `E/REPORT.md`:

```markdown
# Edit report: <clips in order, plus plan doc if any>

## Summary
<2-4 lines: clips joined, what was cut, whether a plan guided it, whether it is a two-camera 4K composite, anything unusual (VFR, merge fallbacks, failed steps, fix pass result)>

## Numbers
<the stats table: source and final duration, kept ratio, ranges, cuts, transcription cost = source hours x the profile's scribe_usd_per_hour>

## Camera
<two-camera runs only; leave this section out when `cams` is empty>
<each cam: file, matched screen clip, offset_s, residual_ms, drift (from E/cam_sync.json)>
<canvas size: `ffprobe -v error -select_streams v:0 -show_entries stream=width,height -of csv=p=0 "E/final.mp4"`>
<composite gaps: if `ls "E"` shows composite_notes.json, `jq -c '.' "E/composite_notes.json"`: each output stretch shown without the camera; else "none">
<layouts: the `layouts` check detail from checks.json (minutes split / face / inserts, switches); inserts placed (output timecodes, which clip)>
<layout stills: the E/layout/*.png names (edit.sh copies them next to this report as <name>_layout_N.png for the user to approve the layout)>

## Checks
<the checks table from stats, after the fix pass if there was one>
<the listen-back: match %, and every "At joins" line from E/verify/heard.md that the fix pass did not resolve>

## Verifier verdict
<PASS/FAIL, each problem with its output timecode, and whether the fix pass fixed it>

## Eyeball these timecodes (output time)
<the verifier's problem timecodes, joins with risk >= 2, the listen-back's "At joins" timecodes, every merge-fallback or reverted fix>

## Forecast vs actual
<!-- FORECAST_VS_ACTUAL: edit.sh fills this in after the run -->
```
Leave the `## Forecast vs actual` section exactly as shown. Then append to `E/project.md` (create
it if missing): `## Session <date time>` with 3-6 bullets (outcome, final duration, verdict, open
issues). `studio.py stage report done`.

### 9. If a step fails for good
Do not ask and do not stop silently. Skip what depends on the failure, still write `E/REPORT.md`
(Summary says which step failed and its exit code, the last lines of its error, and what was
skipped), append to `E/project.md`, then `studio.py fail "<step>: <reason>"`. edit.sh handles the
rest (record_run, output copy, the archive stage).

Finish with one line: `DONE <final duration> <verdict>` or `FAILED <step>`.
