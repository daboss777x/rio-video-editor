# Contracts

Every helper and agent codes against these shapes. Change them only through Davis.

Conventions:
- All times are **seconds in source time** (floats, 3 decimals) unless a field says `output`.
- Every script runs from the project root with `.venv/bin/python`, takes paths as arguments, and quotes them (the project path has spaces).
- Scripts exit non-zero with a one-line reason on failure.
- `E` = a run's edit dir: `runs/<YYYY-MM-DD_HHMM>_<slug>/edit/`. The run dir (`runs/<id>/`) is video-use's `<videos_dir>`; the source video sits directly in it.
- Thresholds and priors live in `rio1/profiles/tutorial.json` (owned by Davis; agents propose changes in their report).
- The ElevenLabs key lives in `<project>/.env` as `ELEVENLABS_API_KEY=…`, chmod 600. video-use's `transcribe.py` finds it because it runs with cwd = project root.

## Inputs from video-use (read-only)
- `E/transcripts/<stem>.json`: raw Scribe response. `words[]` entries: `{text, start, end, type: "word"|"spacing"|"audio_event", speaker_id}`. Never load this into an LLM's context.
- `E/takes_packed.md`: phrase lines `  [SSSS.ss-EEEE.ee] S0 text` (from `pack_transcripts.py --edit-dir E`).

## `E/probe.json` (Ari: `rio1/helpers/media/probe.py <video> -o E/probe.json`)
```json
{"path": "...", "duration_s": 3612.4, "width": 2880, "height": 1800,
 "avg_fps": "30000/1001", "r_fps": "60/1", "vfr": true,
 "video_codec": "h264", "audio_codec": "aac", "audio_channels": 2, "size_bytes": 1234}
```

## `E/activity.json` (Ari: `rio1/helpers/media/detect_screen_activity.py`)
```json
{"source": "clip.mov", "profile": "tutorial", "params": {"gap_min_s": 1.0, "scene_T": 0.003, "...": "..."},
 "gaps": [
   {"start": 41.20, "end": 47.95, "class": "static|typing|active",
    "change_score": 0.0021, "typing_score": 0.0,
    "must_keep": [43.10, 47.95]}
 ]}
```
- One entry per gap between consecutive `word`s of ≥ `gap_min_s` (also the lead-in before the first word and the tail after the last).
- `static`: `must_keep` is `null`, so it's safe to cut entirely.
- `typing`: `must_keep` = `[end - typing_tail_s, end]`, which keeps the last ~1 s so the finished text shows.
- `active`: `must_keep` = the active sub-span (or the whole gap). It must be fully inside kept ranges.

## Chunks (Natalie: `rio1/helpers/chunk_edl.py split --edit-dir E`)
- `E/chunks/chunk_NN.md`: the chunk's phrase lines from `takes_packed.md`, plus `ACTIVITY` lines for its gaps, plus `WORDS` lines (`word@start-end`) **only** for phrases flagged as containing fillers or repeats.
- `E/chunks/chunk_NN.json`: `{"index": 1, "start": 0.0, "end": 902.3, "source": "clip.mov"}`.
- Chunks break at the gap ≥ 1 s nearest each ~15-min mark (`chunk_minutes` in the profile).

## Per-chunk EDL (written by the `edl-editor` agent)
`E/chunks/edl_NN.json`: a JSON array only:
```json
[{"source": "clip", "start": 2.42, "end": 6.85, "beat": "INTRO", "quote": "...", "reason": "..."}]
```
`source` is the video stem (the key into `sources` below).

## `E/edl.json` (Natalie: `rio1/helpers/chunk_edl.py merge --edit-dir E`)
video-use format:
```json
{"version": 1, "sources": {"clip": "../clip.mov"}, "ranges": [ ...per-chunk ranges, merged... ],
 "grade": "none", "overlays": [], "total_duration_s": 812.4}
```
Merge snaps every start and end to word boundaries with 30–200 ms padding, then validates: ranges are in order, nothing overlaps, everything is inside the source duration, and every `active` `must_keep` span is fully inside a kept range. It exits non-zero and lists the violations if any check fails.

## Render (Ari: `rio1/helpers/media/render_tutorial.py E/edl.json -o E/final.mp4 [--threads N] [--profile P]`)
- **Output (user, 2026-11-19): always 16:9 4K.** The profile's `render.output` (`size` [3840, 2160], `encoder`
  `hevc_videotoolbox`, `quality` 85 = constant quality, `prio_speed` true = the encoder's speed switch,
  1.86x faster at 4K60 for the same picture), tagged `hvc1`, bt709. A smaller recording (1080p from OBS)
  is scaled up with lanczos; one that isn't 16:9 is fitted with black bars, never stretched. `size: null`
  = the old behaviour: the first source's size, libx264 `fast` CRF 20.
