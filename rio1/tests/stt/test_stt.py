import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "helpers" / "stt"))
import compare as C  # noqa: E402
import providers as P  # noqa: E402

AAI = {"language_code": "en", "words": [
    {"text": "Um,", "start": 500, "end": 800, "speaker": "A"},
    {"text": "hello", "start": 900, "end": 1200, "speaker": "A"},
    {"text": "world.", "start": 1250, "end": 1700, "speaker": "A"}]}
DG = {"results": {"channels": [{"alternatives": [{"words": [
    {"word": "uh", "punctuated_word": "Uh,", "start": 0.5, "end": 0.7},
    {"word": "hello", "punctuated_word": "hello", "start": 0.92, "end": 1.2},
    {"word": "world", "punctuated_word": "world.", "start": 1.25, "end": 1.7}]}]}]}}


def test_assemblyai_shape():
    t = P.assemblyai_to_scribe(AAI)
    ws = [w for w in t["words"] if w["type"] == "word"]
    assert [w["text"] for w in ws] == ["Um,", "hello", "world."]
    assert ws[0]["start"] == 0.5 and ws[0]["end"] == 0.8 and isinstance(ws[0]["start"], float)
    assert any(w["type"] == "spacing" for w in t["words"])
    assert all("speaker_id" in w for w in t["words"])


def test_deepgram_shape():
    t = P.deepgram_to_scribe(DG)
    ws = [w for w in t["words"] if w["type"] == "word"]
    assert [w["text"] for w in ws] == ["Uh,", "hello", "world."]
    assert ws[1]["start"] == 0.92


def test_filler_count():
    assert C.count_fillers(P.assemblyai_to_scribe(AAI)) == 1
    assert C.count_fillers(P.deepgram_to_scribe(DG)) == 1


def test_alignment_metrics():
    m = C.align_metrics(P.assemblyai_to_scribe(AAI), P.deepgram_to_scribe(DG))
    assert m["aligned"] == 2  # "um" vs "uh" differ; hello, world match
    assert abs(m["start_med_ms"] - 10) < 1e-6  # diffs 20 and 0 -> median 10
    assert m["start_p90_ms"] >= m["start_med_ms"]


def test_filler_diffs_and_cost():
    ref = P.assemblyai_to_scribe(AAI)
    hyp = P.deepgram_to_scribe({"results": {"channels": [{"alternatives": [{"words": [
        {"word": "hello", "start": 0.9, "end": 1.2}]}]}]}})
    d = C.filler_diffs(ref, hyp)
    assert d and "missed" in d[0][1]
    assert abs(C.cost("assemblyai", 60) - 0.15) < 1e-9


def test_report_renders():
    ref = P.assemblyai_to_scribe(AAI)
    tr = P.deepgram_to_scribe(DG)
    rep = C.build_report(ref, {"deepgram": {"transcript": tr, "wall_s": 2.0, "metrics": C.align_metrics(ref, tr)},
                               "assemblyai": {"skipped": "ASSEMBLYAI_API_KEY not set"}}, 3.0)
    assert "Summary" in rep and "deepgram" in rep and "Skipped" in rep


def test_transcribe_cli_cache_and_missing_key(tmp_path, monkeypatch):
    import json
    from helpers.stt import transcribe as T
    vid = tmp_path / "clip1.mp4"
    vid.write_bytes(b"x")
    e = tmp_path / "E"
    (e / "transcripts").mkdir(parents=True)
    (e / "transcripts" / "clip1.json").write_text(json.dumps({"words": []}))
    assert T.main([str(vid), "--edit-dir", str(e)]) == 0          # cache hit, no key needed
    (e / "transcripts" / "clip1.json").unlink()
    monkeypatch.setattr(T.providers, "has_key", lambda n: False)
    assert T.main([str(vid), "--edit-dir", str(e)]) == 2          # one-line failure
    monkeypatch.setattr(T.providers, "has_key", lambda n: True)
    monkeypatch.setattr(T.providers, "extract_speech_audio", lambda v, o: o)
    monkeypatch.setattr(T.providers, "transcribe_assemblyai", lambda p, state_path=None: {"words": [1]})
    assert T.main([str(vid), "--edit-dir", str(e)]) == 0
    assert json.loads((e / "transcripts" / "clip1.json").read_text()) == {"words": [1]}


def test_assemblyai_resumes_from_saved_id_and_retries_network_errors(tmp_path, monkeypatch):
    import json
    import requests
    from helpers.stt import providers as P
    monkeypatch.setattr(P, "load_env_key", lambda n: "k")
    monkeypatch.setattr(P.time, "sleep", lambda s: None)
    state = tmp_path / "s.json"
    state.write_text(json.dumps({"upload_url": "u", "id": "t1"}))  # a previous run got this far
    calls = {"get": 0}

    class R:
        def __init__(self, j): self._j = j
        def raise_for_status(self): pass
        def json(self): return self._j

    def get(url, **kw):
        calls["get"] += 1
        if calls["get"] == 1:
            raise requests.ConnectionError("wifi")
        return R({"status": "completed", "words": [{"text": "hi", "start": 0, "end": 300}]})

    def post(*a, **kw):
        raise AssertionError("must not upload or submit again")
    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(requests, "post", post)
    out = P.transcribe_assemblyai(tmp_path / "missing.ogg", state_path=state)
    assert [w["text"] for w in out["words"] if w["type"] == "word"] == ["hi"] and calls["get"] == 2
