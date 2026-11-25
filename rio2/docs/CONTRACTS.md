# Contracts

Every helper and agent codes against these shapes. Change them only through Davis.

Conventions:
- All times are **seconds in source time** (floats, 3 decimals) unless a field says `output`.
- Every script runs from the project root with `.venv/bin/python`, takes paths as arguments, and quotes them (the project path has spaces).
- Scripts exit non-zero with a one-line reason on failure.
- `E` = a run's edit dir: `runs/<YYYY-MM-DD_HHMM>_<slug>/edit/`. The run dir (`runs/<id>/`) is video-use's `<videos_dir>`; the source video sits directly in it.
- Thresholds and priors live in `rio2/profiles/tutorial.json` (owned by Davis; agents propose changes in their report).
- The ElevenLabs key lives in `<project>/.env` as `ELEVENLABS_API_KEY=…`, chmod 600. video-use's `transcribe.py` finds it because it runs with cwd = project root.

## Inputs from video-use (read-only)
- `E/transcripts/<stem>.json`: raw Scribe response. `words[]` entries: `{text, start, end, type: "word"|"spacing"|"audio_event", speaker_id}`. Never load this into an LLM's context.
- `E/takes_packed.md`: phrase lines `  [SSSS.ss-EEEE.ee] S0 text` (from `pack_transcripts.py --edit-dir E`).

## `E/probe.json` (Ari: `rio2/helpers/media/probe.py <video> -o E/probe.json`)
```json
{"path": "...", "duration_s": 3612.4, "width": 2880, "height": 1800,
 "avg_fps": "30000/1001", "r_fps": "60/1", "vfr": true,
 "video_codec": "h264", "audio_codec": "aac", "audio_channels": 2, "size_bytes": 1234}
```

## `E/activity.json` (Ari: `rio2/helpers/media/detect_screen_activity.py`)
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

## Chunks (Natalie: `rio2/helpers/chunk_edl.py split --edit-dir E`)
- `E/chunks/chunk_NN.md`: the chunk's phrase lines from `takes_packed.md`, plus `ACTIVITY` lines for its gaps, plus `WORDS` lines (`word@start-end`) **only** for phrases flagged as containing fillers or repeats.
- `E/chunks/chunk_NN.json`: `{"index": 1, "start": 0.0, "end": 902.3, "source": "clip.mov"}`.
- Chunks break at the gap ≥ 1 s nearest each ~15-min mark (`chunk_minutes` in the profile).

## Per-chunk EDL (written by the `edl-editor` agent)
`E/chunks/edl_NN.json`: a JSON array only:
```json
[{"source": "clip", "start": 2.42, "end": 6.85, "beat": "INTRO", "quote": "...", "reason": "..."}]
```
`source` is the video stem (the key into `sources` below).

## `E/edl.json` (Natalie: `rio2/helpers/chunk_edl.py merge --edit-dir E`)
video-use format:
```json
{"version": 1, "sources": {"clip": "../clip.mov"}, "ranges": [ ...per-chunk ranges, merged... ],
 "grade": "none", "overlays": [], "total_duration_s": 812.4}
```
Merge snaps every start and end to word boundaries with 30–200 ms padding, then validates: ranges are in order, nothing overlaps, everything is inside the source duration, and every `active` `must_keep` span is fully inside a kept range. It exits non-zero and lists the violations if any check fails.

## Render (Ari: `rio2/helpers/media/render_tutorial.py E/edl.json -o E/final.mp4 [--threads N]`)
- Same resolution as the source (no scaling), constant frame rate at the source's `avg_fps`, libx264 `fast` CRF 20, yuv420p, AAC 192k 48 kHz, 30 ms audio fades per segment, `-c copy` concat, two-pass loudnorm (−14 LUFS, −1 dBTP), `+faststart`.
- Reuses `vendor/video-use/helpers/render.py` (`concat_segments`, `apply_loudnorm_two_pass`, `probe_source_fps`) instead of copying them.

## Media checks (Ari: `rio2/helpers/media/checks.py`, imported by `verify_edit.py`)
- `probe(path) -> dict` (shape of `probe.json`)
- `loudness(path) -> {"integrated_lufs": float, "true_peak_dbtp": float}` (ebur128 peak=true)
- `av_durations(path) -> {"video_s": float, "audio_s": float}`

## `E/verify/checks.json` (Natalie: `rio2/helpers/verify_edit.py --edit-dir E`)
```json
{"overall": "PASS|FAIL", "checks": [{"name": "duration|step_coverage|word_boundaries|audio_peak|av_sync|cut_ratio", "status": "PASS|FAIL", "detail": "...", "data": {}}]}
```

