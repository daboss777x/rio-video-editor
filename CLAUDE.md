# CLAUDE.md

Guidance for Claude Code in this repository.

## Machine setup

**Status: NOT DONE.** Before anything else (before editing a video, before changing code), read
[SETUP.md](SETUP.md) and follow it from step 1. It audits this computer, explains what has to change
for it, and adapts Rio after the person agrees. When it's done, SETUP.md step 7 replaces this
paragraph with a short record of the machine and what was adapted.

## What this is

**Rio** is an AI video editor for YouTube tutorials. You drop a raw screen recording (about an hour,
one speaker; several clips and an optional script are fine) into `input/`, and Rio returns a tight
**cut-and-join** edit of about 20-25 minutes: every pause, filler, false start and retake is cut on the
audio (never mid-word), sensitive text on screen is blurred, the voice is cleaned up, and the result is
a 4K HEVC MP4 plus a report. It runs overnight headless (`./edit.sh`) or live in a Claude Code chat
(`/rio`), and it has a revise loop: timestamped notes on a finished video re-edit only those spots.

Two systems share this folder:
- **Rio 1** (`rio1/`, `./edit.sh`, `/rio`, skill `tutorial-edit`): one screen recording (for example
  from OBS, with the camera already layered in). **The main system.**
- **Rio 2.0** (`rio2/`, `./edit2.sh`, `/rio2`, skill `tutorial-edit-2`): everything Rio 1 does, plus a
  4K two-camera mode (a phone video with `cam` in its name is synced by sound and laid out next to the
  screen), face/split layouts, face tracking and insert clips (`testimonial`/`insert` in the name).

Shared at the root: `.venv/`, `.env`, `vendor/`, `input/`, `output/`, `Reports/`, `runs/`, `cache/`,
`history/`, `brief.md`, `glossary.md`, `.claude/`. Code finds both roots through
`<system>/helpers/paths.py`. Commands run from the project root, so helper paths carry the system
prefix (`rio1/helpers/...`). Below, `helpers/...` means "inside each system".

## The crew (agents)

| Name | What | Where | Model |
|---|---|---|---|
| Rio | the orchestrator: runs the pipeline, hands out work one job at a time | `.claude/skills/rio`, `rio2`, `tutorial-edit`, `tutorial-edit-2` | Sonnet headless; the chat model in `/rio` |
| Rosa | the editor: picks the words to keep in each ~15-minute chunk, writes the EDL | `.claude/agents/edl-editor.md` | Opus |
| Ines | the verifier: judges the finished cut cold, listens back, writes fixes | `.claude/agents/edit-verifier.md` | Opus |
| Bea | the blur judge: labels flagged text blur/keep, eye-checks blurred stills | `.claude/agents/blur-judge.md` | Sonnet |
| The Studio | a live pixel-art view of the run, with forecast vs actual | `.claude/skills/studio/` | no model |

Agents never read the large JSON files directly. They use
`.claude/skills/tutorial-edit[-2]/scripts/edit_tools.py lessons|activity|kept|joins|out2src|edges|stats`.
The orchestrator passes `CODE=rio1|rio2` and `EDIT_TOOLS=<path>` as the first lines of every agent prompt.
**Never register `vendor/video-use/SKILL.md` as a skill**: its defaults (grading, subtitles,
animations, approval waits) conflict with this project. Only its `helpers/` code is used.

## Pipeline (the `tutorial-edit` skill, in order)

1. Read `brief.md`, the Approved section of `history/LESSONS.md`, `E/project.md`; turn the plan doc
   (if any) into `E/plan_brief.md`.
2. `helpers/media/probe.py` → `probe.json`.
3. Per clip: `helpers/stt/transcribe.py` (AssemblyAI, fillers kept, cached in `cache/transcripts/`),
   then `helpers/media/silences.py` (silences + a 10 ms loudness envelope on the voice band). Then
   `vendor/video-use/helpers/pack_transcripts.py` → `takes_packed.md`.
4. `helpers/media/detect_screen_activity.py` → `activity/` (no tokens).
5. `helpers/chunk_edl.py split` → Rosa once per chunk, **one at a time** → `chunk_edl.py merge`
   (cuts every pause on the audio envelope, keeps word endings whole) → `edl.json`.
6. Blur (when the person said there may be sensitive info): `helpers/media/blur_find.py --sensitive`
   (screen reader every 1 s: emails, phones, keys/IDs, `.env` values, Gmail, private folders), Bea
   labels, `--check --fix` re-reads the blurred frames, Bea eye-checks contact sheets → `blurs.json`.
7. `helpers/media/render_tutorial.py` → `final.mp4` (4K HEVC, voice filter, loudness steered to
   −14 LUFS / ≤ −1 dBTP, segments cached in `E/segments/`).
8. `helpers/verify_edit.py` → `verify/checks.json`; `helpers/hear_check.py` transcribes the result and
   diffs it against the EDL → `verify/heard.md`; Ines → `verify/verdict.json`; `--coverage` makes sure
   she ruled on every flagged edge.
9. At most **one** fix pass, then `REPORT.md`. `edit.sh --finish` records the run in `history/`,
   recalibrates the forecast and copies the video to `output/` and the report to `Reports/`. It also
   writes a Premiere Pro XML next to the video (`helpers/media/premiere_xml.py`: the cuts on the raw
   clips, Rio's audio as `_audio.wav`, markers at blurs and Ines's problems; best effort, see
   CONTRACTS "Premiere Pro XML"). Revisions get `_edited_vN.xml` the same way.

