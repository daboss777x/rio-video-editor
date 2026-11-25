> **History:** this is the original spec Rio was built from (2026-10-28). Several choices changed
> since (AssemblyAI instead of ElevenLabs, Opus for the editor and verifier, every pause cut, 4K HEVC
> output). `CLAUDE.md` describes the system as it is now.

# Project Plan: Overnight Tutorial Auto-Editor

Hand this file to Claude Code. Work through the phases in order. Stop at every **CHECKPOINT** and report to me before continuing.

---

## 1. Goal

Turn a raw tutorial screen recording of about 1 hour into a tight edit of about 15–25 minutes, using **cut-and-join only**, unattended overnight, on my Mac, within my Claude Pro usage limits.

## 2. Hard constraints

| Constraint | Detail |
|---|---|
| Machine | MacBook, Apple M3, **8 GB RAM**. Keep peak extra RAM under ~2 GB. Nothing runs in parallel. |
| Plan | Claude **Pro**. One video must fit inside **one 5-hour usage window**. Use **Sonnet**, never Opus. |
| Content | Screen-recorded tutorials, single speaker. |
| Edit scope | Cuts and joins only. **No** colour grading, subtitles, animations, music, SFX or zooms. |
| Base tool | [browser-use/video-use](https://github.com/browser-use/video-use), used for its helpers (transcribe, pack, render, timeline_view). |
| Transcription | ElevenLabs Scribe (hosted, about $0.22 per audio hour). Never transcribe locally. |
| Output | Match the source resolution and fps. H.264 MP4. |

## 3. Non-negotiable editing rules for tutorials

These override anything in video-use's `SKILL.md` that conflicts with them.

1. **Silence is not dead space in a tutorial.** A silent stretch with on-screen activity (typing, clicking, scrolling, a page loading that the viewer needs to see) is a **step** and must be kept. Only cut silence when the screen is also static.
2. **Never drop a step.** If in doubt, keep it. A slightly long video beats a broken tutorial.
3. **Retakes:** when I repeat a sentence or section, keep the **last** complete take unless it's clearly worse.
4. **Cut:** filler words (um, uh, "so basically" restarts), false starts, dead air with a static screen, and off-topic tangents I explicitly abandon.
5. **The strategy is pre-approved.** Do not stop to ask for approval of the plan (this overrides video-use Hard Rule 11). The brief in section 6 *is* the approved strategy.
6. Keep all of video-use's correctness rules that still apply: word-boundary cuts, 30–200 ms padding, 30 ms audio fades at every join, per-segment extract followed by `-c copy` concat, cached transcripts, all outputs in `<videos_dir>/edit/`.
7. **Grade = `none`. Subtitles = off. Overlays = none.**

---

## 4. Phase 0 — Prepare the Mac (I do this, you verify)

- [ ] Restart the Mac.
- [ ] Kill any leftover `next-server` / `npm run dev` processes.
- [ ] Close Chrome. Keep one browser only.
- [ ] Confirm at least 15 GB of free disk space.
- [ ] Confirm the Mac is on power.

**Verify:** run `vm_stat`, `df -h ~`, and `ps aux | grep -i next` and report free memory, free disk, and any stray dev servers.

## 5. Phase 1 — Install video-use

1. Clone to `~/Developer/video-use`. If it already exists, `git pull --ff-only`.
2. Install Python deps (`uv sync`, or `pip install -e .`).
3. `brew install ffmpeg` if it's missing. Ask me before running brew.
4. Ask me for my ElevenLabs API key. Write it to `~/Developer/video-use/.env` with `chmod 600`. Never echo it back.
5. Validate the key with the quota-free `GET https://api.elevenlabs.io/v1/user` (expect 200).
6. **Do not** register video-use's own `SKILL.md` as a global skill. Its defaults (grading, subtitles, parallel animations, the approval wait) conflict with this project. We only use its `helpers/`.
7. Read every file in `helpers/` and summarise in 5 lines how `render.py` handles: grade `none`, no subtitles, no overlays, and variable-frame-rate sources.

**CHECKPOINT 1:** report install status and the `render.py` summary.

## 6. Phase 2 — Build the project files

Create this structure:

```
~/Developer/tutorial-editor/
├── CLAUDE.md                        ← points to this plan + the rules in section 3
├── brief.md                         ← the fixed, pre-approved tutorial brief
├── helpers/
│   ├── detect_screen_activity.py    ← new: finds silent-but-active spans
│   ├── chunk_edl.py                 ← new: build the EDL per ~15-min chunk, then merge
│   └── verify_edit.py               ← new: deterministic checks for the verifier
├── .claude/
│   ├── skills/tutorial-edit/SKILL.md
│   ├── agents/edl-editor.md
│   ├── agents/edit-verifier.md
│   └── settings.json                ← permission allowlist
└── run_overnight.sh
```

### 6.1 `brief.md` (the pre-approved strategy)

Write it from section 3, plus:

- Target length: whatever the content needs, typically 30–40% of the raw length. Never pad, never force a number.
- Pacing: tight but not frantic. Leave 150–300 ms of air at joins between sentences.
- Must keep: every on-screen step, every result or output shown on screen, the intro and the outro.
- Must cut: see section 3, rule 4.

### 6.2 `helpers/detect_screen_activity.py` (new, deterministic, no LLM tokens)

- Input: the source video and the Scribe transcript JSON.
- Find every gap between words of **≥ 1.0 s**.
- For each gap, measure on-screen change using FFmpeg (scene-change scores via `select='gt(scene,T)'` and/or `freezedetect`) at reduced resolution for speed.
- Classify each gap as `static` (safe to cut) or `active` (must keep, or at most trim to the active part).
- Output `edit/activity.json`, e.g. `[{start, end, class, change_score}]`.
- Thresholds must be configurable. Screen recordings change very little (a cursor moving is a tiny change), so **tune `T` on the Phase 4 test clip** and record the chosen values in `brief.md`.
- Must run on CPU in under ~5 minutes for 1 hour of footage. Downscale and lower the fps for analysis.

### 6.3 `helpers/chunk_edl.py`

- Split `takes_packed.md` and `activity.json` into chunks of about 15 minutes, breaking at the nearest silence ≥ 1 s.
- The `edl-editor` agent produces one EDL per chunk. This script merges them into `edit/edl.json` and validates: ranges are in order, nothing overlaps, every range is inside the source duration, and every `active` span is fully inside a kept range.

### 6.4 `helpers/verify_edit.py` (deterministic checks, run before the verifier agent)

- **Duration:** `ffprobe` duration of `final.mp4` equals the sum of the EDL ranges, within ±0.5 s.
- **Step coverage:** 100% of `active` spans are inside kept ranges. List any that aren't.
- **Word boundaries:** no range start or end falls inside a word in the transcript.
- **Audio:** EBU R128 loudness and true peak via `ffmpeg -af ebur128=peak=true`. Flag a true peak above −1 dBTP.
- **A/V sync:** compare audio and video stream durations. Flag a difference above 100 ms.
- **Cut ratio:** report `final_duration / source_duration`. Flag below 25% (likely over-cut) or above 70% (likely under-cut).
- Output `edit/verify/checks.json` with PASS or FAIL for each check.

### 6.5 `.claude/skills/tutorial-edit/SKILL.md` (the orchestrator)

The steps, in order:

1. Read `brief.md`. Read `edit/project.md` if it exists.
2. `ffprobe` the source. If the source has a **variable frame rate**, note it and make sure segment extraction forces a constant frame rate matching the source's nominal fps.
3. Transcribe with `~/Developer/video-use/helpers/transcribe.py` (cached), then `pack_transcripts.py`.
4. Run `detect_screen_activity.py`.
5. Run `chunk_edl.py` to split. For each chunk **sequentially**, invoke the `edl-editor` sub-agent with that chunk plus `brief.md`. Merge and validate.
6. Render with `~/Developer/video-use/helpers/render.py` (grade `none`, no subtitles, no overlays). Skip the preview render and do the final render directly to save time.
7. Run `verify_edit.py`.
8. Invoke the `edit-verifier` sub-agent.
9. If the verifier returns FAIL with specific fixes: apply them, re-render, re-verify. **Maximum 1 fix pass.** After that, stop and write the remaining issues to the report rather than looping.
10. Write `edit/REPORT.md` with: final duration, cut ratio, number of cuts, checks PASS/FAIL, the verifier's verdict, a list of timecodes I should eyeball, and the Scribe cost.
11. Append a session entry to `edit/project.md`.

Token-budget rules, stated in the skill:

- Model: Sonnet.
- Never load raw Scribe JSON into context. Use `takes_packed.md` only.
- `timeline_view.py` only at genuinely ambiguous points, **at most 10 per chunk**.
- No critic sub-agent, no animations, no parallel sub-agents.

### 6.6 `.claude/agents/edl-editor.md`

- Model: Sonnet. Tools: Read and Bash, restricted to `timeline_view.py`.
- Input: one chunk of `takes_packed.md`, that chunk's `activity.json`, and `brief.md`.
- Output: a JSON EDL array only, in the video-use EDL format (`source, start, end, beat, quote, reason`).
- Must keep every `active` span. Must snap to word boundaries. No questions: pick the most obvious interpretation.

### 6.7 `.claude/agents/edit-verifier.md`

- Model: Sonnet. Tools: Read and Bash, restricted to `ffprobe`, `timeline_view.py` and `verify_edit.py`. **It never edits anything.**
- It did not make the cuts and must not trust the editor's reasoning.
- Inputs: `final.mp4`, `edl.json`, `takes_packed.md`, `activity.json`, `checks.json`.
- Job:
  1. Read the kept transcript in output order and check the tutorial still makes sense step by step. Flag any place where an instruction refers to something that was cut ("as you saw", "now click the button we just made").
  2. Spot-check about **15 joins** with `timeline_view.py` on the **output**: the first 3, the last 3, and the 9 riskiest (shortest gaps, `active` boundaries, retake swaps).
  3. Return a verdict of **PASS** or **FAIL**, a ranked list of problems with output timecodes and evidence, and the exact EDL fixes for each.

### 6.8 `.claude/settings.json` (permission allowlist)

Pre-allow only what the pipeline needs, so an unattended run never stalls on a prompt:

- `ffmpeg`, `ffprobe`
- `python3`/`uv run` on scripts inside `~/Developer/video-use/helpers/` and `~/Developer/tutorial-editor/helpers/`
- Read/Write inside the footage folder and the project folder

Check your installed Claude Code version's documentation for the exact permission-rule syntax before writing this file. Do **not** use `--dangerously-skip-permissions`. Deny `rm -rf`, `brew`, `curl` to any host other than `api.elevenlabs.io`, and `git push`.

### 6.9 `run_overnight.sh`

- Usage: `./run_overnight.sh <path-to-footage-folder>`.
- Wrap the run in `caffeinate -i` so the Mac stays awake.
- Before starting, check: power is connected (`pmset -g batt`), free disk ≥ 15 GB, the ElevenLabs key exists, and nothing like `next-server` is running. Abort with a clear message if any check fails.
- Launch Claude Code headless (`claude -p`) with model Sonnet, telling it to run the `tutorial-edit` skill on the folder. Log everything to `<folder>/edit/run.log`.
- **One video per run.** No queueing. A run that hits the 5-hour limit stops and does not resume on its own.
- Ends by printing the path to `REPORT.md`.

**CHECKPOINT 2:** show me every file you created. I'll review `brief.md` and the two agent files before we test anything.

## 7. Phase 3 — Unit tests (no Scribe spend)

- Create a synthetic 2-minute test video with FFmpeg: alternate (a) speech-like tone + moving content, (b) silence + static frame, and (c) **silence + moving content** (the critical case).
- Write a fake Scribe JSON for it.
- Confirm `detect_screen_activity.py` labels (b) as `static` and (c) as `active`.
- Confirm `chunk_edl.py` rejects an EDL that drops an `active` span.
- Confirm `verify_edit.py` catches a deliberately broken duration and a mid-word cut.

**CHECKPOINT 3:** report the test results.

## 8. Phase 4 — Real test while I'm awake

1. I'll give you a **3-minute** real tutorial clip. Run the full pipeline interactively, not overnight.
2. Tune the activity threshold `T`. I'll confirm whether silent typing and clicking was kept.
3. Report: wall-clock time per step, peak RAM (`/usr/bin/time -l` on the render step), and how much of my usage window it used (I'll check my usage in Claude settings).

**CHECKPOINT 4:** I'll watch the output and decide whether we go to step 4.

4. Full **1-hour** tutorial, still while I'm awake. Same measurements.

**CHECKPOINT 5:** if it fits in one usage window and the edit is good, we move to overnight runs.

## 9. Phase 5 — Overnight runs

- Before bed: close apps, plug in, keep the lid open, run `./run_overnight.sh <folder>`.
- In the morning: read `REPORT.md`, eyeball the flagged timecodes, and watch at 2× speed.

## 10. Definition of done

- [ ] A 1-hour raw tutorial becomes a final MP4 with **zero dropped steps** (I confirm by watching).
- [ ] It runs unattended start to finish with no permission prompts.
- [ ] It uses one Pro usage window or less on Sonnet.
- [ ] Peak extra RAM stays under ~2 GB, with no swap explosion.
- [ ] `REPORT.md` tells me exactly where to look.

## 11. Out of scope for v1

Animations, subtitles, colour grading, music, zoom-ins on clicks, thumbnails, uploading to YouTube, multi-video queues, and cloud or GPU rendering. Revisit only after v1 is reliable.
