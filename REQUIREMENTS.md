# Requirements

Everything Rio needs, what each piece is for, and roughly what it costs. `SETUP.md` checks and
installs most of this for you.

## Computer

| | Reference machine (built and tested on) | Minimum | Notes |
|---|---|---|---|
| Operating system | macOS on Apple Silicon (M3) | macOS 11+, Linux, or Windows 10/11 with WSL2 | Windows and Linux need the adaptations in `SETUP.md` step 6 and are less tested. |
| Memory (RAM) | 8 GB | 8 GB | Rio stays under about 2 GB extra and runs one step at a time. |
| Processor | Apple M3 | Any recent 4+ core chip | Slower chips mean longer renders and blur scans. |
| Video encoder | `hevc_videotoolbox` (Apple hardware) | Any HEVC encoder; `libx265` (software) works everywhere | Software encoding of 4K is several times slower. |
| Disk | | About 3x the raw footage, plus 20 GB | Each run keeps its working files in `runs/`; old runs can be deleted. |
| Power | On the charger | | Overnight runs refuse to start on battery. |

## Accounts and services

| Service | What it's for | Plan | Sign up |
|---|---|---|---|
| Claude (Anthropic) | Runs Claude Code: Rio, Rosa, Ines and Bea | A paid plan (Pro or above). Rosa and Ines use Opus, Rio and Bea use Sonnet. | https://claude.ai |
| AssemblyAI | Transcribes the audio (with fillers) and listens back to the finished video | Pay as you go | https://www.assemblyai.com |
| GitHub (optional) | Downloading video-use and this project | Free | https://github.com |

## Local tools

| Tool | Version it was built on | Used for | Install |
|---|---|---|---|
| Claude Code CLI | 2.1.x | Runs everything | https://docs.claude.com/en/docs/claude-code |
| Python | 3.13 (3.11+ should work) | All helpers | `brew install python@3.13` / `apt install python3 python3-venv` |
| FFmpeg + ffprobe | 8.1 | Probing, silences, cutting, encoding, loudness | `brew install ffmpeg` / `apt install ffmpeg` |
| jq | 1.7+ | The orchestrator reads small JSON summaries with it | `brew install jq` / `apt install jq` |
| git | any | Getting video-use | `brew install git` / `apt install git` |
| Xcode Command Line Tools (`swiftc`) | macOS only | Compiles the screen reader for the blur scan and face tracking | `xcode-select --install` |
| Tesseract OCR | non-Mac only | Replacement screen reader (added by `SETUP.md`) | `apt install tesseract-ocr` |
| video-use | commit `43cfc56` (2026-10-08) | Helper scripts (transcript packing, timeline view, render concat) | `git clone https://github.com/browser-use/video-use vendor/video-use` |

## Python packages (`requirements.txt`)

| Package | Version | Used for |
|---|---|---|
| numpy | 2.5.4 | Audio envelopes, comparing frames |
| pillow | 13.0.0 | Stills, contact sheets, blur previews |
| requests | 2.34.2 | Talking to AssemblyAI |
| librosa | 1.0.0 | video-use's timeline view |
| matplotlib | 3.11.2 | video-use's timeline view |
| pytest | 9.2.0 | The test suites |
| pytesseract, opencv-python, psutil | latest | Only on non-Mac systems, added by `SETUP.md` |

## Environment variables (`.env`)

| Variable | Used by | Where to get it |
|---|---|---|
| `ASSEMBLYAI_API_KEY` | `rio*/helpers/stt/transcribe.py`, `rio*/helpers/hear_check.py` | AssemblyAI dashboard, API keys |

Optional: `STUDIO_OFF=1` turns off Studio updates (the tests use it). How many screen readers run at
once is `VISION_JOBS` (default 3) in `rio*/helpers/media/vision.py`.

## Running costs (rough)

- **Transcription:** about $0.21-0.22 per hour of footage, plus about $0.02 per 5 minutes of
  finished video for the listen-back. Around $0.30-0.45 per video. Check AssemblyAI's current prices.
- **Claude:** covered by your Claude subscription. A full edit of an hour of footage used about 2% of
  a Pro plan's 5-hour window on the reference machine. At pay-as-you-go API prices, the same work
  would be very roughly $8-15 per video, more when there are many revision rounds in one chat.
- **Electricity and time:** about an hour of machine time per hour of footage on an M3; longer on
  slower machines or with software encoding.