**Revise loop**: `./edit.sh --revise <run|output name> <notes.txt>` or `/rio revise`. Each note
(`M:SS - text`, spans `A - B` allowed) becomes a ±60 s window; Rosa fixes up to 5 windows per call;
`revise.py apply` splices them in; the render reuses cached segments → `output/<name>_edited_vN.mp4`
and `_REVISION_vN.md`. No Ines in a revise: the person is the reviewer. After v1 exists, every change
goes through the revise loop, never a full re-merge.

Every heavy step runs through `helpers/step.py` (wall time + peak memory → `timings.jsonl`);
`helpers/monitor.py` samples system memory for the whole run.

## Editing rules (these override video-use)

1. **No silences.** Every pause is cut, whatever the screen is doing, unless the speaker announces a
   silent demo ("watch this"); that range gets `keep_silence: true`. Join gaps: intro 60 + 40 ms,
   body 100 + 40 ms, measured on the audio. `edit.silence_policy: keep_active` in the profile restores
   the old keep-silent-activity behaviour.
2. Never drop a spoken step. If in doubt, keep it.
3. Retakes and rephrasings: keep the **last** complete version unless it's clearly worse.
4. Cut fillers, false starts, stray sounds and tangents the speaker abandons. Small natural stutters stay.
5. No clipped words, ever: cut edges are placed on the audio envelope, not on transcript times.
6. Never stop to ask an editing question; the strategy is pre-approved (`brief.md` + Approved lessons).
7. No grade, subtitles, overlays, animations, music or zooms.
8. A section the speaker recorded late can be moved with `move_after: <source s>` on its ranges.
9. Blurs cover only the value (first to last character), never the field name (`SUPABASE_URL=` stays
   readable). Hand-written blurs use the clip **stem** as `source`.

## Hard constraints (tuned for the reference machine; SETUP.md adjusts them)

- Built for an Apple M3 with **8 GB RAM**: extra memory under ~2 GB. **Pipeline steps never run in
  parallel and agents run one at a time.**
- A run must fit in one Claude Pro 5-hour window. A full run used about 2% of it.
- Transcription is AssemblyAI (hosted, `disfluencies` on). Never local. The key is
  `ASSEMBLYAI_API_KEY` in `.env` (chmod 600). **Never read, echo or log `.env`.**
- Ask before installing anything (`brew`, `apt`, `winget`). Never use `--dangerously-skip-permissions`.

## Commands

```bash
STUDIO_OFF=1 .venv/bin/python -m pytest rio1/tests -q   # Rio 1 tests (synthetic media, no spend)
STUDIO_OFF=1 .venv/bin/python -m pytest rio2/tests -q   # Rio 2.0 tests
./edit.sh --estimate [--sensitive yes|no|unsure]        # forecast only
./edit.sh                                               # Rio 1, headless (wrapper for rio1/edit.sh)
./edit2.sh                                              # Rio 2.0, headless
./edit.sh --revise <run|output name> <notes.txt>        # re-edit only the noted spots
/rio   /rio revise   /rio2   /rio2 revise               # live in a Claude Code chat here
python3 .claude/skills/studio/scripts/studio.py open    # the Studio
```

Headless runs use `claude -p --model sonnet --permission-mode dontAsk` with
`<system>/config/run-settings.json` (its sha256 is pinned in `<system>/helpers/step.py` as
`RUN_SETTINGS_SHA256`; update it after any edit). Anything not allowed there or in
`.claude/settings.json` is denied. **The folder must be trusted** (run `claude` here once and accept),
or Claude Code ignores every allow rule. The only network calls are AssemblyAI (`helpers/stt/transcribe.py`,
also used by `helpers/hear_check.py`).

## Key files

| What | Where |
|---|---|
| Editing strategy, glossary of names | `brief.md`, `glossary.md` |
| Thresholds, voice filter, blur settings, output format, forecast priors | `rio*/profiles/tutorial.json` |
| Data shapes between steps | `rio*/docs/CONTRACTS.md` |
| Run history and lessons (Proposed → Approved by the person) | `history/runs.jsonl`, `history/RUN_LOG.md`, `history/LESSONS.md` |
| Agent briefs | `.claude/agents/*.md` |
| Orchestrator steps | `.claude/skills/tutorial-edit/SKILL.md`, `REVISE.md` (and `-2`) |
| The original spec, the architecture map | `docs/original-spec.md`, `docs/architecture.html` |

## Conventions

- **Test first.** Every behaviour change starts with a failing test (red), then the code (green), then
  a tidy-up. Bug fixes start with a test that reproduces the bug. Purely visual changes are checked by eye.
- A general fix goes into **both** `rio1/` and `rio2/`. Camera layering and inserts are `rio2/` only.
- Lessons the person gives go under `## Proposed` in `history/LESSONS.md`; only they move them to Approved.
- Keep replies to the person short and plain: they're a creator, not necessarily a developer.

## Credits

- Built on the helpers of [video-use](https://github.com/browser-use/video-use) by browser-use
  (transcript packing, timeline views, render concat).
- Transcription by [AssemblyAI](https://www.assemblyai.com). Media work by [FFmpeg](https://ffmpeg.org).
  Text and face reading on macOS by Apple's Vision framework.
- Orchestration and agents run on [Claude Code](https://docs.claude.com/en/docs/claude-code) by Anthropic.
