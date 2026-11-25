# SETUP.md: first-run setup (instructions for Claude Code)

> **Claude, you are reading this because `CLAUDE.md` told you to.** Do everything in this file
> before you edit a video. The person you are helping may not be a developer: explain each step in
> plain words, one or two lines at a time, and ask before you install anything or change any code.
> When setup is finished, update the **Machine setup** section at the top of `CLAUDE.md` (step 7), so
> the next session skips this file.

Rio was built and is used every day on an **Apple Silicon Mac (M3, 8 GB RAM, macOS)**. On that kind
of machine almost nothing needs to change. Everything else (Intel Macs, Windows, Linux) needs
some code adapted, and this file tells you exactly where.

---

## Step 1. Say hello and explain the plan

Tell the person, in 3-4 short lines:
- you'll check their computer first (memory, processor, graphics, disk, operating system, tools),
- then show what Rio needs to change for their machine,
- nothing is installed or changed until they say yes,
- the first edit afterwards should be a short test clip (2-5 minutes), not a real video.

## Step 2. Audit the machine (read-only)

Run the commands for their system. Run each one on its own. Report what you find, never guess.

### macOS
```bash
sw_vers                                   # macOS version
uname -m                                  # arm64 = Apple Silicon, x86_64 = Intel
sysctl -n machdep.cpu.brand_string        # processor
sysctl -n hw.ncpu                         # cores
sysctl -n hw.memsize                      # RAM in bytes (divide by 1073741824 for GB)
system_profiler SPDisplaysDataType | grep -E "Chipset Model|Metal"   # graphics
df -h .                                   # free disk space here
pmset -g batt | head -1                   # on charger or battery
```

### Windows (PowerShell)
```powershell
Get-ComputerInfo -Property OsName,OsVersion,CsProcessors,CsTotalPhysicalMemory
Get-CimInstance Win32_Processor | Select-Object Name,NumberOfCores,NumberOfLogicalProcessors
Get-CimInstance Win32_VideoController | Select-Object Name,AdapterRAM,DriverVersion
Get-PSDrive -PSProvider FileSystem
wsl --status          # is WSL2 installed? (recommended way to run Rio on Windows, see step 5)
```

### Linux
```bash
cat /etc/os-release | head -3
uname -m
lscpu | grep -E "Model name|^CPU\(s\)"
free -g
lspci | grep -iE "vga|3d|display"
nvidia-smi --query-gpu=name,driver_version --format=csv 2>/dev/null
df -h .
```

### Tools (every system)
Check each and note the version or "missing":
```bash
python3 --version          # needs 3.11 or newer (built on 3.13)
ffmpeg -version | head -1  # needs a recent build (built on 8.x)
ffprobe -version | head -1
jq --version
git --version
claude --version           # Claude Code CLI
swiftc --version           # macOS only: the screen reader for the blur scan
```
Then list which **video encoders** this ffmpeg has (this decides how the final 4K video is made):
```bash
ffmpeg -hide_banner -encoders | grep -E "hevc|265"
```
Look for: `hevc_videotoolbox` (Mac), `hevc_nvenc` (NVIDIA), `hevc_qsv` (Intel graphics),
`hevc_amf` (AMD), `hevc_vaapi` (Linux, Intel/AMD), `libx265` (software: works everywhere, slow).

## Step 3. Show the report

Show a short table: what they have, what Rio needs, and the verdict. Use these rules:

