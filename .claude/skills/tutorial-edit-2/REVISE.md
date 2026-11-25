# Revise: re-edit only the noted spots (orchestrator steps)

Read this instead of SKILL.md when the prompt says "Revise". The user watched the finished video and
wrote timestamped notes; only those spots are re-edited. `edit.sh` has already run `revise.py start`
(it wrote the windows) and does everything after step 3 itself: render (cached), checks, the
REVISION report and the copy to `output/`. **No Ines in a revise**: the user is the reviewer.

You run headless (or as Rio in /rio2 revise). **The user's notes are authoritative and pre-approved.
Never ask a question, never stop early.**

## Inputs (from the prompt)
`RUN` (run dir), `E` = `RUN/edit`, `N` (this revision's number), `V` = `E/revisions/vN`.
`V/windows.json` lists the windows; `V/window_NN.md` holds each window's notes, transcript (KEPT/CUT
words) and current ranges; Rosa writes `V/window_NN.edl.json`.

## Rules
- SKILL.md's **Shell rules** apply unchanged: project root, relative double-quoted paths, one command
  per Bash call, `py` after `step.py ... --`, the same allowlist. Studio calls as in SKILL.md (put
  `--lead` on step.py calls; never spend a turn on the Studio alone).
- **Token rules**: never Read the window files, the transcripts, the EDL or `takes_packed.md`
  yourself; Rosa reads the window files. You only list, count and pass paths.
- Every Agent call (retries included) starts its prompt (after `REVISE WINDOW MODE`) with `CODE=rio2`
  and `EDIT_TOOLS=.claude/skills/tutorial-edit-2/scripts/edit_tools.py`.
- Exactly one Agent call at a time. Every step is timed into `V/timings.jsonl` with
  `step.py --edit-dir "V"` (not `E`).

## 1. Windows and lessons
1. `jq -c '[.[] | {id, source, n_notes: (.notes | length)}]' "V/windows.json"` → the window ids
   (NN = zero-padded id) and their clip stems.
2. `.venv/bin/python .claude/skills/tutorial-edit-2/scripts/edit_tools.py lessons` (Approved only).
3. Read `E/sources.json` (clip stem → file name) and `mkdir -p "V/views"`.

## 2. Rosa, in batches of up to 5 windows
Split the windows, in id order, into batches of at most 5 (7 windows → 5 + 2). For each batch
B = 01, 02, … **one at a time**:
1. `.venv/bin/python rio2/helpers/step.py --edit-dir "V" --name edl_rev_B --studio story --task "windows <first>-<last> of <total>" --lead "Rosa is fixing your notes (batch B)" --begin`
2. Agent tool, `subagent_type: "edl-editor"`, prompt starting with **`REVISE WINDOW MODE`**, then **`CODE=rio2`** and
   **`EDIT_TOOLS=.claude/skills/tutorial-edit-2/scripts/edit_tools.py`**, giving:
   the batch's window files `V/window_NN.md` and, for each, its output path `V/window_NN.edl.json`;
   `brief.md`; `glossary.md`; the edit dir `E` (for `kept` and `loud`); for each clip a window uses:
   its stem, its video `RUN/<file>` and transcript `E/transcripts/STEM.json` (timeline_view only);
   `E/plan_brief.md` if it exists; the views dir `V/views`; the Approved lessons text. Tell her to
   write only those output files.
3. `jq -c length "V/window_NN.edl.json" ...` (all of the batch's files in one call). Then
   `... step.py --edit-dir "V" --name edl_rev_B --studio story --end --exit-code 0` (`1` if any file is
   missing or not a JSON array; carry on: step 3 retries it).

## 3. Apply (one retry at most)
1. `.venv/bin/python rio2/helpers/step.py --edit-dir "V" --name edl_rev_apply --studio story --lead "Splicing the fixes into the edit" -- py rio2/helpers/revise.py apply --edit-dir "E" --version N`
2. Exit 0 → done: finish with `APPLIED vN`.
3. Exit 1 → its stderr lists violations as `window NN: ...` and `general: ...`. ONE Rosa call for
   every failing window (a `general:` line, or a window whose file is missing, counts for that window;
   a `general:` line that names no window means all of them), timed as `--name edl_rev_retry`
   (`--begin`/`--end`), prompt `REVISE WINDOW MODE` as above plus the violation lines quoted: "fix
   exactly these and rewrite each listed file". Then apply again with `--name edl_rev_apply_retry`.
4. Still exit 1 → **stop**. Never edit the window files or `E/edl.json` yourself. Finish with
   `FAILED apply vN: <first violation line>`. edit.sh writes the user's "not applied" report and leaves
   `E/edl.json` as it was.

Finish with exactly one line: `APPLIED vN` or `FAILED apply vN: <reason>`.
