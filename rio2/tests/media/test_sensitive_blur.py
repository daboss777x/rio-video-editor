"""Auto-blur of sensitive on-screen text (blur_find.sensitive_boxes), on OCR words taken from the
real drive-organizer recording (2026-11-17). Items are macOS Vision words: [x, y, w, h, text] in
1512-px-wide frame pixels, origin top-left."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "helpers" / "media"))
import blur_find as B  # noqa: E402

W, H = 1512, 982


def kinds(items, **kw):
    return sorted(k for k, _ in B.sensitive_boxes(items, W, H, **kw))


def test_email_address_is_flagged_with_its_box():
    items = [[125, 734, 140, 18, "REPORT_EMAIL"], [481, 734, 176, 15, "janedo.abc@gmail.com"]]
    assert B.sensitive_boxes(items, W, H) == [("email", [481, 734, 176, 15])]


def test_ids_and_secrets_are_flagged_even_with_ocr_lookalike_letters():
    items = [
        [233, 213, 433, 15, "192u7rD-0cРzpjbТYТХ7fS2BylcSeDs181TvhsJzNqv_IULqyE4609seM"],  # Apps Script ID
        [481, 644, 292, 15, "1KzVUDIf68weQ4IVsmtYLЗIGdxuY9Nfd-"],                       # Drive folder ID
        [481, 822, 321, 20, "It8uwPGzoH67DIP0xN2an-ymp-UozVW00Pb"],                      # SECRET value
        [523, 538, 466, 22, "yfRu92RHXf_GnIKwAm7BВmіА5s5MxyqtFYm0xdJeJkB7b1082PYXL52"],  # wrapped URL tail
    ]
    assert kinds(items) == ["secret"] * 4


def test_id_inside_a_url_is_flagged():
    items = [[474, 51, 478, 18, "https://drive.google.com/drive/folders/1KzVUDIf68weQ4lVsmtYL3IGdxuY9Nfd-"]]
    assert kinds(items) == ["secret"]


def test_ordinary_screen_text_is_left_alone():
    items = [
        [912, 639, 160, 18, "Screenshot_20251217-..."],
        [429, 365, 560, 18, "fanation(tosubjectbodyMailappsendEmailto,subject,body):)"],
        [938, 406, 210, 16, "ss.getSheetByName('Inbox')"],
        [125, 644, 185, 18, "FORBIDDEN_FOLDER_IDS"],
        [541, 620, 68, 13, "KEY=value"],
        [703, 53, 105, 15, "drive.google.com"],
        [100, 700, 300, 18, "Copy of Smith, Alexa G8 2021-22.pdf"],
        [100, 760, 300, 18, "Tax_invoice_#50_for_order_#9R4JN.pdf"],
        [100, 800, 200, 18, "2026-11-17 18:40"], [100, 840, 120, 18, "$1,021.00"],
        [100, 880, 220, 18, "modifiedTime,viewedByMeTime,shortcutDetails"],
    ]
    assert kinds(items) == []


def test_env_style_key_value_lines_are_flagged_but_code_and_help_text_are_not():
    flagged = [[521, 496, 468, 24, "DRIVE_BRIDGE_URL=https.7/script.google.com/macros/s/AKfycbx9Q2mLr7TzP4kWn8Vd"]]
    assert "key_value" in kinds(flagged)
    calm = [[100, 300, 30, 16, "var"], [135, 300, 60, 16, "SAFE_ID"], [200, 300, 10, 16, "="],
            [215, 300, 200, 16, "/^[A-Za-z0-9_-]+$/;"],
            [541, 620, 68, 13, "KEY=value"], [100, 400, 200, 16, "TIMEOUT=30"]]
    assert kinds(calm) == []


def test_phone_numbers_split_into_words_are_flagged_as_one_box():
    items = [[100, 300, 50, 16, "Phone:"], [160, 300, 40, 16, "+254"], [205, 300, 35, 16, "712"],
             [245, 300, 35, 16, "345"], [285, 300, 35, 16, "678"]]
    assert B.sensitive_boxes(items, W, H) == [("phone", [160, 300, 160, 16])]
    for one in ("(555) 123-4567", "0712 345678", "+44 20 7946 0958"):
        assert kinds([[100, 500, 200, 16, one]]) == ["phone"], one


def test_dates_times_prices_and_versions_are_not_phone_numbers():
    for calm in ("2026-11-17", "18:40", "$1,021.00", "v2.1.105", "1,021 / 0 / 1,021 / 0", "Nov 13 at 6:40 PM",
                 "327 tests", "8.24 GB of 15 GB used"):
        assert kinds([[100, 500, 200, 16, calm]]) == [], calm


GMAIL = [[152, 149, 66, 24, "Gmail"], [152, 230, 73, 18, "Compose"], [152, 301, 46, 13, "Inbox"],
         [152, 336, 55, 15, "Starred"], [152, 483, 79, 16, "Purchases"], [152, 625, 88, 17, "Newsletters"],
         [433, 151, 60, 21, "Search"], [495, 152, 37, 20, "mail"], [705, 53, 101, 15, "mail.google.com"],
         [455, 279, 59, 26, "Drive"], [477, 918, 75, 18, "Reply"], [628, 916, 66, 21, "Forward"]]


def test_open_gmail_blurs_the_whole_mail_pane_right_of_the_sidebar():
    # customer names hide in subjects and file names, which no pattern can tell apart from plain text
    got = [b for b in B.sensitive_boxes(GMAIL, W, H) if b[0] == "gmail"]
    assert got == [("gmail", [260, 182, W - 260, H - 182])]   # sidebar edge 240 + 20, below the search bar


def test_a_gmail_tab_or_shortcut_on_another_page_is_not_gmail():
    drive = [[450, 94, 38, 13, "Gmail"], [70, 145, 53, 20, "Drive"], [310, 147, 54, 18, "Search"],
             [530, 330, 33, 11, "Gmail"], [714, 562, 31, 14, "Inbox"]]
    assert kinds(drive) == []


DRIVE_SIDEBAR = [[68, 356, 19, 15, "My"], [89, 356, 37, 16, "Drive"], [310, 147, 54, 18, "Search"]]


def test_inside_a_private_folder_blurs_the_whole_screen():
    inside = DRIVE_SIDEBAR + [[277, 211, 33, 26, "My"], [313, 211, 59, 26, "Drive"],
                              [426, 210, 42, 25, "Red"], [471, 211, 60, 25, "Zone"], [534, 211, 18, 24, "-"]]
    assert B.sensitive_boxes(inside, W, H, private=["Red Zone"]) == [("private", [0, 0, W, H])]


def test_private_folder_tile_seen_from_outside_is_not_blurred():
    outside = DRIVE_SIDEBAR + [[277, 210, 33, 27, "My"], [313, 210, 59, 27, "Drive"],
                               [912, 428, 28, 14, "Red"], [942, 428, 34, 14, "Zone"]]
    assert kinds(outside, private=["Red Zone"]) == []


# ------------------------------------------------------------ frame sampling (2026-11-17)
import subprocess  # noqa: E402

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402


def colour_at(t):  # the synthetic clip: red until 3.3 s, green until 5.6 s, then blue
    return "r" if t < 3.3 else "g" if t < 5.6 else "b"


def colour_of(path):
    px = np.asarray(Image.open(path).convert("RGB"), dtype=float).reshape(-1, 3).mean(0)
    return "rgb"[int(px.argmax())]


def test_sampled_frames_show_their_labelled_time_and_short_shots_get_their_end(tmp_path):
    # the Red Zone visit lasted 2.5 s and was missed: frames ran ~0.5 s late and the end was never read
    clip = tmp_path / "clip.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "nullsrc=s=96x64:r=60:d=8,format=rgb24,"
                    "geq=r='if(lt(T,3.3),255,0)':g='if(between(T,3.3,5.6),255,0)':b='if(gt(T,5.6),255,0)'",
                    "-pix_fmt", "yuv420p", str(clip)], check=True)
    for start, dur in ((2.0, 2.45), (5.0, 1.5), (0.0, 0.4)):
        shots = B.sample_frames(clip, start, dur, 1.0, tmp_path / f"w{start}")
        ts = [t for t, _ in shots]
        assert ts[0] == start and ts == sorted(ts)
        assert ts[-1] >= start + dur - 0.2, (start, ts)                     # the shot's last moment is read
        for t, p in shots:
            assert colour_of(p) == colour_at(t), (start, t)                   # the frame shows its own time


def test_false_alarms_from_the_first_full_run_stay_unblurred():
    # 2026-11-17 trial on the whole video: a sheet name, a camelCase call and a code line were flagged
    calm = [[100, 300, 280, 22, "AIA-Project2MainSheet"],
            [100, 340, 300, 18, ".requireValueInList(choices,"],
            [100, 380, 520, 18, "FOLDER='application/vnd.google-apps.folder';"],
            [100, 420, 560, 18, "SHORTCUT='application/vnd.google-apps.shortcut';"]]
    assert kinds(calm) == []
    still = [[100, 500, 600, 18, '"session_id":"cse_01DA7ZjwjurxmHxTJdeTjokm"'],
             [100, 540, 600, 18, "DRIVE_BRIDGE_SECRET=It8uwPGzoH67DIP0xN2an-ymp-"]]
    assert "secret" in kinds(still[:1]) and kinds(still[1:]) == ["key_value", "secret"]


def test_a_big_region_blur_is_not_padded_to_the_whole_screen():
    # trial run: the Gmail pane came out as [0, 0, 3024, 1964] because padding was half the box height
    out = B.group([("s", "gmail", 10.0, [520, 364, 2504, 1600])], 1.0, {"s": (3024, 1964)})
    assert out[0]["rect"] == [490, 334, 2534, 1630]


def test_private_folder_is_followed_by_its_id_while_a_popup_hides_the_breadcrumb():
    # 1486-1497 s: breadcrumb "My Drive > Red Zone", then the favourites popup hides it for 8 s while the
    # address bar shows the folder's ID (read slightly differently each time by OCR)
    crumb = [[277, 211, 33, 26, "My"], [313, 211, 61, 26, "Drive"], [426, 210, 42, 25, "Red"], [471, 211, 60, 25, "Zone"]]
    popup = [[275, 213, 36, 24, "My"], [314, 213, 56, 25, "Drive"], [508, 211, 58, 22, "Work"], [582, 211, 110, 22, "Favourites"]]
    url = lambda fid: [[457, 41, 495, 34, f"https://drive.google.com/drive/folders/{fid}"]]  # noqa: E731
    frames = [
        crumb,                                               # 0: inside, no URL shown
        popup + url("1K2VUDIf68weQ4IVsmtYL3IGdJY9Nfd-"),     # 1: popup, ID misread
        popup + url("1KzVUDIf68weQ4lVsmtYL3IGdxuY9Nfd-"),     # 2
        crumb + url("1KzVUDIf68weQ4IVsmtYL3IGdxuY9Nfd-"),     # 3: inside again, URL shown
        popup + url("1AbCdEf9876543210ZyXwVuTsRqPoNm-"),     # 4: another folder's URL
    ]
    assert B.private_frames(frames, ["Red Zone"], 982) == {0, 1, 2, 3}


def test_private_folder_spreads_along_the_frames_that_keep_showing_its_id():
    # the real 1486-1498 s order: no frame shows both the breadcrumb and the full ID
    crumb = [[277, 211, 33, 26, "My"], [313, 211, 61, 26, "Drive"], [426, 210, 42, 25, "Red"], [471, 211, 60, 25, "Zone"]]
    popup = [[275, 213, 36, 24, "My"], [314, 213, 56, 25, "Drive"], [582, 211, 110, 22, "Favourites"]]
    url = lambda fid: [[457, 41, 495, 34, f"https://drive.google.com/drive/folders/{fid}"]]  # noqa: E731
    elsewhere = [[100, 300, 200, 18, "Apps"], [140, 300, 200, 18, "Script"]]
    frames = [elsewhere, crumb,
              popup + url("1K2VUDIf68weQ4IVsmtYL3IGdJY9Nfd-"), popup + url("1KzVUDIf68weQ4lVsmtYL3IGdxuY9Nfd-"),
              crumb, elsewhere,
              popup + url("1AbCdEf9876543210ZyXwVuTsRqPoNm-")]   # later: some other folder's URL
    assert B.private_frames(frames, ["Red Zone"], 982) == {1, 2, 3, 4}


# ---- 2026-11-20 blur upgrade (user: blur only the value, keep the field name readable) ----

def _box(items, kind="key_value"):
    return [b for k, b in B.sensitive_boxes(items, W, H) if k == kind]


def test_env_line_blurs_only_the_value_from_its_first_to_its_last_character():
    # one OCR word holding the whole line: 13 chars of "SUPABASE_URL=" + 39 chars of value, 600 px wide
    items = [[100, 300, 600, 16, "SUPABASE_URL=https://abcdwxyzqrstuvklmno.supabase.co"]]
    [box] = _box(items)
    assert abs(box[0] - 250) <= 2 and abs(box[0] + box[2] - 700) <= 2   # value spans x 250..700
    assert box[1] == 300 and box[3] == 16


def test_a_key_inside_a_longer_word_blurs_only_the_key_not_its_label():
    # "TRIGGER_SECRET_KEY=" is 19 chars, the key 34 chars; 530 px wide -> 10 px per char
    items = [[100, 500, 530, 16, "TRIGGER_SECRET_KEY=tr_dev_sk_Qa7Lm2Xc9RtB4wNp8Kd3ZyHj"]]
    boxes = [b for _, b in B.sensitive_boxes(items, W, H)]
    assert boxes, "the key must be blurred"
    for b in boxes:
        assert b[0] >= 100 + 19 * 10 - 2, f"blur {b} covers the label TRIGGER_SECRET_KEY="
        assert abs(b[0] + b[2] - 630) <= 2


def test_env_values_are_blurred_even_lowercase_refs_and_ids_but_harmless_fields_stay():
    lines = {
        "SUPABASE_PROJECT_REF=abcdwxyzqrstuvklmno": True,      # lowercase ref (the user wants it hidden)
        "WHATSAPP_PHONE_NUMBER_ID=1000222033304440": False,     # user 2026-11-21: the phone number ID stays readable
        "AIRTABLE_BASE_ID=appQx7LmNoPqRsTuV": True,
        "WHATSAPP_VERIFY_TOKEN=example-verify-9a8b7c": True,
        "OPENAI_MODEL=gpt-6-luna": False,                       # harmless: model name
        "AIRTABLE_TABLE_NAME=Bookings": False,                  # harmless: table name
        "SUPABASE_REGION=eu-west-2-london": False,
    }
    for text, want in lines.items():
        got = bool(_box([[100, 300, 10 * len(text), 16, text]]))
        assert got == want, text


# made-up values with the same shapes as the real ones
REF = "abcdwxyzqrstuvklmnop"                                  # Supabase project ref (20 lowercase)
PAT = "tr_pat_k3j9q2lmnb7wq8zrxtyhgfdsapoiuytrewqlkjh"           # trigger.dev token
ENV_FRAME = [
    [100, 300, 520, 16, f"SUPABASE_URL=https://{REF}.supabase.co"],
    [100, 320, 400, 16, f"SUPABASE_PROJECT_REF={REF}"],
    [100, 340, 660, 16, f"TRIGGER_SECRET_KEY={PAT}"],
    [100, 360, 230, 16, "OPENAI_MODEL=gpt-6-luna"],
]


def test_secrets_seen_on_an_env_screen_are_remembered():
    seen = B.secret_values(ENV_FRAME)
    assert REF in seen and PAT in seen
    assert "gpt-6-luna" not in seen


def test_a_remembered_secret_is_blurred_wherever_it_shows_up_again_and_only_its_characters():
    known = B.secret_values(ENV_FRAME)
    url = f"https://{REF}.supabase.co/functions/v1/whatsapp-webhook"
    later = [
        [700, 300, 240, 16, PAT[-24:]],                       # .env scrolled sideways: only the tail shows
        [300, 60, 10 * len(url), 16, url],                     # Claude Code chat / address bar
        [300, 90, 110, 16, "supabase.co"],                     # common part: not a secret
        [300, 120, 250, 16, "proj_imusaasernzovjawtipy"],      # unrelated
    ]
    got = sorted((b for k, b in B.sensitive_boxes(later, W, H, known=known) if k == "known"),
                 key=lambda b: -b[0])
    assert len(got) == 2
    tail, in_url = got
    assert tail == [700, 300, 240, 16]                         # the whole fragment
    assert abs(in_url[0] - (300 + 8 * 10)) <= 2                # "https://" stays readable...
    assert abs(in_url[0] + in_url[2] - (300 + 28 * 10)) <= 2   # ...".supabase.co/..." too; the ref is hidden


def test_spot_scan_covers_thirty_seconds_either_side_of_the_noted_output_time_in_source_time(tmp_path):
    # output timeline: a 0-20 (main1 100-120), b 20-50 (main1 500-530), c 50-120 (main3 0-70)
    edl = {"sources": {"main1": "../main1.mp4", "main3": "../main3.mp4"}, "ranges": [
        {"source": "main1", "start": 100.0, "end": 120.0},
        {"source": "main1", "start": 500.0, "end": 530.0},
        {"source": "main3", "start": 0.0, "end": 70.0}]}
    spans = B.spot_spans(edl, tmp_path, out_t=45.0, window=30.0)      # output 15..75
    got = [(s["source"], round(s["start"], 3), round(s["start"] + s["dur"], 3)) for s in spans]
    assert got == [("main1", 115.0, 120.0), ("main1", 500.0, 530.0), ("main3", 0.0, 25.0)]


def _clip(tmp_path, secs=4):
    clip = tmp_path / "main1.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c=black:s=1512x982:r=30:d={secs}",
                    "-pix_fmt", "yuv420p", str(clip)], check=True)
    return clip


def test_find_blurs_a_secret_seen_early_where_only_its_tail_shows_later(tmp_path, monkeypatch):
    # the OS text reader is stubbed: the .env is fully on screen at 0-1 s, scrolled sideways (tail only) at 2-3 s
    # (a moving picture, so every frame is read: a still screen shares one reading, test_blur_speed.py)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=1512x982:r=30:d=4",
                    "-pix_fmt", "yuv420p", str(tmp_path / "main1.mp4")], check=True)
    E = tmp_path / "edit"; E.mkdir()
    (E / "edl.json").write_text(json.dumps({"sources": {"main1": "../main1.mp4"},
                                            "ranges": [{"source": "main1", "start": 0.0, "end": 4.0}]}))
    def fake_vision(mode, images, work):
        out = {}
        for p in images:
            t = sorted(images).index(p)            # frames are read one a second, in time order
            out[str(p)] = {"text": ENV_FRAME if t < 2 else [[700, 340, 240, 16, PAT[-24:]]]}
        return out
    monkeypatch.setattr(B, "run_vision", fake_vision)
    blurs = B.find(E, [], step=1.0, sensitive=True)["blurs"]
    def covers(b, t, x, y):
        r = b["rect"]
        return b["start"] <= t <= b["end"] and r[0] <= x <= r[0] + r[2] and r[1] <= y <= r[1] + r[3]
    assert any(covers(b, 2.5, 820, 348) for b in blurs), "the scrolled tail at 2-3 s is not blurred"


def test_a_spot_scan_appends_and_keeps_every_existing_blur(tmp_path, monkeypatch):
    _clip(tmp_path)
    E = tmp_path / "edit"; E.mkdir()
    (E / "edl.json").write_text(json.dumps({"sources": {"main1": "../main1.mp4"},
                                            "ranges": [{"source": "main1", "start": 0.0, "end": 4.0}]}))
    old = {"source": "main1", "start": 0.5, "end": 1.5, "rect": [1, 2, 3, 4], "text": "secret", "auto": True}
    (E / "blurs.json").write_text(json.dumps({"version": 1, "blurs": [old]}))
    monkeypatch.setattr(B, "run_vision", lambda m, imgs, w: {str(p): {"text": ENV_FRAME} for p in imgs})
    out = B.find(E, [], step=0.5, sensitive=True, spans=B.spot_spans(json.loads((E / "edl.json").read_text()), E, 2.0, 1.0),
                 append=True)
    assert old in out["blurs"]
    assert any(b["text"] == "key_value" and 1.0 - 0.8 <= b["start"] <= 3.0 for b in out["blurs"])


SECRET_RECT = [400, 300, 300, 40]          # where the made-up key sits on screen (1512-px frame)


def _noisy_clip(tmp_path):
    clip = tmp_path / "main1.mp4"
    x, y, w, h = SECRET_RECT
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=black:s=1512x982:r=30:d=3",
                    "-f", "lavfi", "-i", f"nullsrc=s={w}x{h}:r=30:d=3,geq=lum='random(1)*255':cb=128:cr=128",
                    "-filter_complex", f"[0][1]overlay={x}:{y},format=yuv420p", str(clip)], check=True)
    return clip


def _sharp_text_reader(mode, images, work):
    """Stub OCR: 'reads' the key only while its block is still sharp (high pixel spread)."""
    out = {}
    for p in images:
        x, y, w, h = SECRET_RECT
        line = f"OPENAI_API_KEY={PAT}"
        vx = x + w * 15 // len(line)                       # the value starts after the 15-char label
        px = np.asarray(Image.open(p).convert("L"), dtype=float)[y:y + h, vx:x + w]
        out[str(p)] = {"text": [[x, y, w, h, line]] if px.std() > 25 else []}
    return out


def _edit(tmp_path, blurs):
    E = tmp_path / "edit"; E.mkdir()
    (E / "edl.json").write_text(json.dumps({"sources": {"main1": "../main1.mp4"},
                                            "ranges": [{"source": "main1", "start": 0.0, "end": 3.0}]}))
    (E / "blurs.json").write_text(json.dumps({"version": 1, "blurs": blurs}))
    return E


def test_blur_check_finds_a_key_still_readable_where_no_blur_covers_it(tmp_path, monkeypatch):
    _noisy_clip(tmp_path)
    E = _edit(tmp_path, [])
    monkeypatch.setattr(B, "run_vision", _sharp_text_reader)
    leaks = B.check(E, step=1.0)
    assert leaks and all(l["source"] == "main1" for l in leaks)


def test_blur_check_passes_when_the_real_render_blur_covers_the_key(tmp_path, monkeypatch):
    _noisy_clip(tmp_path)
    x, y, w, h = SECRET_RECT
    E = _edit(tmp_path, [{"source": "main1", "start": 0.0, "end": 3.0, "rect": [x - 10, y - 10, w + 20, h + 20],
                          "text": "secret"}])
    monkeypatch.setattr(B, "run_vision", _sharp_text_reader)
    assert B.check(E, step=1.0) == []


def test_blur_check_fix_adds_a_tight_blur_for_each_leak(tmp_path, monkeypatch):
    _noisy_clip(tmp_path)
    E = _edit(tmp_path, [])
    monkeypatch.setattr(B, "run_vision", _sharp_text_reader)
    B.check(E, step=1.0, fix=True)
    added = json.loads((E / "blurs.json").read_text())["blurs"]
    assert added and B.check(E, step=1.0) == []        # after the fix nothing is readable
    for b in added:                                    # tight: the value, not the label "OPENAI_API_KEY="
        assert b["rect"][0] > SECRET_RECT[0] + 50


def test_spot_scan_still_counts_the_time_of_ranges_it_does_not_read(tmp_path):
    # a face-only range (no screen to read) plays 0-40 s; the screen range after it plays 40-70 s
    edl = {"sources": {"main1": "../main1.mp4"}, "ranges": [
        {"source": "main1", "start": 0.0, "end": 40.0, "layout": "face"},
        {"source": "main1", "start": 300.0, "end": 330.0}]}
    spans = B.spot_spans(edl, tmp_path, out_t=60.0, window=5.0)          # output 55..65
    assert [(s["start"], s["start"] + s["dur"]) for s in spans] == [(315.0, 325.0)]


def test_a_phone_number_id_is_not_remembered_as_a_secret_but_real_phone_numbers_are_blurred():
    # Meta's phone number list: the numbers are private, the "Phone Number ID" is not (user, 2026-11-21)
    env = [[100, 300, 410, 16, "WHATSAPP_PHONE_NUMBER_ID=1000222033304440"]]
    assert "1000222033304440" not in B.secret_values(env)
    meta = [[455, 285, 40, 14, "+256"], [497, 285, 30, 14, "700"], [529, 285, 50, 14, "123456"],
            [455, 296, 90, 10, "Phone"], [547, 296, 40, 10, "Number"], [589, 296, 20, 10, "ID:"],
            [611, 296, 100, 10, "1000222033304440"]]
    got = B.sensitive_boxes(meta, W, H, known=B.secret_values(env))
    assert [k for k, _ in got] == ["phone"]
    assert got[0][1][1] == 285                       # the number's line, not the ID line below it


def test_a_fifteen_digit_phone_number_id_is_not_a_phone_number():
    # Meta phone number IDs are often 15 digits, inside the phone rule's 9-15 (2026-11-21 rehearsal:
    # the whole .env line, field name too, was blurred as a phone)
    assert kinds([[154, 347, 510, 29, "WHATSAPP_PHONE_NUMBER_ID=104857392018475"]]) == []
    meta = [[455, 296, 90, 10, "Phone"], [547, 296, 40, 10, "Number"], [589, 296, 20, 10, "ID:"],
            [611, 296, 100, 10, "104857392018475"]]
    assert kinds(meta) == []


def test_a_phone_number_in_an_env_line_blurs_only_the_number_not_its_field_name():
    # 13 characters of field name + "=", then 13 of number: the box starts halfway along the word
    got = dict(B.sensitive_boxes([[100, 300, 520, 16, "SUPPORT_PHONE=+447700900123"]], W, H))
    x, _, w, _ = got["phone"]
    assert x == 100 + 520 * 14 / 27 and round(x + w) == 620


def test_prefixed_token_ids_like_airtables_are_secrets_but_ordinary_words_are_not():
    # Airtable's token list shows IDs like "patXxxx..." with only one digit or none (user, 2026-11-21)
    for word in ["patQxLmNoPqRsTuVw", "patUv3KdLwPqZsmTa", "sbp_qwErTyUiOpAsDf", "ghp_aBcDeFgHiJkLmNoP"]:
        assert kinds([[300, 400, 160, 14, word]]) == ["secret"], word
    for word in ["patient", "pattern", "patients_bookings", "Patreon", "path/to/file", "pathname"]:
        assert kinds([[300, 400, 160, 14, word]]) == [], word


def test_trigger_project_refs_are_secrets_even_all_lowercase():
    # trigger.dev project refs are "proj_" + 20 lowercase letters/digits (user, 2026-11-21: shown in a chat)
    for word in ["proj_qwertyuiopasdfghjklz", "proj_abcd1234efgh5678ijkl"]:
        assert kinds([[300, 400, 200, 14, word]]) == ["secret"], word
    for word in ["proj_notes", "project_ref", "projects"]:
        assert kinds([[300, 400, 160, 14, word]]) == [], word


def test_a_supabase_project_address_is_blurred_anywhere_but_only_its_project_id():
    # the project ID in "https://<20 letters>.supabase.co/..." showed in Meta's callback URL box in the
    # intro (user, 2026-11-21); the .env.example's "abcd1234.supabase.co" sample is not a real project
    url = "https://qwertyuiopasdfghjklz.supabase.co/functions/v1/whatsapp-webhook"
    got = B.sensitive_boxes([[100, 400, 680, 14, url]], W, H)
    assert [k for k, _ in got] == ["secret"]
    x, _, w, _ = got[0][1]
    assert 100 + 680 * 8 / len(url) - 2 <= x and x + w <= 100 + 680 * 28 / len(url) + 2   # just the ID
    assert B.sensitive_boxes([[100, 400, 400, 14, "e.g. https://abcd1234.supabase.co"]], W, H) == []


def test_numbered_slide_cards_are_not_a_phone_number():
    # the PACT + D slide numbers its cards "01 02 03 04 05" (user, 2026-11-21: a blur line across the slide)
    row = [[400, 381, 30, 30, "01"], [650, 381, 30, 30, "02"], [900, 381, 30, 30, "03"],
           [1150, 381, 30, 30, "04"], [1450, 381, 30, 30, "05"]]
    assert kinds(row) == []
    assert kinds([[300, 400, 160, 14, "+1 415 555 0132"]]) == ["phone"]


def test_plan_text_and_empty_or_placeholder_env_lines_are_never_blurred():
    # user, 2026-11-21: "only keys and emails" - Claude's plan names KEY/TOKEN fields all the time
    plan = [[100, 300, 600, 16, ".env: every key gets listed, with a one-line comment each."],
            [100, 320, 300, 16, "TRIGGER_SECRET_KEY=tr_dev_..."],
            [100, 340, 300, 16, "WHATSAPP_ACCESS_TOKEN=<from Meta>"],
            [100, 360, 420, 16, "SUPABASE_URL=https://your-project.supabase.co"],
            [100, 380, 300, 16, "OPENAI_API_KEY=paste-your-key-here"],
            [100, 400, 300, 16, "SUPABASE_SERVICE_ROLE_KEY="],
            [100, 420, 500, 16, "# Lets the edge function start the agent. DEV key (tr_dev_...)"]]
    assert kinds(plan) == []
    assert B.secret_values(plan) == set()


def test_field_names_are_never_remembered_as_secrets():
    # the reader sometimes runs an empty line into the next one: "SUPABASE_URL=SUPABASE_PROJECT_REF="
    merged = [[100, 300, 500, 16, "SUPABASE_URL=SUPABASE_PROJECT_REF="],
              [100, 320, 500, 16, "WHATSAPP_ACCESS_TOKEN=WHATSAPP_PHONE_NUMBER_ID="]]
    seen = B.secret_values(merged)
    assert not any(v.replace("_", "").isupper() or "SUPABASE" in v or "WHATSAPP" in v for v in seen), seen
    later = [[100, 500, 260, 16, "SUPABASE_PROJECT_REF="], [100, 520, 300, 16, "TRIGGER_SECRET_KEY="]]
    assert kinds(later, known=seen) == []


def test_only_key_like_text_is_remembered_so_comments_are_never_blurred():
    # 2026-11-21: .env comments ("# Master key the agent uses...") got blurred all over 0-15 min
    known = {"Masterkeytheagentusesto9", "conversationstableservice", "qwertyuiopasdfghjklz", "sk_Qa7Lm2Xc9RtB4wNp8Kd3"}
    cores = B._cores(known)
    assert "qwertyuiopasdfghjklz" in cores and "sk_Qa7Lm2Xc9RtB4wNp8Kd3" in cores
    comment = [[100, 300, 600, 16, "# Master key the agent uses to read/write the conversations table"]]
    assert kinds(comment, known=known) == []


def test_a_field_name_glued_to_its_key_does_not_make_the_field_name_secret():
    # the reader sometimes drops the '=': "SUPABASE_SERVICE_ROLE_KEYeyJhbG..." is remembered whole, and the
    # bare field name on an empty .env line must still stay readable (user, 2026-11-21, 08:20)
    known = {"SUPABASE_SERVICE_ROLE_KEYeyJhbGciOiJIUzI1NiIsInR5cCI6", "TRIGGER_SECRET_KEYtr_dev_Qa7Lm2Xc9RtB4wNp"}
    empty = [[100, 300, 260, 16, "SUPABASE_SERVICE_ROLE_KEY="], [100, 320, 200, 16, "TRIGGER_SECRET_KEY="]]
    assert kinds(empty, known=known) == []
    tail = [[100, 340, 240, 16, "eyJhbGciOiJIUzI1NiIsInR5"]]
    assert "known" in kinds(tail, known=known)                 # the key itself is still caught


def test_scanning_again_keeps_the_same_auto_blurs(tmp_path, monkeypatch):
    # bug 2026-11-21: a second full scan dropped every auto blur it found again (old autos were
    # discarded, and new ones equal to an old one were skipped as duplicates)
    _clip(tmp_path)
    E = tmp_path / "edit"; E.mkdir()
    (E / "edl.json").write_text(json.dumps({"sources": {"main1": "../main1.mp4"},
                                            "ranges": [{"source": "main1", "start": 0.0, "end": 4.0}]}))
    monkeypatch.setattr(B, "run_vision", lambda m, imgs, w: {str(p): {"text": ENV_FRAME} for p in imgs})
    first = B.find(E, [], step=1.0, sensitive=True)["blurs"]
    again = B.find(E, [], step=1.0, sensitive=True)["blurs"]
    assert first and again == first


# ---- 12:05 in the WhatsApp video v3 (Supabase API keys page, main1 2087 s, real OCR words)
SUPA_KEYS = [
    [641, 314, 58, 13, "publishable"], [724, 314, 31, 13, "secret"], [776, 314, 24, 13, "keys"],
    [547, 364, 81, 17, "Publishable"], [629, 366, 28, 16, "key"],
    [747, 474, 259, 13, "sb_publishable_Xq7Lm2Pa9RtB-KdmvT4hGw_2pZQN..."],
    [560, 510, 55, 11, "Publishable"], [616, 510, 22, 11, "keys"],
    [1357, 382, 54, 13, "publishable"], [1412, 382, 19, 13, "key"],
    [547, 576, 46, 15, "Secret"], [594, 577, 37, 15, "keys"],
    [747, 685, 58, 11, "sbsecret"],
]


def test_a_remembered_key_never_blurs_the_ordinary_words_it_contains():
    known = B.secret_values(SUPA_KEYS)
    boxes = [b for k, b in B.sensitive_boxes(SUPA_KEYS, W, H, known=known) if k in ("secret", "known")]
    assert boxes and all(b[0] >= 740 and 470 <= b[1] <= 480 for b in boxes)    # only the key itself


def test_a_masked_secret_key_is_blurred_to_the_end_of_its_dots():
    # "sb_secret_KBEcr••••••••••••" reads as "sbsecret" (58 px): its visible start is part of the real key
    masked = [b for k, b in B.sensitive_boxes(SUPA_KEYS, W, H) if k == "masked"]
    assert len(masked) == 1
    x, y, w, h = masked[0]
    assert x <= 747 and y <= 685 and x + w >= 928 and y + h >= 696


def test_a_lone_word_secret_is_not_a_masked_key():
    assert [k for k, _ in B.sensitive_boxes([[547, 576, 46, 15, "Secret"], [594, 577, 37, 15, "keys"]], W, H)] == []


def test_a_row_of_password_dots_alone_is_not_blurred():
    # a password field shows only dots: nothing readable to hide (WhatsApp e2e, main3 19:01)
    items = [[700, 300, 220, 14, "••••••••••••••••••."], [700, 330, 160, 14, "•••••."]]
    assert [k for k, _ in B.sensitive_boxes(items, W, H)] == []