| Check | Rio needs | If it falls short |
|---|---|---|
| RAM | 8 GB (Rio stays under about 2 GB extra) | Under 8 GB: warn that long videos may swap and get slow; suggest closing apps during a run and keeping clips under 30 minutes. 16 GB or more: no change needed. |
| Processor | Any 4+ core chip from the last ~6 years | Older chips: everything works, but the render and the blur scan take longer. |
| Hardware video encoder | One of `hevc_videotoolbox`, `hevc_nvenc`, `hevc_qsv`, `hevc_amf`, `hevc_vaapi` | None: use `libx265` (software). Warn that a 25-minute 4K video can take several hours to encode, or offer 1080p output instead (`render.output.size: null` in the profile keeps the recording's own size). |
| Disk | About 3x the size of the raw footage, plus 20 GB | Less: warn before the first real run. Old runs in `runs/` can be deleted. |
| Charger | Overnight runs refuse to start on battery (laptops) | Desktops: the battery check must be adapted (step 6). |
| Python 3.11+, ffmpeg, ffprobe, jq, git, Claude Code | All required | Offer to install them in step 5. |

Then show **what has to change for their system** (from the table in step 6), in plain words.
For example: "Your PC has an NVIDIA card, so Rio will use it to make the final video. The blur scan
uses a screen reader that only exists on Macs, so I'll swap in Tesseract, a free text reader."

### The Windows notice (always show it on Windows)
> **Heads up:** Rio was built and tested on a Mac. Windows support is new and hasn't been tested
> nearly as much. The editing itself (transcripts, cuts, the AI editors) is the same everywhere, but
> the screen reader for blurring, the video encoder and a few system checks are swapped out on
> Windows. Check your first few videos carefully, especially the blurs, before you publish them.

Show a shorter version on Linux and Intel Macs ("less tested than Apple Silicon Macs").

## Step 4. Ask before changing anything

Use the AskUserQuestion tool: "Ready for me to set Rio up for this computer? I'll install the
missing tools and adapt the code listed above." Options: **Yes, set it up** / **Show me the
changes first** / **Not now**. Do nothing more until they answer. With "Show me the changes
first", list every file you'll touch (from step 6) and ask again.

## Step 5. Install the basics

Ask before each install. Prefer the system's normal package manager.

**macOS**
1. Homebrew (https://brew.sh) if missing, then `brew install ffmpeg jq git python@3.13`.
2. Xcode Command Line Tools for `swiftc`: `xcode-select --install`.

**Windows: use WSL2 (recommended).** Rio's scripts are bash and Unix tools. Running Rio inside WSL2
(Ubuntu) means most of it works unchanged, and only the Linux adaptations in step 6 apply.
1. `wsl --install -d Ubuntu` in an admin PowerShell, then restart.
2. Inside Ubuntu: `sudo apt update && sudo apt install -y ffmpeg jq git python3 python3-venv tesseract-ocr`.
3. Install Claude Code inside WSL (https://docs.claude.com/en/docs/claude-code) and keep the
   project **inside the Linux file system** (for example `~/rio-video-editor`), not on `C:\`.
   Files on `C:\` are much slower from WSL.
4. NVIDIA cards: install the normal Windows NVIDIA driver (it already supports WSL), then check
   `nvidia-smi` and `ffmpeg -encoders | grep nvenc` inside Ubuntu. If the Ubuntu ffmpeg has no
   `hevc_nvenc`, install a build that does (for example a static build from https://johnvansickle.com/ffmpeg/
   or https://github.com/BtbN/FFmpeg-Builds).
5. If the person refuses WSL, Rio can run natively on Windows, but it needs a much larger port
   (every `edit.sh` call, every `.venv/bin/python` path in `.claude/settings.json`, both
   `rio*/config/run-settings.json` files and the skills becomes `.venv\Scripts\python.exe`, and bash
   needs Git Bash). Say plainly that this is a big change, and recommend WSL2.

**Linux**: `sudo apt install -y ffmpeg jq git python3 python3-venv tesseract-ocr` (or the
distribution's equivalents).

**Every system**, from the project folder:
```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
git clone https://github.com/browser-use/video-use vendor/video-use
git -C vendor/video-use checkout 43cfc566833548093455c9d4a46bd69913b7e552   # the version Rio was built on
```
Then the AssemblyAI key (https://www.assemblyai.com, the account dashboard has the key). The
person types it themselves. **Never ask them to paste it into the chat:**
```bash
cp .env.example .env
chmod 600 .env
# ask them to open .env in a text editor and paste the key after ASSEMBLYAI_API_KEY=
```
Finally, run `claude` once in this folder and accept the "trust this folder" question, or the
overnight runs can't use the project's permission rules.

## Step 6. Adapt the code to this machine

**Rules while you change code** (these are the project's own rules, keep them):
- Test first. For each change, write or adjust a test, run it and see it fail, make the change, run
  it again and see it pass. Show the person the red and green runs.
- Make every change in **both** `rio1/` and `rio2/` (they are two copies of the same system; Rio 2.0
  adds a two-camera mode).
- Keep behaviour identical: same file formats, same outputs, same JSON shapes. Only the
  machine-specific piece changes.
- `rio1/config/run-settings.json` and `rio2/config/run-settings.json` are pinned by a hash in
  `rio1/helpers/step.py` and `rio2/helpers/step.py` (`RUN_SETTINGS_SHA256`). If you edit a
  run-settings file, update that hash, or every overnight run is refused.

### What changes, by system

| Piece | Where | Apple Silicon Mac | Intel Mac | Linux / Windows (WSL2) |
|---|---|---|---|---|
| Final video encoder | `rio1/profiles/tutorial.json` `render.output.encoder` (+ `quality`), `rio1/helpers/media/render_tutorial.py` (the `_videotoolbox` branch). Rio 2.0 also `rio2/helpers/media/composite.py` (`layout.encoder`, `layout.quality`) | none | Keep `hevc_videotoolbox` if listed (2016+ Macs); older: `libx265` | `hevc_nvenc` (NVIDIA), `hevc_qsv` (Intel), `hevc_amf` (AMD on native Windows), `hevc_vaapi` (Linux AMD/Intel), else `libx265`. `libx265` already works with no code change (the non-videotoolbox branch of `encoder_args()` passes `-preset`/`-crf` and tags `hvc1`): just set the encoder. Hardware encoders need their own branch in `encoder_args()`, because they don't take `-crf`: e.g. nvenc `-rc vbr -cq 22 -preset p5`, qsv `-global_quality 22`, vaapi `-qp 22`. Keep `-tag:v hvc1`. A/B one short clip by eye against a Mac render if possible. |
| Hardware decoding | `rio2/helpers/media/composite.py` and `rio2/helpers/media/face_track.py` (`-hwaccel videotoolbox`); `detect_screen_activity.py` `hwaccel` (already `none`) | none | Usually fine; drop it if decoding fails | Remove `-hwaccel videotoolbox` or use `-hwaccel cuda` / `qsv` / `vaapi`. Software decoding is always safe. |
| Screen reader (blur scan) | `rio*/helpers/media/vision.py` (`_reader()`), `rio*/helpers/media/vision.swift` | none (needs `swiftc` from Xcode tools) | Same as Apple Silicon (Vision exists on macOS 11+) | Write `rio*/helpers/media/vision_portable.py` with the **same command line and output** as `vision.swift` (see the contract below), using Tesseract (`pytesseract`, word-level `image_to_data`) or, on native Windows, Windows' built-in OCR (`winocr`). Make `_reader()` pick it when `sys.platform != "darwin"`. |
| Face tracking (Rio 2.0 camera mode only) | `rio2/helpers/media/face_track.py` + `vision.swift faces` | none | same | Same portable reader, `faces` mode, with OpenCV's face detector (`opencv-python`, `cv2.FaceDetectorYN` or a Haar cascade). `yaw` can be `0` if the detector gives none. |
| Keep the computer awake | `rio*/edit.sh` (`caffeinate -i`), `rio*/helpers/step.py` (`caffeinate` is an allowed wrapper) | none | none | Linux: `systemd-inhibit --what=idle:sleep`. WSL2: keep Windows awake with Power settings or PowerToys Awake, and drop the wrapper. Update the allow-list in `step.py` to match. |
| Charger check | `rio*/edit.sh` (`pmset -g batt`) | none | none | Laptops: `cat /sys/class/power_supply/*/online` (Linux) or Python `psutil.sensors_battery()`. Desktops and WSL: skip the check, with a note. |
| Memory monitor | `rio*/helpers/monitor.py` (`vm_stat`, `sysctl vm.swapusage`) | none | none | Read `/proc/meminfo` (`MemTotal - MemAvailable`, `SwapTotal - SwapFree`) or use `psutil.virtual_memory()` / `psutil.swap_memory()`. Keep the same output fields (`used_mb`, `swap_used_mb`). |
| Peak memory per step | `rio*/helpers/step.py` (`/usr/bin/time -l`, `parse_max_rss_mb`) | none | none | Linux: `/usr/bin/time -v` reports "Maximum resident set size (kbytes)" in **kilobytes** (macOS reports bytes). Change the flag, the regex and the unit. Install `time` if missing (`apt install time`). |
| Free disk check | `rio*/edit.sh` (`df -k`) | none | none | Works on Linux/WSL as is. |
| Studio open | `.claude/skills/studio/scripts/studio.py open` | none | none | Already prints the page path off Mac. Optionally use `xdg-open` (Linux) or `explorer.exe "$(wslpath -w <page>)"` (WSL). |
| Studio lock | `studio.py` (`fcntl`) | none | none | Works on Linux/WSL; on native Windows it's skipped safely. |
| Python path | `.claude/settings.json`, `rio*/config/run-settings.json`, skills | none | none | WSL/Linux: unchanged (`.venv/bin/python`). Native Windows only: `.venv\Scripts\python.exe` everywhere. |

### Contract for a replacement screen reader (`vision_portable.py`)
- Command: `<reader> text <list.txt>` or `<reader> faces <list.txt>`. `list.txt` has one image path
  per line.
- Output: **one JSON line per image** on stdout, in any order:
  - text: `{"p": "<path>", "text": [[x, y, w, h, "word"], ...]}`, **one box per word**
  - faces: `{"p": "<path>", "faces": [[x, y, w, h, confidence, yaw], ...]}`
  - unreadable image: `{"p": "<path>", "error": "unreadable"}`
- Boxes are in the image's **pixels**, **top-left origin**, rounded to whole numbers.
- Exit code 0 on success; on failure, a message on stderr and a non-zero exit.
- `vision.py` runs 3 readers at once (`VISION_JOBS`). On 8 GB machines keep 3 for Tesseract; drop
  to 2 if memory runs short.
- Tesseract reads small screen text worse than Apple's reader. Recommend reading at 2x scale and
  test it on a real frame with an API key on it: the blur tests in `rio*/tests/media/` describe what
  must be caught (`test_sensitive_blur.py`, `test_blur_judge.py`, `test_blur_render.py`).

### Tests that only make sense on a Mac
Some tests call the real `hevc_videotoolbox` encoder or `vision.swift`. On other systems, make them
use the adapted piece (preferred), or mark them
`@pytest.mark.skipif(sys.platform != "darwin", reason="macOS-only")` and add an equivalent test for
the replacement. Both suites must end green:
```bash
STUDIO_OFF=1 .venv/bin/python -m pytest rio1/tests -q
STUDIO_OFF=1 .venv/bin/python -m pytest rio2/tests -q
```
(On the reference Mac: rio1 460 passed / 1 skipped, rio2 534 passed / 1 skipped.)

## Step 7. Prove it works, then record it

1. Ask the person for a short screen recording (2-5 minutes, talking over something with a fake
   API key on screen) and put it in `input/`.
2. `./edit.sh --estimate --sensitive yes`: the forecast. It starts from built-in values and
   learns from each run on this machine (`history/runs.jsonl`).
3. Type `/rio` in Claude Code and run the whole edit live. Check: the video plays, no clipped words,
   the fake key is blurred, `Reports/` has the report.
4. Update the **Machine setup** section at the top of `CLAUDE.md`. Replace the "NOT DONE" text
   with: the date, the OS and chip, RAM, the encoder chosen, the screen reader used, anything
   skipped (for example "charger check off: desktop"), and the test results. Then tell the person
   they're ready, and that the first few real videos deserve a careful watch.
