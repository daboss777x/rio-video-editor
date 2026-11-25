#!/usr/bin/env python3
"""Transcribe one clip with AssemblyAI into E/transcripts/<stem>.json (Scribe shape).

Same contract as vendor transcribe.py: skips if the file exists (cache), exits non-zero
with a one-line reason on failure. Key: ASSEMBLYAI_API_KEY from <project>/.env (never printed).
"""
import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from helpers.stt import providers  # noqa: E402

# Price used for cost estimates (USD per source hour). Profile key scribe_usd_per_hour should match.
ASSEMBLYAI_USD_PER_HOUR = 0.37


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("video")
    ap.add_argument("--edit-dir", required=True)
    a = ap.parse_args(argv)
    video = Path(a.video)
    out = Path(a.edit_dir) / "transcripts" / (video.stem + ".json")
    if out.exists():
        print("cache hit: %s" % out)
        return 0
    if not video.is_file():
        print("transcribe failed: video not found: %s" % video.name, file=sys.stderr)
        return 2
    if not providers.has_key("ASSEMBLYAI_API_KEY"):
        print("transcribe failed: ASSEMBLYAI_API_KEY not set in .env", file=sys.stderr)
        return 2
    state = out.parent / (video.stem + ".assemblyai.json")  # resume point; never holds the key
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory() as td:
            audio = Path(td) / "audio.ogg"
            if not state.exists():  # resuming needs no new upload
                providers.extract_speech_audio(video, audio)
            data = providers.transcribe_assemblyai(audio, state_path=state)
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data))
        os.replace(tmp, out)
        state.unlink(missing_ok=True)
    except Exception as ex:  # one line, never the key
        print("transcribe failed: %s: %s" % (type(ex).__name__, str(ex).splitlines()[0][:200] if str(ex) else ""),
              file=sys.stderr)
        return 1
    print("wrote %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