- Constant frame rate at the first source's `avg_fps`, AAC 192k 48 kHz, 10 ms audio fades per segment, `-c copy` concat, the profile's `render.voice_filter`, two-pass loudnorm (−14 LUFS) plus a peak limiter, steered on the encoded AAC (up to 4 tries: loudness within 0.5 LU, true peak ≤ −1.3 dBTP; never the whole mix down unless the limiter can't do it), `+faststart`.
- **Blurs.** `E/blurs.json` `{"version": 1, "blurs": [{"source" (clip stem), "start", "end" (source s), "rect": [x, y, w, h] (source px), "text", "auto"}]}`
  from `blur_find.py` (or hand-written). Each rect is boxblurred for its window at the source's own size,
  before the scale. A blur `source` given as the clip's file name is mapped to its stem; any other
  unknown source stops the render (`blur.check_blur_sources`). The segment cache key includes the
  encoder and the segment's blurs.
- Reuses `vendor/video-use/helpers/render.py` (`concat_segments`, `apply_loudnorm_two_pass`, `probe_source_fps`) instead of copying them.

## Media checks (Ari: `rio1/helpers/media/checks.py`, imported by `verify_edit.py`)
- `probe(path) -> dict` (shape of `probe.json`)
- `loudness(path) -> {"integrated_lufs": float, "true_peak_dbtp": float}` (ebur128 peak=true)
- `av_durations(path) -> {"video_s": float, "audio_s": float}`

## `E/verify/checks.json` (Natalie: `rio1/helpers/verify_edit.py --edit-dir E`)
```json
{"overall": "PASS|FAIL", "checks": [{"name": "duration|step_coverage|word_boundaries|audio_peak|av_sync|cut_ratio", "status": "PASS|FAIL", "detail": "...", "data": {}}]}
```

## `E/verify/verdict.json` (written by the `edit-verifier` agent)
```json
{"verdict": "PASS|FAIL", "problems": [{"rank": 1, "output_tc": "03:12.4", "evidence": "...", "fix": {"op": "extend|add|remove|move", "range_index": 7, "start": 0.0, "end": 0.0}}]}
```

## Measurement (Ruth)
- `rio1/helpers/step.py --edit-dir E --name <step> -- <cmd...>` runs the command under `/usr/bin/time -l` and appends to `E/timings.jsonl`: `{"name", "started_at", "wall_s", "max_rss_mb", "exit_code"}`. It passes through the command's exit code. Step names must be `transcribe|detect|edl|render|verify|verifier` (optionally suffixed with `_`, `-`, `:` or `.` plus anything, e.g. `edl_02`); the outer headless session is `claude` (total wall time, not summed with steps).
- `rio1/helpers/monitor.py start|stop|summary --edit-dir E`: samples every 5 s into `E/monitor.jsonl` (`{"t", "used_mb", "swap_used_mb"}`). `summary` writes `E/monitor_summary.json`: `{"baseline_used_mb", "peak_used_mb", "peak_extra_mb", "peak_swap_mb", "swap_growth_mb"}`.
- `E/tokens.json`: `{"main": {"input", "output", "cache_read", "cache_creation"}, "subagents": {...same...}, "total": int, "num_turns": int, "cost_usd_equiv": float, "source": "claude-json|transcripts"}`.

## Estimate and history (Natalie)
- `rio1/helpers/estimate.py --source <video> --out E/estimate.json [--profile rio1/profiles/tutorial.json] [--history history/runs.jsonl]` prints a table and writes:
```json
{"source": {"duration_s": 0, "width": 0, "height": 0, "fps": "30/1", "vfr": false},
 "steps": {"transcribe": {"wall_s": [lo, mid, hi], "peak_rss_mb": 0}, "detect": {}, "edl": {}, "render": {}, "verify": {}, "verifier": {}},
 "totals": {"wall_s": [lo, mid, hi], "tokens": {"main": 0, "subagents": 0, "total": [lo, mid, hi]}, "peak_extra_ram_mb": [lo, mid, hi], "scribe_usd": 0.0},
 "calibration": {"runs_used": 0, "coefficients": {}},
 "gate": {"ok": true, "reasons": []}}
```
- Model: each step's cost = coefficient × driver (source minutes, words, chunks, output megapixel-seconds). Coefficients are the profile priors until history exists; after that, each one is the median of its last 5 observed actual/driver ratios.
- `rio1/helpers/record_run.py --run-dir runs/<id>` reads `estimate.json`, `timings.jsonl`, `monitor_summary.json`, `tokens.json`, `verify/*.json` and `probe.json`. It then appends a line to `history/runs.jsonl` (the prediction, the actuals, error % per metric, outcome), appends a section to `history/RUN_LOG.md`, and adds **Proposed** items to `history/LESSONS.md` from verifier FAILs and from any filled-in `feedback.md` in earlier run folders that hasn't been ingested yet.
- `history/LESSONS.md` has two sections, `## Approved` and `## Proposed`. The skill reads only `## Approved`. Only the user moves items between them.

## Multi-clip runs and the video plan (added 2026-10-29, v2 workflow)
The user drops **several clips plus an optional plan doc** in `input/`, e.g. `intro.mp4 main1.mp4 main2.mp4 outro.mp4 plan.docx`. Every clip is edited and they're **joined in order into ONE output video**. A single clip is just the N=1 case of the same shapes.

- **Order** (Ruth, edit.sh): `intro*` first, then the rest in natural order (main1 < main2 < main10, other names alphabetical), `outro*` last. Matching is case-insensitive on the stem. Stems must be unique.
- **Plan doc** (Ruth, edit.sh): at most one of `.docx .doc .rtf .txt .md` goes in `input/`. It's converted with macOS `textutil -convert txt` (`.md`/`.txt` are copied) to `E/plan.md`, and the original is moved into the run dir. No plan doc is fine. `.pdf` is refused with a clear message (textutil can't read it).
- **`E/sources.json`** (Ruth, edit.sh, written before Claude starts):
```json
{"order": ["intro", "main1", "main2", "outro"],
 "files": {"intro": "intro.mp4", "main1": "main1.mp4", "main2": "main2.mp4", "outro": "outro.mp4"},
 "plan": "plan.md"}
```
  `plan` is null when there's no plan doc. Readers fall back to the single video in the run dir when the file is missing (old runs).