## `E/verify/verdict.json` (written by the `edit-verifier` agent)
```json
{"verdict": "PASS|FAIL", "problems": [{"rank": 1, "output_tc": "03:12.4", "evidence": "...", "fix": {"op": "extend|add|remove|move", "range_index": 7, "start": 0.0, "end": 0.0}}]}
```

## Measurement (Ruth)
- `rio2/helpers/step.py --edit-dir E --name <step> -- <cmd...>` runs the command under `/usr/bin/time -l` and appends to `E/timings.jsonl`: `{"name", "started_at", "wall_s", "max_rss_mb", "exit_code"}`. It passes through the command's exit code. Step names must be `transcribe|detect|edl|render|verify|verifier` (optionally suffixed with `_`, `-`, `:` or `.` plus anything, e.g. `edl_02`); the outer headless session is `claude` (total wall time, not summed with steps).
- `rio2/helpers/monitor.py start|stop|summary --edit-dir E`: samples every 5 s into `E/monitor.jsonl` (`{"t", "used_mb", "swap_used_mb"}`). `summary` writes `E/monitor_summary.json`: `{"baseline_used_mb", "peak_used_mb", "peak_extra_mb", "peak_swap_mb", "swap_growth_mb"}`.
- `E/tokens.json`: `{"main": {"input", "output", "cache_read", "cache_creation"}, "subagents": {...same...}, "total": int, "num_turns": int, "cost_usd_equiv": float, "source": "claude-json|transcripts"}`.

## Estimate and history (Natalie)
- `rio2/helpers/estimate.py --source <video> --out E/estimate.json [--profile rio2/profiles/tutorial.json] [--history history/runs.jsonl]` prints a table and writes:
```json
{"source": {"duration_s": 0, "width": 0, "height": 0, "fps": "30/1", "vfr": false},
 "steps": {"transcribe": {"wall_s": [lo, mid, hi], "peak_rss_mb": 0}, "detect": {}, "edl": {}, "render": {}, "verify": {}, "verifier": {}},
 "totals": {"wall_s": [lo, mid, hi], "tokens": {"main": 0, "subagents": 0, "total": [lo, mid, hi]}, "peak_extra_ram_mb": [lo, mid, hi], "scribe_usd": 0.0},
 "calibration": {"runs_used": 0, "coefficients": {}},
 "gate": {"ok": true, "reasons": []}}
```
- Model: each step's cost = coefficient × driver (source minutes, words, chunks, output megapixel-seconds). Coefficients are the profile priors until history exists; after that, each one is the median of its last 5 observed actual/driver ratios.
- `rio2/helpers/record_run.py --run-dir runs/<id>` reads `estimate.json`, `timings.jsonl`, `monitor_summary.json`, `tokens.json`, `verify/*.json` and `probe.json`. It then appends a line to `history/runs.jsonl` (the prediction, the actuals, error % per metric, outcome), appends a section to `history/RUN_LOG.md`, and adds **Proposed** items to `history/LESSONS.md` from verifier FAILs and from any filled-in `feedback.md` in earlier run folders that hasn't been ingested yet.
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
- **`rio2/helpers/revise.py`** (Natalie):
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

---

## Rio 2.0: two-camera 4K mode (rio2 only)

Added 2026-11-03 by Davis. The original Rio (rio1/) never sees any of this.

**When it is on:** at least one camera file is in `input/`. A camera file is a video whose stem
contains `cam` (case-insensitive), e.g. `tdevsetupcam.MOV` or `cam1.mov`. Every other video is a
screen clip, ordered as before. No camera file means rio2 behaves exactly like rio1 (same output
size, same encoder).

**Editing never changes:** transcription, silences, activity, Rosa, merge and Ines work on the
screen clips only. The camera is only used at render time.

### `E/sources.json` (intake, Ruth)
Adds one key: `"cams": {"<cam stem>": "<file name>", ...}` (`{}` when there are none). The cam
files are moved into the run dir like the clips, but they are NOT in `order`.

### `E/cam_sync.json` (`rio2/helpers/media/sync_cam.py`, Ari)
```json
{"ok": true,
 "cams": [{"cam": "tdevsetupcam", "file": "tdevsetupcam.MOV", "screen": "tdevsetupmain1",
           "offset_s": 3.214, "drift": 4.8e-05, "residual_ms": 6.1,
           "covers": [0.0, 1612.4],
           "points": [[120.0, 123.22, 0.91], ...]}],
 "notes": []}
```
- Mapping: `cam_t = offset_s + (1 + drift) * screen_t`. `covers` = the screen-time span the camera
  file covers.
