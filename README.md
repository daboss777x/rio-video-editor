# Rio: an AI video editor for YouTube tutorials

Rio turns about an hour of raw screen recording into a tight, finished 20-25 minute tutorial. It
cuts every pause, "um", false start and retake (on the sound, so no word is ever clipped), blurs
API keys and emails on screen, cleans up your voice and hands you a 4K video plus a report. You
drop the clips in a folder, and a crew of Claude agents does the rest, overnight or live in a chat
while you watch.

It only **cuts and joins**. No music, captions, zooms or effects: it does the slow part of editing,
and you keep the creative part.

## Who it's for

| Who | What it takes off their plate |
|---|---|
| YouTubers who make screen-recorded tutorials (coding, no-code, AI tools, software walkthroughs) | Hours of cutting silences, retakes and filler out of every video |
| Course creators and educators | Turning long recorded lessons into tight modules, consistently |
| Agencies and freelancers who record client walkthroughs or demos | Fast first cuts, and blurring client keys, emails and IDs before anything is shared |
| SaaS teams recording product demos and docs videos | A repeatable edit with the same pacing, loudness and quality every time |
| Video editors | A clean first assembly to start from instead of a raw hour |

## How it creates value

- **Time.** The rough cut is the slow, boring part of editing a talking tutorial. Rio does it
  unattended: a 1-hour recording took about an hour of machine time on an 8 GB MacBook Air, with no
  one watching.
- **Fewer leaks.** Keys, passwords, emails and phone numbers on screen are found and blurred
  automatically, then checked again by a second pass and an AI reviewer. You still check, but
  you're not hunting frame by frame.
- **Consistent quality.** Same pacing, same voice clean-up, same YouTube loudness (−14 LUFS) on
  every video. Every cut is placed on the audio, so words aren't chopped.
- **Cheap to run.** Transcription costs about $0.22 per hour of footage (AssemblyAI). The AI work
  runs on a Claude subscription. On a Pro plan a full edit used about 2% of a 5-hour window.
- **Easy fixes.** Watch the result, write "4:05 cut this tangent", and only that spot is re-edited.

## Features

- Several clips joined in order (intro, main1, main2, outro), guided by your script if you have one.
- Every pause cut, unless you announce a silent demo ("watch this").
- Retakes and rephrasings: the last good take is kept.
- Auto-blur of keys, secrets, `.env` values, emails, phone numbers, an open Gmail pane and private
  folders, judged by an AI so ordinary words stay readable.
- Voice clean-up (tone fix and light denoise), YouTube loudness, 4K HEVC output.
- An AI reviewer that listens back to the finished video and flags clipped words or repeats.
- Revise loop: timestamped notes produce v2, v3 and so on, re-rendering only what changed.
- A Premiere Pro file with every edit (`output/<name>_edited.xml`, File > Import): Rio's cuts on
  your raw clips, still editable, plus Rio's cleaned audio and markers at every blur and every spot
  the reviewer flagged. Blurs and the 4K upscale are redone by hand in Premiere.
- A time, cost and memory forecast before every run that learns from your past runs.
- **Rio 2.0:** a two-camera mode (phone camera synced by sound, face bubble next to the screen,
  face tracking, face-only shots, testimonial inserts).
- The Studio: a live pixel-art view of the crew at work.

## How it works

```
input/  clips + script
   |
   v
[1] Rio prepares ............ edit.sh / /rio (Claude Code)      forecast, preflight, run folder
   |
   v
[2] Listen .................. AssemblyAI + ffmpeg               words, fillers, 10 ms loudness map
   |
   v
[3] Cut ..................... Rosa (Opus), one chunk at a time  guided by brief.md, glossary.md, script
   |                          merge: cuts every pause on the audio, never mid-word
   v
[4] Blur + render ........... screen reader + Bea (Sonnet)     keys/emails blurred, 4K HEVC, voice clean-up
   |
   v
[5] Check ................... listen-back + Ines (Opus)        at most one fix pass
   |
   v
output/  finished video  +  Reports/  report   ---> history/ (forecast learns, lessons you approve)
   ^                                          |
   +---------- your notes: revise ------------+
```

A hand-drawn version is in [docs/architecture.html](docs/architecture.html).

## Project structure

```
CLAUDE.md, SETUP.md       instructions for Claude Code (SETUP.md runs first on a new machine)
brief.md, glossary.md     the editing strategy and the names your transcripts mishear
edit.sh, edit2.sh         start Rio 1 / Rio 2.0
rio1/                     Rio 1: helpers (media, logic, tooling), profiles, config, tests, docs
rio2/                     Rio 2.0: the same plus camera mode
.claude/agents/           Rosa (editor), Ines (verifier), Bea (blur judge)
.claude/skills/           rio, rio2, tutorial-edit, tutorial-edit-2, studio
history/                  run log, forecast data, lessons (Proposed and Approved)
assets/                   backgrounds for camera mode
docs/                     the original spec and the architecture map
input/ output/ Reports/ runs/    your videos and results (not in git)
```