- **`E/probe.json`** (Ari, `probe.py <v1> [<v2> ...] -o E/probe.json`): same top-level fields as before, with a backward-compatible extension.
  - Taken from the **first clip in order**: `path`, `width`, `height`, `avg_fps`, `r_fps`, codecs. These set the output format.
  - Summed or combined across all clips: `duration_s` (sum), `size_bytes` (sum), `vfr` (true if any clip is VFR).
  - New field: `"clips": {stem: <the single-clip probe dict>}`.
  - With one clip, `clips` has one entry.
- **Activity** (Ari's detector, run once per clip by the skill): `E/activity/<stem>.json`, same shape as the old `E/activity.json`. Readers use `load_activity(edit_dir, stem)` and fall back to `E/activity.json` for old runs. Under `silence_policy: cut_all` activity is background only.
- **Chunks** follow `sources.json` order: chunk_01… are the intro clip's, then main1's, and so on. Chunks never span two clips. `chunk_NN.json` `source` = that clip's file name.
- **Merge** orders ranges by `sources.json` order, then by time. `edl.json` `sources` lists every clip.
- **Pacing:** a range is **intro** (air_ms.intro and compact_gap_ms.intro) if its `beat` starts with INTRO **or** its source stem starts with "intro". Everything else is body.
- **Render** (Ari): the output takes the first clip's resolution and avg fps. A clip with a different size is scaled to fit and padded (letterboxed, never stretched), and a clip with a different fps is converted. Audio is always 48 kHz stereo. It stays drift-free across clips.
- **Transcript cache** (Ruth, edit.sh): `cache/transcripts/<key>.json`. The key is sha256 of (file size + first 64 MB + last 64 MB). Before Claude starts, edit.sh copies a cached transcript to `E/transcripts/<stem>.json`, so `transcribe.py` skips Scribe. Afterwards it stores any new transcript under its key. `cache/` is gitignored. A clip is never paid for twice.
- **Plan for the editor** (Rio, in the skill): if `E/plan.md` exists, Rio writes `E/plan_brief.md` once, ≤ 600 words: the planned sections in order, mapped to clips where obvious, and the key steps and points of each. Each edl-editor call gets `plan_brief.md` (never the full plan) and is told: "a guide to intent and order. The speaker does not follow it word for word. Edit what was actually said."
- **Estimate** (Natalie): `estimate.py --source <v1> [--source <v2> ...]` (or `--run-dir`) sums across clips. Its `source` block = the probe.json top-level fields above.

## Revise loop (added 2026-10-29, approved by the user)
After a run, the user watches the output and writes timestamped notes. Only the noted sections are re-edited, only the changed segments are re-rendered, and a new full version comes out. The overnight run itself is unchanged and stays fully autonomous.

- **Segment cache** (Ari, `render_tutorial.py`): every rendered range is kept in `E/segments/` as `<key>.mp4` (video) + `<key>.wav` (audio).
  - `key` = sha256 of (source file's resolved path, size and mtime; start; n frames; output fps; output size; the fit/HDR filter; CRF/PRESET/FADE_S; and a `CACHE_VERSION` constant), first 20 hex chars.
  - A render reuses a cached pair when both files exist and are valid, and extracts only what's missing. The concat and two-pass loudnorm still run over the whole video, so the loudness is always for the full edit.
  - It writes `E/segments/manifest.json` = `{"renders": [{"at", "edl", "keys": [...]}]}`, newest last. It keeps the last 2 renders, so the previous version can still be re-rendered quickly, and deletes any other `E/segments/*` files.
  - It prints `cache: R reused, X extracted`.
  - Flags: `--no-cache` (extract everything and keep nothing, the old behaviour) and `--keep-intermediates` (unchanged).
  - Output is byte-identical in duration and frame count to an uncached render.
- **Notes file** (user, plain text, one note per line): `<timecode> <separator> <text>`.
  - The timecode is `M:SS`, `MM:SS`, `H:MM:SS` or any of those with `.s`. The separator is any of `-`, `–`, `—`, `:` or spaces.
  - Blank lines and lines starting with `#` are ignored.
  - An optional first line `version: N` says which output version the notes refer to. The default is the latest.
- **Versions**: v1 = the original run's `E/edl.json` and `output/<name>_edited.mp4`. Revision N lives in `E/revisions/vN/`.
  - Before the first revision, the v1 EDL is archived to `E/revisions/v1/edl.json`.
  - `E/edl.json` always holds the latest version, and `E/final.mp4` is the latest render.
  - Outputs are `output/<name>_edited_vN.mp4` and `output/<name>_REVISION_vN.md`. Older versions are never deleted.
- **`rio1/helpers/revise.py`** (Natalie):
  - `start --edit-dir E --notes FILE [--of N] [--window-s 60]`
    - Parses the notes and maps each timecode to its output version's EDL. Output time = cumulative sum of each range's `n/fps` frame duration, the same as the render, with `fps` from `E/probe.json`.
    - The **window** is the ranges whose output span overlaps `tc ± window_s`. Overlapping windows are merged.
    - Creates `E/revisions/vN/` with `notes.txt`, `windows.json` and `window_NN.md`.
    - `windows.json` = `[{"id", "notes": [{"tc_s", "text"}], "out_start", "out_end", "range_first", "range_last", "source", "src_start", "src_end"}]`. A window never spans two sources; if it would, it's split per source.
    - `window_NN.md` is like a chunk file. It holds the note(s); the transcript phrases of that source from `src_start` to `src_end`, where those are the window's first range start − 20 s and last range end + 20 s, clamped; and every word marked as **KEPT** or **CUT** in the current EDL; plus the current ranges of the window as JSON. Rosa can then see what was dropped near the problem.
    - Prints N and the window list.
  - Rosa writes `E/revisions/vN/window_NN.edl.json`: a JSON array in the per-chunk EDL shape that **replaces** ranges `range_first..range_last`. It stays inside `[src_start, src_end]` of that source.
  - `apply --edit-dir E --version N`
    - Splices every window's replacement into the EDL, in descending window order.
    - Runs the same refit and `heal_edges` as `chunk_edl.py validate --refit`, then validates.
    - Runs `clipped_edges` and the long-silence check on the changed ranges.
    - On success, writes `E/edl.json` (the version N EDL) and `E/revisions/vN/edl.json`.
    - Writes `E/revisions/vN/changes.json` = `[{"window", "notes", "before": [ranges], "after": [ranges], "new_out_start", "new_out_end"}]`.
    - Exits 1 and lists the violations per window if anything fails, leaving `E/edl.json` untouched.
  - `report --edit-dir E --version N [--lessons history/LESSONS.md]`
    - Writes `E/revisions/vN/REVISION.md`: for each note, what changed (words removed or restored, in plain words) and its new output timecode, followed by the checks summary.
    - Appends one dated line per note to `## Proposed` in LESSONS.md: `- [YYYY-MM-DD <run> vN] "<note>" -> <change>`.
- **Orchestration** (Ruth):
  - `./edit.sh --revise <run dir | name> <notes file>` (headless) and `/rio revise` (in chat; Rio writes the notes the user types to a file first) run the same steps:
    1. `revise.py start`
    2. Rosa (edl-editor, "revise window" mode) in **batches of up to 5 windows per call** (user, 2026-10-29: saves Rosa start-up time and tokens), batches one at a time. Within a batch she treats each window separately and writes each `window_NN.edl.json`. Step names `edl_rev_B` per batch (B = 01, 02, …).
    3. `revise.py apply` (on failure, ONE Rosa call retries all failing windows once, given the violation lines)
    4. render (cached)
    5. `verify_edit.py`
    6. `revise.py report`
    7. copy to `output/<name>_edited_vN.mp4` plus the REVISION report
  - No Ines in a revise, because the user is the reviewer.
  - Step names: `edl_rev_B` (per batch), `edl_rev_apply`, `render_rev`, `verify_rev`.

## Rio 1 for one OBS video (added 2026-11-19, approved by the user)
Rio 1 edits one already-layered recording (OBS puts the camera on the screen). It has everything Rio
2.0 has except the camera layering (sync, face tracking, layouts, 4K composite) and insert clips.
- **Cutting and revising** (ported from Rio 2.0): words the transcriber stretched are kept
  (`_holds_word_sound`), `hold_edges` ranges keep their lead-in and tail, `move_after` sections,
  the silence floor ignores digital silence, revise span notes (`20:30 - 21:55 <note>`), revise
  pre-roll for clipped starts, and a per-note REVISION report.
- **Sensitive info question.** `edit.sh --sensitive yes|no|unsure` (any mode but revise; asked at a
  terminal when not given, else `unsure`) → `E/sensitive.json` `{"answer", "blur_scan"}`; `/rio` asks
  it with AskUserQuestion. `no` = no blur scan and no `blur` step in the forecast (`estimate.py --no-blur`).
- **Auto-blur** (`rio1/helpers/media/blur_find.py --edit-dir E --sensitive [--text <word> ...]`, SKILL
  step 4b): every 1 s (profile `blur.step_s`) in every kept range, macOS Vision OCR (`vision.py` +
  `vision.swift`). `sensitive_boxes` finds `secret` (keys, secrets, long IDs, also in URLs), `email`,
  `phone`, `key_value` (`.env` lines), `gmail` (the open mail pane) and `private` (the whole screen
  inside a folder named in `blur.private_folders`). Hand-written entries (no `auto`) survive re-runs.
  Revise notes that ask for a blur are placed by hand in `E/blurs.json` (clip stem, source seconds).
- **Output names.** `intake.scan --output-dir` names the video after the plan doc's title (first
  line), then the old rules, and adds `-2`, `-3`… when `output/<name>_edited[_vN].mp4` already exists.
- **Forecast.** `blur` = `blur_s_per_output_min` x output min; 4K render = `render4k_s_per_output_gigapixel`
  x output frames x output MP / 1000 (RSS `render4k_peak_rss_mb`). record_run writes these instead of
  the old `render_s_per_output_megapixel_min` when `drivers.render_mode` is `4k`.

## Premiere Pro XML (user, 2026-11-25)

Every finished edit and every revision also writes, next to the video in `output/`:
- `<name>_edited[_vN].xml`: a Final Cut Pro 7 XML (xmeml v4) sequence that Premiere Pro imports
  (File > Import). `helpers/media/premiere_xml.py --edit-dir E -o <xml>`, called by `edit.sh`
  (`premiere_xml`) after the video is copied; best effort, it never fails a run.
- `<name>_edited[_vN]_audio.wav`: Rio's finished sound (voice filter + loudness), 48 kHz stereo PCM.

The sequence: the first clip's size and frame rate (NTSC when the rate's denominator is 1001).
V1 = one clip item per EDL range on the raw clip (`edl.sources`, resolved from E), timeline frames
from the running total of exact seconds, so nothing drifts; source length = timeline length.
A1/A2 = Rio's audio (on); A3/A4 = each range's raw audio (off while Rio's audio is there).
Markers: one per blurred stretch (`blurs.json`, mapped to output time, same kind + overlapping =
one, named `Blur: <kind>`) and one per verifier problem (`verdict.json` `output_tc`, `Ines #<rank>`).
Not rebuilt (the user chose raw cuts only): blurs, the 4K upscale, the voice filter, the camera
layout (Rio 2.0). The XML points at the raw clips in `runs/<id>/`: moving them means relinking.