- Each cam is matched to the screen clip whose audio correlates best.
- `ok` is false when any cam has `residual_ms > layout.max_sync_residual_ms` (20) or no confident
  match. The render then stops (the orchestrator reports it) rather than ship lips out of sync.
- CLI: `.venv/bin/python rio2/helpers/media/sync_cam.py --edit-dir E` (reads sources.json and the
  run dir's files). Deterministic, no network, about 1 min per hour of audio. RAM under 500 MB (8 kHz
  mono, windowed correlation).

### `layout` in `rio2/profiles/tutorial.json` (Ari)
```json
"layout": {"canvas": [3840, 2160], "fps": null,
           "background": "assets/background.png", "background_color": "#101418",
           "screen_rect": [x, y, w, h], "screen_radius": 24,
           "cam_crop": [x, y, w, h], "cam_rect": [x, y, w, h], "cam_radius": 48,
           "encoder": "hevc_videotoolbox", "bitrate": "40M", "max_sync_residual_ms": 20}
```
- Every rect is in pixels. `cam_crop` is measured in the camera's own frame. `fps: null` means the
  first screen clip's avg fps.
- `background` is relative to the project root (shared `assets/`, which the user supplies). If it
  is missing, `background_color` is used.

### Render in composite mode (`render_tutorial.py`, Ari)
- Turned on when `cam_sync.json` exists with `ok: true`.
- Per kept range: background + screen [s, e] scaled into `screen_rect` + camera [cam(s), cam(e)]
  cropped, scaled into `cam_rect` and given rounded corners. Exactly n frames at the output fps,
  constant frame rate.
- If a range falls outside the camera's `covers`, it renders the screen on the background without
  the camera, and the gap is listed in `E/composite_notes.json`.