## Setup

The easiest way: **let Claude Code set it up.** It checks your computer, explains what (if
anything) needs to change for it, and adapts the code after you say yes.

1. Install [Claude Code](https://docs.claude.com/en/docs/claude-code) and sign in. A paid Claude
   plan is needed (Pro works; Rosa and Ines use Opus).
2. Download this project: `git clone https://github.com/daboss777x/rio-video-editor` (or
   Code → Download ZIP), and open a terminal in the folder.
3. Run `claude` in the folder. Accept "trust this folder".
4. Say: **"Set this project up for my computer."** Claude reads `CLAUDE.md`, which sends it to
   `SETUP.md`. It will:
   - audit your memory, processor, graphics card, disk and operating system,
   - tell you what Rio needs to change for your machine (Windows gets a "less tested" notice),
   - wait for your OK,
   - install what's missing (ffmpeg, jq, Python packages, video-use), help you add your AssemblyAI
     key without pasting it into the chat, adapt the code, and run the tests,
   - help you edit a short test clip first.

If you prefer to do it by hand on a Mac with Apple Silicon:

1. `brew install ffmpeg jq git python@3.13` and `xcode-select --install`.
2. `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`
3. `git clone https://github.com/browser-use/video-use vendor/video-use`, then
   `git -C vendor/video-use checkout 43cfc566833548093455c9d4a46bd69913b7e552`.
4. Make an [AssemblyAI](https://www.assemblyai.com) account and copy your API key. Copy
   `.env.example` to `.env`, open `.env` in a text editor and paste the key after
   `ASSEMBLYAI_API_KEY=`. Then run `chmod 600 .env`.
5. Run `claude` here once and accept "trust this folder".
6. Check it: `STUDIO_OFF=1 .venv/bin/python -m pytest rio1/tests -q` should end with all tests passing.

### Your first edit
1. Put your clips in `input/`. Name them in order: `intro.mp4`, `main1.mp4`, `main2.mp4`,
   `outro.mp4` (one clip is fine). Optionally add your script (`.docx`, `.md`, `.txt`).
2. Plug in your laptop's charger.
3. In Claude Code, type **`/rio`**. Rio asks if there's sensitive info on screen, shows the
   forecast and starts. Or run `./edit.sh` in a terminal and leave it overnight.
4. Find the video in `output/` and the report in `Reports/`. The report lists timecodes to check.
   Want to keep editing by hand? Import `output/<name>_edited.xml` into Premiere Pro (keep the raw
   clips in `runs/` where they are, or Premiere asks you to relink them).
5. To fix spots: `/rio revise`, then type notes like `4:05 - cut this tangent`.

## Make it yours

| What | Where |
|---|---|
| Editing style (what to cut, what to keep, pacing) | `brief.md` |
| Product and tool names your transcripts mishear | `glossary.md` |
| Rules learned from your feedback | `history/LESSONS.md` (move lines from Proposed to Approved) |
| Pause lengths, voice filter, loudness, blur settings, output size and encoder | `rio1/profiles/tutorial.json` (`rio2/profiles/tutorial.json` for Rio 2.0) |
| Words and folder names to always blur | `blur.private_folders` in the profile, or `--text` on the blur step |
| Camera layout, backgrounds (Rio 2.0) | the `layout` block in `rio2/profiles/tutorial.json`, `assets/` (add your own `reference.png`) |
| The agents' instructions | `.claude/agents/*.md` |

## Security notes

- Your AssemblyAI key lives only in `.env` (git-ignored, `chmod 600`). Claude Code is denied
  reading it by `.claude/settings.json`.
- Overnight runs are locked down: `claude -p --permission-mode dontAsk` with an allow-list
  (`rio*/config/run-settings.json`, pinned by a hash in `rio*/helpers/step.py`). It can't `curl`,
  `git push`, `rm -rf` or install software.
- Your footage is uploaded to AssemblyAI for transcription (audio only, a small speech file).
  Nothing else leaves your machine except the Claude conversation itself.
- **The auto-blur is a safety net, not a guarantee.** Always watch the parts of a video where keys
  or private data were on screen before you publish. Better still, rotate any key that appeared on
  screen.

## Credits

- [video-use](https://github.com/browser-use/video-use) by browser-use: Rio uses its helpers for
  transcript packing, timeline views and render concat.
- [AssemblyAI](https://www.assemblyai.com) for transcription, [FFmpeg](https://ffmpeg.org) for all
  media work, Apple's Vision framework for reading the screen on macOS.
- [Claude Code](https://docs.claude.com/en/docs/claude-code) by Anthropic runs Rio and its agents.
- Designed and built with Claude Code.

## Licence

MIT. See [LICENSE](LICENSE). You may use, copy, change and share Rio, including commercially, as
long as the copyright notice stays in. video-use, AssemblyAI, FFmpeg and Claude Code keep their own
licences and terms.
