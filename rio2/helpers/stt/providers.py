"""Speech-to-text providers returning the Scribe-shaped JSON the pipeline reads.

Keys come from <project>/.env (never printed). The pipeline uploads small mono 16 kHz Opus
(~3 MB per 20 min: the user's uplink is ~80 KB/s with Wi-Fi drops); the bake-off uses FLAC.
"""
import os
import subprocess
import time
from pathlib import Path

# .env is shared at the project root, one level above this system's code (rio1/ or rio2/).
ROOT = Path(__file__).resolve().parents[3]


def load_env_key(name):
    v = os.environ.get(name, "")
    if v:
        return v
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            line = line.strip()
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def has_key(name):
    return bool(load_env_key(name))


def extract_flac(video_path, out_path):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(video_path), "-vn",
                    "-ac", "1", "-ar", "16000", "-c:a", "flac", str(out_path)], check=True)
    return Path(out_path)


def extract_speech_audio(video_path, out_path):
    """Mono 16 kHz Opus at 24 kbps in Ogg: about 10x smaller than FLAC, plenty for speech recognition."""
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(video_path), "-vn", "-ac", "1", "-ar", "16000",
                    "-c:a", "libopus", "-b:a", "24k", "-application", "voip", str(out_path)], check=True)
    return Path(out_path)


def _retry(fn, what, tries=6, waits=(5, 15, 30, 60, 120)):
    """Call fn(); on network errors wait and try again (Wi-Fi drops). HTTP errors are not retried."""
    import requests
    for i in range(tries):
        try:
            return fn()
        except (requests.ConnectionError, requests.Timeout) as ex:
            if i == tries - 1:
                raise
            w = waits[min(i, len(waits) - 1)]
            print("%s: network problem (%s), retrying in %ds" % (what, type(ex).__name__, w), flush=True)
            time.sleep(w)


def to_scribe_shape(items, language_code=None):
    """items: [(text, start_s, end_s, speaker)] -> Scribe-like dict."""
    words = []
    prev_end = None
    for text, s, e, spk in items:
        if not text:
            continue
        if prev_end is not None and s > prev_end:
            words.append({"text": " ", "start": prev_end, "end": float(s), "type": "spacing", "speaker_id": spk})
        words.append({"text": text, "start": float(s), "end": float(e), "type": "word", "speaker_id": spk})
        prev_end = float(e)
    return {"language_code": language_code, "text": " ".join(i[0] for i in items if i[0]), "words": words}


def assemblyai_to_scribe(resp):
    items = [(w["text"], w["start"] / 1000.0, w["end"] / 1000.0,
              ("speaker_" + str(w["speaker"]).lower()) if w.get("speaker") else "speaker_0")
             for w in resp.get("words") or []]
    return to_scribe_shape(items, resp.get("language_code"))


def deepgram_to_scribe(resp):
    ch = resp["results"]["channels"][0]
    alt = ch["alternatives"][0]
    items = [(w.get("punctuated_word") or w["word"], w["start"], w["end"],
              "speaker_%s" % w.get("speaker", 0)) for w in alt.get("words") or []]
    return to_scribe_shape(items, ch.get("detected_language"))


def transcribe_assemblyai(audio_path, poll_s=3, timeout_s=3600, state_path=None):
    """Upload, submit, poll. With state_path, progress (upload_url, then transcript id) is saved
    after each stage, so a rerun after a crash or Wi-Fi drop resumes instead of paying twice."""
    import json as _json
    import requests
    key = load_env_key("ASSEMBLYAI_API_KEY")
    if not key:
        raise RuntimeError("ASSEMBLYAI_API_KEY not set")
    base = "https://api.assemblyai.com/v2"
    h = {"authorization": key}
    state = {}
    if state_path and Path(state_path).exists():
        try:
            state = _json.loads(Path(state_path).read_text())
        except ValueError:
            state = {}

    def save():
        if state_path:
            Path(state_path).write_text(_json.dumps(state))

    if not state.get("id"):
        if not state.get("upload_url"):
            def up():
                with open(audio_path, "rb") as f:
                    r = requests.post(base + "/upload", headers=h, data=f, timeout=(30, 300))
                r.raise_for_status()
                return r.json()["upload_url"]
            state["upload_url"] = _retry(up, "upload")
            save()
        body = {"audio_url": state["upload_url"], "disfluencies": True,
                "punctuate": True, "format_text": True, "language_code": "en"}

        def submit():
            r = requests.post(base + "/transcript", headers=h, json=body, timeout=(30, 60))
            r.raise_for_status()
            return r.json()["id"]
        state["id"] = _retry(submit, "submit")
        save()
    tid = state["id"]
    t0 = time.time()
    while True:
        r = _retry(lambda: requests.get("%s/transcript/%s" % (base, tid), headers=h, timeout=(30, 60)), "poll")
        r.raise_for_status()
        j = r.json()
        if j["status"] == "completed":
            return assemblyai_to_scribe(j)
        if j["status"] == "error":
            raise RuntimeError("AssemblyAI error: %s" % j.get("error"))
        if time.time() - t0 > timeout_s:
            raise TimeoutError("AssemblyAI polling timed out")
        time.sleep(poll_s)


def transcribe_deepgram(audio_path):
    import requests
    key = load_env_key("DEEPGRAM_API_KEY")
    if not key:
        raise RuntimeError("DEEPGRAM_API_KEY not set")
    params = {"model": "nova-3", "filler_words": "true", "punctuate": "true",
              "smart_format": "false", "language": "en"}
    with open(audio_path, "rb") as f:
        r = requests.post("https://api.deepgram.com/v1/listen", params=params, data=f, timeout=900,
                          headers={"Authorization": "Token " + key, "Content-Type": "audio/flac"})
    r.raise_for_status()
    return deepgram_to_scribe(r.json())