- Audio is unchanged (the screen clip's audio, the same pipeline). The segment cache key includes
  the layout and the cam sync.

### `layout_preview.py` (Ari)
`.venv/bin/python rio2/helpers/media/layout_preview.py --edit-dir E [--at S ...] -o E/layout/`
writes a few PNG stills of the composite, for the user to approve the layout. No video render.

### History (Natalie)
rio2's `record_run` adds `"system": "rio2"` and `"composite": true|false` to each runs.jsonl
line. A line without `system` is rio1. rio2's estimator calibrates the render prior separately for
composite runs.

## Rio 2.0: layouts and inserts (added 2026-11-09, user-approved plan; rio2 only)
- **Inserts.** A video whose stem contains `insert` or `testimonial` (checked before `cam`) is an
  insert clip: moved into the run, `sources.json` `"inserts": {stem: file}` (key present only when
  there are any), transcribed + silence-scanned + cached like a clip, never in `order`, never probed,
  chunked or camera-synced. `chunk_edl.py split` writes them to `E/inserts_packed.md`.
- **EDL range fields.** `"layout": "split"|"face"` (optional, default split; composite runs only) and
  `"insert": true` on ranges whose `source` is an insert clip. `edl.json` gains `"inserts": [stems]`.
  The editor writes an insert range right after the main range that leads into it; `merge` anchors
  it there (splitting a joined main range at the anchor if needed), applies the light touch
  (profile `insert.max_pause_s` 0.6 / `insert.air_ms` 150; fillers kept) and `heal_edges`, and
  `_join_touching` never moves inserts or joins ranges of different layouts. Then
  `smooth_layouts`: face runs < `layout.min_face_s` (6) → split; split blips < `layout.min_split_s`
  (2) between face runs → face. `long_silences` skips inserts.
- **Render.** face = the synced camera alone, crop of the canvas aspect `layout.face_zoom` (1.3)
  tighter, centred on `layout.face_center` (null = centre of `cam_crop`), scaled to the canvas; no
  camera → split picture without the camera, logged in `composite_notes.json` with reason `face: ...`.
  Insert = fitted to the canvas (full), its audio shifted by a fixed gain to the main clip's
  integrated loudness (±12 dB, `E/insert_gain.json`), then the whole edit is voice-filtered and
  loudnormed as before. `COMPOSITE_VERSION` 2; the mode is in the segment key.
- **Slides.** `helpers/media/slides.py --edit-dir E` → `E/slides/<stem>.json`
  `{"source", "threshold", "slides": [{"t", "still"}]}` + 480 px JPEGs; `split` adds
  `SLIDE t still=...` and `NO CAMERA a-b` (from cam_sync covers) lines to each chunk.
- **Checks.** advisory `layouts` (minutes per mode, switches, face runs < 3 s flagged); `cut_ratio`
  excludes inserts. Verifier fix op `layout` (`range_index..range_last` → layout).
- **Layout stills.** `layout_preview.py` adds `face_<t>.png` per camera still and
  `insert_<stem>.png` per insert.
- Not yet: revise windows treat an insert range like any range of its source (whole-range edits only).

## Rio 2.0: camera look and blur (added 2026-11-09, user feedback on HMJ v1; rio2 only)

- **Tone.** `layout.cam_hdr_gain` (0.6) scales the iPhone HLG light before the SDR LUT, and
  `layout.cam_eq` (`contrast=1.12:gamma=0.88:saturation=1.05`, an ffmpeg `eq`) runs after it, on
  the camera only (split bubble and face mode; never the screen or inserts). Picked by eye from an
  A/B of four looks ("C"); change only after a new A/B.
- **Face tracking.** `face_track.py --edit-dir E` → `E/face_track.json`
  `{"version": 1, "step": 1.0, "cams": {"<cam>": [[cam_t, cx, cy, face_h], ...]}}` (camera pixels;
  macOS Vision via `helpers/media/vision.swift`, no installs). With `layout.face_track` set
  (`still_px` 160, `deadband_px` 80, `y_offset` 0.1, `max_gap_s` 3), the render centres each cut's
  crop (bubble and face mode) on face centre + y_offset × face height: a fixed crop when the face
  spread in the cut is under still_px (and it stays on the previous cut's point within
  deadband_px), else a linear pan through the per-second points (median-of-3 smoothed), clamped
  inside the frame. No face within max_gap_s → the fixed `cam_crop`/`face_center`. `face_track: null`
  turns it off. The track is part of the segment cache key.
- **Auto-blur of sensitive text (2026-11-17).** `blur_find.py --edit-dir E --sensitive` runs on every
  two-camera run. `sensitive_boxes(items, w, h, private)` classifies the OCR words of each frame: `secret`
  (a 16+ character segment that switches between upper case, lower case and digits 6+ times: keys,
  secrets, Drive/Apps Script IDs, also inside URLs; OCR look-alike letters count), `email`, `phone`
  (9-15 digits, never a date or a time), `key_value` (`.env` style `NAME=value`, no spaces, value 8+),
  `gmail` (Gmail's own Compose + Inbox + Search on screen: the pane right of the sidebar, below the
  search bar) and `private` (a Drive breadcrumb "My Drive …" in heading size naming a folder in the
  profile's `blur.private_folders`: the whole frame). Each becomes a blur entry with `text` = the kind.
  Profile block `blur`: `sensitive`, `step_s` (1.0), `private_folders`. The render maps a blur `source`
  given as the clip's file name to its stem and stops on any other unknown source
  (`composite.check_blur_sources`).
- **Output names (2026-11-17).** `intake.scan` names the output after the plan doc's title (first line),
  then the old rules; `--output-dir` adds `-2`, `-3`… when `output/<name>_edited[_vN].mp4` already exists.
  Revise notes may be spans: `20:30 - 21:55 <note>` gives `tc_end_s`; its window reaches the end + 60 s.
- **Blurs.** `blur_find.py --edit-dir E --text <word> [...]` → `E/blurs.json`
  `{"version": 1, "blurs": [{"source", "start", "end", "rect": [x, y, w, h], "text", "auto": true}]}`
  (source seconds and pixels). Text is read once a second in kept split ranges (Vision OCR);
  sightings of a word in the same place ≤ 2.5 s apart merge; each blur is padded by one step in
  time and half the text height in space. Hand-written entries (no `auto`) survive re-runs. The
  render blurs each rect (boxblur) on the screen for its window; composite mode only. Part of the
  cache key.
- **Loudness and peaks, steered on the encoded audio** (2026-11-09, after HMJ v3 came out at
  −16.7 LUFS). The final encode runs `alimiter` (latency-compensated) on loudnorm's 192 kHz
  output, then encodes the AAC audio alone and measures it (ebur128). Up to 4 tries: loudness off
  −14 by more than 0.5 LU → the gain moves; true peak over −1.3 dBTP → only the limiter's ceiling
  drops (start −3 dBFS, floor −9), never the whole mix. Then the video is muxed once (`-c copy`).
  On HMJ v3's audio: 2 tries, −14.4 LUFS / −1.3 dBTP (about 2 min).
- **Encoder quality.** `layout.quality` (80) = hevc_videotoolbox constant quality (`-q:v`, Apple
  Silicon), replacing the fixed `bitrate` (kept as the fallback when quality is null). Picked by
  VMAF on lossless 10 s HMJ clips: face shot 40M → 93.7, q80 → 96.6 (~77 Mbit/s); split 40M → 95.4,
  q80 → 95.2 (~13 Mbit/s). It is in the layout, so changing it invalidates the segment cache.

