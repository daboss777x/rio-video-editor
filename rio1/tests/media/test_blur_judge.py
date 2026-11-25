"""Smarter blur (2026-11-21): the rules' blurs and near misses become a short list of unique on-screen
texts (blur_find.candidates) that Bea, the blur judge, labels blur/keep; apply_labels turns her labels
into the final E/blurs.json, always with the reader's exact boxes."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "helpers" / "media"))
import blur_find as B  # noqa: E402


def blur(said, kind, start, rect=(100, 100, 200, 20), auto=True):
    b = {"source": "main1", "start": start, "end": start + 2.0, "rect": list(rect), "text": kind, "said": said}
    return {**b, "auto": True} if auto else b


def edit(tmp_path, blurs, hints=()):
    E = tmp_path / "edit"
    E.mkdir()
    (E / "blurs.json").write_text(json.dumps({"version": 1, "blurs": blurs}))
    (E / "blur_hints.json").write_text(json.dumps({"version": 1, "hints": list(hints)}))
    return E


def test_candidates_are_the_unique_texts_the_rules_blurred_and_the_near_misses(tmp_path):
    E = edit(tmp_path, [blur("sk-proj-Ab3dE5fG7hJ9kL1mN3pQ5rS7", "secret", 10.0),
                        blur("sk-proj-Ab3dE5fG7hJ9kL1mN3pQ5rS7", "secret", 50.0, (300, 400, 200, 20)),
                        blur("SUPABASE_SERVICE_ROLE_KEY", "key_value", 20.0),
                        blur("hand blur", "Barbara", 5.0, auto=False)],
             [blur("password: hunter2-staging", "hint", 30.0)])
    cands = B.candidates(E)
    got = [(c["id"], c["said"], c["rule"], c["seen"]) for c in cands]
    assert got == [("c1", "sk-proj-Ab3dE5fG7hJ9kL1mN3pQ5rS7", "secret", 2),
                   ("c2", "SUPABASE_SERVICE_ROLE_KEY", "key_value", 1),
                   ("c3", "password: hunter2-staging", "near miss", 1)]
    md = (E / "blur_candidates.md").read_text()
    assert "c3" in md and "0:30" in md and "hand blur" not in md      # hand blurs are never judged


def test_labels_remove_kept_texts_add_judged_near_misses_and_leave_the_rest(tmp_path):
    key = blur("sk-proj-Ab3dE5fG7hJ9kL1mN3pQ5rS7", "secret", 10.0)
    field = blur("SUPABASE_SERVICE_ROLE_KEY", "key_value", 20.0)
    hand = blur("hand blur", "Barbara", 5.0, auto=False)
    miss = blur("password: hunter2-staging", "hint", 30.0, (500, 600, 220, 18))
    E = edit(tmp_path, [key, field, hand], [miss])
    B.candidates(E)
    (E / "blur_labels.json").write_text(json.dumps({"c2": {"call": "keep", "why": "field name"},
                                                    "c3": {"call": "blur", "why": "a real password"}}))
    out = B.apply_labels(E)
    blurs = json.loads((E / "blurs.json").read_text())["blurs"]
    assert key in blurs and hand in blurs and field not in blurs
    added = [b for b in blurs if b.get("said") == miss["said"]]
    assert len(added) == 1 and added[0]["rect"] == [500, 600, 220, 18] and added[0]["auto"]
    assert out == {"removed": 1, "added": 1}


def test_applying_the_labels_again_changes_nothing(tmp_path):
    E = edit(tmp_path, [blur("SUPABASE_SERVICE_ROLE_KEY", "key_value", 20.0),
                        blur("sk-proj-Ab3dE5fG7hJ9kL1mN3pQ5rS7", "secret", 30.0)],
             [blur("password: hunter2-staging", "hint", 40.0)])
    B.candidates(E)
    (E / "blur_labels.json").write_text(json.dumps({"c1": {"call": "keep"}, "c3": {"call": "blur"}}))
    B.apply_labels(E)
    once = json.loads((E / "blurs.json").read_text())
    assert B.apply_labels(E) == {"removed": 0, "added": 0}
    assert json.loads((E / "blurs.json").read_text())["blurs"] == once["blurs"]


def test_the_scan_records_what_each_blur_says_and_the_near_misses(tmp_path, monkeypatch):
    import subprocess
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=gray:s=1512x982:r=30:d=2",
                    "-pix_fmt", "yuv420p", str(tmp_path / "main1.mp4")], check=True)
    E = tmp_path / "edit"
    E.mkdir()
    (E / "edl.json").write_text(json.dumps({"sources": {"main1": "../main1.mp4"},
                                            "ranges": [{"source": "main1", "start": 0.0, "end": 2.0}]}))
    frame = [[100, 300, 520, 16, "OPENAI_API_KEY=sk-proj-Ab3dE5fG7hJ9kL1mN3pQ5rS7tU9vW1xY3z"],
             [100, 340, 120, 16, "password:"], [225, 340, 150, 16, "hunter2-staging"],
             [100, 380, 300, 16, "Publishable keys can be shared"]]
    monkeypatch.setattr(B, "run_vision", lambda m, imgs, w: {str(p): {"text": frame} for p in imgs})
    blurs = B.find(E, [], step=1.0, sensitive=True)["blurs"]
    assert blurs and all(b["said"] == frame[0][4] for b in blurs)
    hints = json.loads((E / "blur_hints.json").read_text())["hints"]
    assert [h["said"] for h in hints] == ["password: hunter2-staging"]
    assert hints[0]["rect"][0] <= 100 and hints[0]["rect"][0] + hints[0]["rect"][2] >= 375


def test_the_eye_check_sheet_shows_each_judged_blur_once_with_its_id(tmp_path):
    import subprocess
    from PIL import Image
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=1512x982:r=30:d=12",
                    "-pix_fmt", "yuv420p", str(tmp_path / "main1.mp4")], check=True)
    E = edit(tmp_path, [blur("sk-proj-Ab3dE5fG7hJ9kL1mN3pQ5rS7", "secret", 1.0, (400, 300, 300, 20)),
                        blur("sk-proj-Ab3dE5fG7hJ9kL1mN3pQ5rS7", "secret", 6.0, (400, 300, 300, 20)),
                        blur("x@y.co", "email", 8.0, (100, 700, 120, 18))])
    (E / "edl.json").write_text(json.dumps({"sources": {"main1": "../main1.mp4"},
                                            "ranges": [{"source": "main1", "start": 0.0, "end": 12.0}]}))
    sheets = B.sheet(E)
    assert len(sheets) == 1 and sheets[0].suffix == ".png"
    im = Image.open(sheets[0])
    assert im.width >= 900 and im.height >= 150
    assert "c1" in (E / "blur_sheet.md").read_text() and "c2" in (E / "blur_sheet.md").read_text()


def test_cli_lists_candidates_then_applies_the_labels(tmp_path, capsys):
    E = edit(tmp_path, [blur("SUPABASE_SERVICE_ROLE_KEY", "key_value", 20.0)])
    assert B.main(["--edit-dir", str(E), "--candidates"]) == 0
    assert "1 candidate" in capsys.readouterr().out
    (E / "blur_labels.json").write_text(json.dumps({"c1": {"call": "keep", "why": "field name"}}))
    assert B.main(["--edit-dir", str(E), "--apply-labels"]) == 0
    assert "removed 1" in capsys.readouterr().out
    assert json.loads((E / "blurs.json").read_text())["blurs"] == []


def test_the_blur_check_does_not_put_back_a_blur_bea_removed(tmp_path, monkeypatch):
    import subprocess
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=gray:s=1512x982:r=30:d=2",
                    "-pix_fmt", "yuv420p", str(tmp_path / "main1.mp4")], check=True)
    line = "SUPABASE_PROJECT_REF=abcdwxyzqrstuvklmnop"          # Bea: a placeholder in the plan doc
    E = edit(tmp_path, [blur(line, "key_value", 0.0, (300, 300, 200, 16))])
    (E / "edl.json").write_text(json.dumps({"sources": {"main1": "../main1.mp4"},
                                            "ranges": [{"source": "main1", "start": 0.0, "end": 2.0}]}))
    B.candidates(E)
    (E / "blur_labels.json").write_text(json.dumps({"c1": {"call": "keep", "why": "placeholder"}}))
    B.apply_labels(E)
    monkeypatch.setattr(B, "run_vision", lambda m, imgs, w: {str(p): {"text": [[100, 300, 400, 16, line]]} for p in imgs})
    assert B.check(E, step=1.0, fix=True) == []
