"""Find words on screen and blur them (Rio 2.0, docs/CONTRACTS.md "Camera look").

    .venv/bin/python rio1/helpers/media/blur_find.py --edit-dir E --text Barbara [--text ...] [--step 1.0]
    .venv/bin/python rio1/helpers/media/blur_find.py --edit-dir E --sensitive [--private "Red Zone"]

--sensitive (2026-11-17) also blurs, with no words given, what sensitive_boxes() finds: emails, phone
numbers, keys/secrets/long IDs (also inside URLs), .env KEY=value lines, the mail pane of an open Gmail
page, and the whole screen inside a private Drive folder (profile blur.private_folders).

Reads the screen in every kept, screen-visible EDL range (split layout; inserts and face-only
ranges skipped) once every --step seconds with macOS Vision text recognition (vision.swift, built
into the OS), finds each --text word (case-insensitive, punctuation ignored) and writes
E/blurs.json:
    {"version": 1, "blurs": [{"source", "start", "end", "rect": [x, y, w, h], "text", "auto": true}]}
in source seconds and source pixels. Sightings of the same word in the same place (boxes overlap)
at most MERGE_GAP_S (8 s) apart are one blur (a name on a slide stays put; OCR misses a frame now
and then); each blur starts PAD_STEPS (1.5) steps before its first sighting and ends 1.5 steps
after its last (a box over plain background blurs to nothing, so the padding is free) and its box is padded by half the text height. Hand-written entries (no "auto") are kept.
The render blurs each rect for its time window (composite.blur_graph). No tokens.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import composite as C  # noqa: E402
from vision import run_vision  # noqa: E402

WIDTH = 1512
MERGE_GAP_S = 8.0
PAD_STEPS = 1.5
PAD_MAX_PX = 30


def norm(s: str) -> str:
    return re.sub(r"[^\w]", "", s).lower()


def matches(word: str, wanted: set[str]) -> str | None:
    w = norm(word)
    for t in wanted:
        if w == t or (len(t) >= 4 and w.startswith(t)):
            return t
    return None


def overlap(a: list[float], b: list[float]) -> bool:
    return not (a[0] + a[2] < b[0] or b[0] + b[2] < a[0] or a[1] + a[3] < b[1] or b[1] + b[3] < a[1])


def group(sightings: list[tuple[str, str, float, list[float]]], step: float,
          size: dict[str, tuple[int, int]]) -> list[dict]:
    """[(source, text, t, box)] -> blur entries (see module doc)."""
    blurs: list[dict] = []
    for sg in sorted(sightings, key=lambda s: (s[0], s[1], s[2])):
        src, text, t, box = sg[:4]
        said = sg[4] if len(sg) > 4 else None
        for b in blurs:
            if (b["source"] == src and b["text"] == text and t - b["_last"] <= max(MERGE_GAP_S, 2.5 * step)
                    and overlap(b["_box"], box)):
                x0, y0 = min(b["_box"][0], box[0]), min(b["_box"][1], box[1])
                x1 = max(b["_box"][0] + b["_box"][2], box[0] + box[2])
                y1 = max(b["_box"][1] + b["_box"][3], box[1] + box[3])
                b["_box"], b["_last"] = [x0, y0, x1 - x0, y1 - y0], t
                if said:
                    b["_said"][said] = b["_said"].get(said, 0) + 1
                break
        else:
            blurs.append({"source": src, "text": text, "_first": t, "_last": t, "_box": list(box),
                          "_said": {said: 1} if said else {}})
    out = []
    for b in blurs:
        x, y, w, h = b["_box"]
        pad = max(6.0, min(h * 0.5, PAD_MAX_PX))   # half a text line, never more (a mail pane stays a pane)
        W, H = size[b["source"]]
        x0, y0 = max(0, int(x - pad)), max(0, int(y - pad))
        x1, y1 = min(W, int(x + w + pad + 1)), min(H, int(y + h + pad + 1))
        out.append({"source": b["source"], "start": round(max(0.0, b["_first"] - PAD_STEPS * step), 2),
                    "end": round(b["_last"] + PAD_STEPS * step, 2), "rect": [x0, y0, x1 - x0, y1 - y0],
                    "text": b["text"], "auto": True})
        if b["_said"]:   # what the reader saw there most often (for the blur judge)
            out[-1]["said"] = max(b["_said"], key=lambda k: (b["_said"][k], len(k)))
    return out


# ---------------------------------------------------------------- sensitive text (2026-11-17)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# .env style, no spaces round '=' (code like "SAFE_ID = /.../" has them), and the value is a URL or a
# key/ID (OCR drops the spaces in code too: "FOLDER='application/vnd...'" is not a secret)
KEY_VALUE_RE = re.compile(r"(?<!\w)([A-Z][A-Z0-9_]{2,})=(\S{8,})")
# .env fields whose value is a secret or identifies an account even when it looks plain (a lowercase
# Supabase project ref, a phone number ID, a made-up verify token); and fields that are never secret
SECRET_FIELD_RE = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|PASS\b|REF|_ID\b|^ID\b|URL|URI|DSN|AUTH|"
                             r"PRIVATE|CREDENTIAL|ACCOUNT|SID|PHONE|EMAIL")
# whole name parts only ("AIRTABLE_BASE_ID" is not a TABLE field)
HARMLESS_FIELD_RE = re.compile(r"(?:^|_)(?:MODEL|NAME|TABLE|REGION|PORT|TIMEOUT|LEVEL|MODE|ENV|VERSION|LANG)(?:_|$)|"
                               r"PHONE_NUMBER_ID")   # user 2026-11-21: the phone number ID is not secret
# digits with spaces/dashes/dots/brackets, 9-15 digits in all (dates have 8, times have ':')
PHONE_RE = re.compile(r"(?<![\w$.,/:])\+?\(?\d[\d\s().-]{7,}\d(?![\w.,%/:])")


SEG_SPLIT_RE = re.compile(r"[^\w\-]+")   # URL slashes, '=', quotes, dots split; '_' and '-' stay (keys use them)
TOKEN_MIN_LEN, TOKEN_MIN_SWITCHES = 16, 6


def _switches(seg: str) -> int:
    """Changes between upper case, lower case and digits along `seg` ('_'/'-' ignored). A random
    key or ID switches all the time; words, file names and camelCase code rarely do."""
    cls = [("u" if c.isupper() else "l" if c.islower() else "d") for c in seg if c.isalnum()]
    return sum(1 for a, b in zip(cls, cls[1:]) if a != b)


# known key/token prefixes followed by a random-looking body (Airtable "pat...", Supabase "sbp_",
# GitHub "ghp_", OpenAI "sk-"/"sk_", trigger.dev "tr_", Meta "EAA"): fewer digits are needed (2026-11-21)
PREFIX_TOKEN_RE = re.compile(r"^(?:pat|sbp_|sb_|ghp_|gho_|sk-|sk_|tr_|xox[abp]-|EAA)([A-Za-z0-9_-]{12,})$")


PROJ_REF_RE = re.compile(r"^proj_[a-z0-9]{16,}$")   # trigger.dev project refs: all lowercase (2026-11-21)


def _is_token_seg(seg: str) -> bool:
    if PROJ_REF_RE.match(seg):
        return True
    m = PREFIX_TOKEN_RE.match(seg)
    if m and _switches(m.group(1)) >= 4:
        return True
    return (len(seg) >= TOKEN_MIN_LEN and _switches(seg) >= TOKEN_MIN_SWITCHES
            and sum(c.isdigit() for c in seg) >= 2)      # names like "AIA-Project2MainSheet" have <2


SUPABASE_HOST_RE = re.compile(r"(?<![a-z0-9])([a-z0-9]{16,24})\.supabase\.(?:co|in)\b")   # project ID (2026-11-21)


def token_spans(word: str) -> list[tuple[int, int]]:
    """Character spans [a, b) of the keys, secrets or long IDs inside `word`."""
    out = [m.span(1) for m in SUPABASE_HOST_RE.finditer(word)]
    pos = 0
    for seg in SEG_SPLIT_RE.split(word):
        a = word.find(seg, pos)
        if seg and _is_token_seg(seg):
            out.append((a, a + len(seg)))
        pos = a + len(seg)
    return out


def is_token(word: str) -> bool:
    """A key, secret or long ID somewhere in `word` (OCR look-alike letters count as letters)."""
    return bool(token_spans(word))


def _lines(items: list) -> list[list]:
    """OCR words grouped into lines (vertical centres within half a word height), left to right."""
    lines: list[list] = []
    for it in sorted(items, key=lambda i: (i[1] + i[3] / 2, i[0])):
        cy = it[1] + it[3] / 2
        for ln in lines:
            ref = ln[0]
            if abs(cy - (ref[1] + ref[3] / 2)) <= max(it[3], ref[3]) / 2:
                ln.append(it)
                break
        else:
            lines.append([it])
    return [sorted(ln, key=lambda i: i[0]) for ln in lines]


def _union(boxes: list) -> list:
    x0 = min(b[0] for b in boxes); y0 = min(b[1] for b in boxes)
    x1 = max(b[0] + b[2] for b in boxes); y1 = max(b[1] + b[3] for b in boxes)
    return [x0, y0, x1 - x0, y1 - y0]


def _span_box(spans: list, a: int, b: int) -> list | None:
    """Box of characters [a, b) of a line, from its OCR words `spans` [(start, end, item)]: each word
    that holds part of the span gives the share of its box those characters take (editor and URL
    text is close to fixed width, so a character is about box width / length)."""
    parts = []
    for s, e, it in spans:
        lo, hi = max(a, s), min(b, e)
        if lo >= hi:
            continue
        n = max(1, e - s)
        x0 = it[0] + it[2] * (lo - s) / n
        x1 = it[0] + it[2] * (hi - s) / n
        parts.append([x0, it[1], x1 - x0, it[3]])
    return _union(parts) if parts else None


def _word(items: list, text: str) -> list:
    return [it for it in items if str(it[4]) == text]


def gmail_pane(items: list, w: int, h: int) -> list | None:
    """The mail pane of an open Gmail page (its own sidebar 'Compose' + 'Inbox' are on screen): right of
    the sidebar, below the search bar, to the frame edge. A Gmail tab or shortcut elsewhere has no Compose."""
    compose, inbox, search = _word(items, "Compose"), _word(items, "Inbox"), _word(items, "Search")
    if not (compose and inbox and search):
        return None
    col = inbox[0][0]
    side = max(it[0] + it[2] for it in items if abs(it[0] - col) <= 15)
    x0 = int(side + 20)
    y0 = int(min(it[1] + it[3] for it in search) + 10)
    return [x0, y0, w - x0, h - y0]


def inside_private(items: list, h: int, names) -> bool:
    """A Drive breadcrumb ('My Drive' in heading size, top third of the screen) that names a private folder."""
    for ln in _lines(items):
        text = " ".join(str(it[4]) for it in ln)
        heading = any(str(a[4]) == "My" and str(b[4]) == "Drive" and a[3] >= 20 and a[1] < h / 3
                      for a, b in zip(ln, ln[1:]))
        if heading and any(n.lower() in text.lower() for n in names):
            return True
    return False


FOLDER_ID_RE = re.compile(r"folders\W?(\w[\w-]{15,})")
ID_MATCH = 0.8


def _folder_ids(items: list) -> list[str]:
    return [m.group(1) for it in items for m in FOLDER_ID_RE.finditer(str(it[4]))]


def private_frames(frames: list[list], names, h: int) -> set[int]:
    """Indices of the frames (in time order) inside a private folder: its breadcrumb is on screen, or the
    address bar shows the folder ID seen in such a frame, or a run of frames keeps showing a folder ID
    next to such a frame (a popup can hide the breadcrumb; OCR misreads a letter or two of an ID, so
    IDs match at ID_MATCH similarity)."""
    if not names:
        return set()
    from difflib import SequenceMatcher
    def same(a: list[str], b: list[str]) -> bool:
        return any(SequenceMatcher(None, x, y).ratio() >= ID_MATCH for x in a for y in b)

    fids = [_folder_ids(items) for items in frames]
    inside = {i for i, items in enumerate(frames) if inside_private(items, h, names)}
    ids = [fid for i in inside for fid in fids[i]]
    inside |= {i for i in range(len(frames)) if ids and same(fids[i], ids)}
    # spread along neighbouring frames (time order) that keep showing a folder ID: the ID must match the
    # neighbour's, or the neighbour shows none (the breadcrumb frame); another folder's ID stops it
    grew = True
    while grew:
        grew = False
        for i in range(len(frames)):
            if i in inside or not fids[i]:
                continue
            for j in (i - 1, i + 1):
                if j in inside and (not fids[j] or same(fids[i], fids[j])):
                    inside.add(i)
                    grew = True
                    break
    return inside


KNOWN_MIN = 10   # a later sighting must share at least this many characters with a remembered secret


def _line_text(ln: list) -> tuple[str, list]:
    text, spans = "", []
    for it in ln:
        if text:
            text += " "
        spans.append((len(text), len(text) + len(str(it[4])), it))
        text += str(it[4])
    return text, spans


PLACEHOLDER_RE = re.compile(r"\.\.\.|\u2026|[<>=]|your|paste|here|example\.|xxxx", re.I)


def _env_secret(field: str, value: str) -> bool:
    """Is `value` (of `field`=value) a real secret? Only keys and IDs that look real (user, 2026-11-21:
    "only keys and emails"): never a placeholder, a field name or plan text that just says KEY/TOKEN."""
    value = value.strip("\"'")
    if PLACEHOLDER_RE.search(value) or re.fullmatch(r"[A-Z0-9_]+", value):
        return False
    if is_token(value):
        return True
    if not SECRET_FIELD_RE.search(field) or HARMLESS_FIELD_RE.search(field):
        return False
    if field.endswith("REF"):
        return bool(re.fullmatch(r"[a-z0-9]{16,24}", value))
    return len(value) >= 12 and any(c.isdigit() for c in value) and not re.search(r"[./]", value)


def secret_values(items: list) -> set[str]:
    """The secrets this frame reveals: values of sensitive .env lines and every key/ID-like token.
    find() remembers them for the whole edit, so a later partial sighting is blurred too."""
    out = {str(it[4])[a:b] for it in items for a, b in token_spans(str(it[4]))}
    for ln in _lines(items):
        text, _ = _line_text(ln)
        for m in KEY_VALUE_RE.finditer(text):
            if _env_secret(m.group(1), m.group(2)):
                out.add(m.group(2).strip("\"'"))
    return out


def _cores(known) -> list[str]:
    """The distinctive parts of remembered secrets: their runs of word characters at least KNOWN_MIN
    long ("https://<ref>.supabase.co" -> "<ref>"; a key stays whole)."""
    segs = (GLUED_FIELD_RE.sub("", seg) for v in known for seg in SEG_SPLIT_RE.split(v))
    return sorted({seg for seg in segs if len(seg) >= KNOWN_MIN and _key_like(seg)})


# a field name the reader glued to its key ("SUPABASE_SERVICE_ROLE_KEYeyJ..."): never part of the secret
GLUED_FIELD_RE = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+(?=[a-z0-9])")


def _key_like(seg: str) -> bool:
    """Only text that looks like a real key is remembered (2026-11-21): a random token, or a Supabase-style
    project ID (20 random lowercase letters/digits, few vowels). Never words, comments or field names."""
    if re.fullmatch(r"[A-Z0-9_]+", seg):
        return False
    if _is_token_seg(seg):
        return True
    if re.fullmatch(r"[a-z0-9]{20}", seg):
        return sum(c in "aeiou" for c in seg) / 20 < 0.3
    return False


def _wordlike(piece: str) -> bool:
    """An ordinary word: letters only and pronounceable (a random key has few vowels)."""
    core = re.sub(r"[_-]", "", piece)
    return core.isalpha() and sum(c in "aeiouAEIOU" for c in core) / max(len(core), 1) >= 0.3


def known_spans(word: str, cores: list[str]) -> list[tuple[int, int]]:
    """Character spans of `word` that repeat at least KNOWN_MIN characters of a remembered secret."""
    from difflib import SequenceMatcher
    out = []
    for c in cores:
        m = SequenceMatcher(None, word, c, autojunk=False).find_longest_match(0, len(word), 0, len(c))
        piece = word[m.a:m.a + m.size]
        # a field name, or an ordinary word the key happens to contain ("publishable" in
        # "sb_publishable_Xq7Lm...", 2026-11-21 12:05): only a random-looking piece is the key
        if m.size >= KNOWN_MIN and not re.fullmatch(r"[A-Z_]+", piece) and not _wordlike(piece):
            out.append((m.a, m.a + m.size))
    return out


# a key shown masked ("sb_secret_KBEcr••••", read as "sbsecret"): its visible start is part of the key
MASKED_RE = re.compile(r"^(?:sb_?secret|sb_?publishable|sbp_|sk[-_](?:live|test|proj)?|rk_live|ghp_|pat|tr_(?:dev|prod))"
                       r"_?[A-Za-z0-9]{0,8}[•●*·.…]*$", re.I)
MASK_RE = re.compile(r"[•●*]{3,}")


def masked_keys(items: list) -> list[list]:
    """Boxes over keys shown masked: a key prefix with dots after it (the reader often drops the dots,
    so the box runs on to the right edge of the widest key box in the same column, or 3.5x the word)."""
    out = []
    for it in items:
        t = str(it[4])
        if MASK_RE.search(t) and not re.search(r"[A-Za-z0-9]{2,}", t):
            continue          # only dots: a password field hides it already
        if not (MASK_RE.search(t) or (MASKED_RE.match(t) and re.search(r"[_\d]|secret|publishable", t, re.I)
                                       and len(t) >= 6)):
            continue
        if is_token(t):
            continue          # a readable key is blurred as a secret already
        x, y, w, h = it[:4]
        col = [o for o in items if o is not it and abs(o[0] - x) <= 15 and o[2] > w and is_token(str(o[4]))]
        right = max((o[0] + o[2] for o in col), default=x + 3.5 * w)
        out.append([x, y, right - x, h])
    return out


# a number labelled as an ID ("..._ID=", "ID:"): Meta's phone number IDs are 15-16 digits (user, 2026-11-21)
ID_LABEL_RE = re.compile(r"(?:\b|_)ID\s*[:=]?\s*$", re.I)


def sensitive_boxes(items: list, w: int, h: int, private=(), known=()) -> list[tuple[str, list]]:
    """[(kind, [x, y, w, h])] for the clearly sensitive text among OCR words `items`
    ([x, y, w, h, text], frame pixels, top-left origin). Kinds: secret (keys, IDs), email, phone,
    key_value (.env lines), gmail (an open mail pane), private (inside a folder in `private`: whole frame)."""
    if private and inside_private(items, h, private):
        return [("private", [0, 0, w, h])]
    out = [("secret", _span_box([(0, len(str(it[4])), it)], a, b))
           for it in items for a, b in token_spans(str(it[4]))]
    cores = _cores(known) if known else []
    for it in items if cores else ():
        out += [("known", _span_box([(0, len(str(it[4])), it)], a, b))
                for a, b in known_spans(str(it[4]), cores)]
    out += [("masked", box) for box in masked_keys(items)]
    pane = gmail_pane(items, w, h)
    if pane:
        out.append(("gmail", pane))
    for ln in _lines(items):
        text, spans, pos = "", [], 0
        for it in ln:
            if text:
                text += " "
            spans.append((len(text), len(text) + len(str(it[4])), it))
            text += str(it[4])
        for kind, rx in (("email", EMAIL_RE), ("key_value", KEY_VALUE_RE), ("phone", PHONE_RE)):
            for m in rx.finditer(text):
                if kind == "phone" and not 9 <= sum(c.isdigit() for c in m.group()) <= 15:
                    continue
                if kind == "phone" and max(len(g) for g in re.findall(r"\d+", m.group())) <= 2:
                    continue      # "01 02 03 04 05": numbered cards on a slide, not a phone (2026-11-21)
                if kind == "phone" and ID_LABEL_RE.search(text[:m.start()]):
                    continue      # "PHONE_NUMBER_ID=1048...", "Phone Number ID: 1048...": an ID, not a phone
                if kind == "key_value":
                    if not _env_secret(m.group(1), m.group(2)):
                        continue
                    out.append((kind, _span_box(spans, m.start(2), m.end(2))))
                    continue
                # only the match's own characters: never a field name glued before it ("SUPPORT_PHONE=")
                out.append((kind, _span_box(spans, m.start(), m.end())))
    return out


def screen_spans(edl: dict, edit_dir: Path) -> list[dict]:
    inserts = set(edl.get("inserts") or [])
    out = []
    for r in edl.get("ranges") or []:
        if r.get("insert") or r["source"] in inserts or r.get("layout") == "face":
            continue
        p = Path(edl["sources"][r["source"]])
        out.append({"source": r["source"], "path": p if p.is_absolute() else (edit_dir / p).resolve(),
                    "start": float(r["start"]), "dur": float(r["end"]) - float(r["start"])})
    return out


def spot_spans(edl: dict, edit_dir: Path, out_t: float, window: float = 30.0) -> list[dict]:
    """The screen spans (as screen_spans) that play within `window` s either side of output time
    `out_t`, clipped to that stretch: what a blur note ("13:11 the key isn't blurred") needs read."""
    lo, hi = out_t - window, out_t + window
    out, pos = [], 0.0
    for r in edl.get("ranges") or []:
        dur = float(r["end"]) - float(r["start"])
        a, b = max(lo, pos), min(hi, pos + dur)
        if b > a:   # every range plays and counts; only screen ranges are read
            for s in screen_spans({**edl, "ranges": [r]}, edit_dir):
                out.append({**s, "start": s["start"] + (a - pos), "dur": b - a})
        pos += dur
    return out


# ---------------------------------------------------------------- faster scan (2026-11-21)
DIFF_SCALE = 4        # frames are compared at a quarter of their size
DIFF_LEVEL = 40       # a pixel changed when it moved more than this (0-255); jpeg noise stays under it
GRID = (16, 9)        # a cell changed when at least DIFF_PIXELS of its pixels did
DIFF_PIXELS = 2
LIVE_MIN_FRAMES, LIVE_SHARE = 4, 0.5   # judged on at least 4 frames: changed in over half the pairs


def _small(p: Path):
    import numpy as np
    from PIL import Image
    im = Image.open(p).convert("L")
    return np.asarray(im.resize((max(1, im.width // DIFF_SCALE), max(1, im.height // DIFF_SCALE)), Image.BOX),
                      dtype=np.int16)


def _cells(a, b):
    import numpy as np
    d = np.abs(a - b) > DIFF_LEVEL
    gx, gy = GRID
    h, w = d.shape
    return np.array([[d[y * h // gy:(y + 1) * h // gy, x * w // gx:(x + 1) * w // gx].sum() >= DIFF_PIXELS
                      for x in range(gx)] for y in range(gy)])


def changed_frames(paths: list[Path]) -> tuple[list[int], list[list[float]]]:
    """For frames of one shot in time order: refs[i] = the frame whose text reading frame i shares (i
    itself when its screen changed since the last frame read), and the boxes ignored as always moving."""
    import numpy as np
    ims = [_small(p) for p in paths]
    gx, gy = GRID
    live = np.zeros((gy, gx), bool)
    if len(ims) >= LIVE_MIN_FRAMES:   # cells that change in most frame pairs: a camera bubble, a video
        live = np.mean([_cells(a, b) for a, b in zip(ims, ims[1:])], axis=0) > LIVE_SHARE
    refs, ref = [], 0
    for i, im in enumerate(ims):
        if i and (_cells(ims[ref], im) & ~live).any():
            ref = i
        refs.append(ref)
    boxes = []
    if live.any():
        from PIL import Image
        with Image.open(paths[0]) as im0:
            W, H = im0.size
        boxes = [[x * W / gx, y * H / gy, W / gx, H / gy] for y in range(gy) for x in range(gx) if live[y, x]]
    return refs, boxes


def read_frames(groups: list[list[Path]], work: Path) -> dict[str, dict]:
    """The text reading of every frame in `groups` (each one shot's frames in time order), running the
    reader only on frames whose screen changed (changed_frames). A frame that shares a reading is read
    after all when that reading has text inside an always-moving area (scrolling text, not a face)."""
    refs = []
    for g in groups:
        r, live = changed_frames(g) if g else ([], [])
        refs.append((r, live))
    first = [g[i] for g, (r, _) in zip(groups, refs) for i in sorted(set(r))]
    res = dict(run_vision("text", first, work))
    again = []
    for g, (r, live) in zip(groups, refs):
        for i, k in enumerate(r):
            if k != i and live and any(overlap(it[:4], b) for it in (res.get(str(g[k])) or {}).get("text") or []
                                       for b in live):
                again.append(g[i])
    res.update(run_vision("text", again, work) if again else {})
    for g, (r, _) in zip(groups, refs):
        for i, k in enumerate(r):
            if str(g[i]) not in res and str(g[k]) in res:
                res[str(g[i])] = res[str(g[k])]
    return res


def _groups(shots: list[tuple], frames: list[Path] | None = None) -> list[list[Path]]:
    """`frames` (default: the shots' own frame files) grouped per shot: shots ((source, t, path), in
    scan order) whose frames sit in one folder are one shot."""
    out: list[list[Path]] = []
    last = None
    for (_, _, p), f in zip(shots, frames if frames is not None else [s[2] for s in shots]):
        if out and p.parent == last:
            out[-1].append(f)
        else:
            out.append([f])
        last = p.parent
    return out


HINT_RE = re.compile(r"key|token|secret|passw|pass\b|pwd|auth|@", re.I)


def said_under(items: list, box: list) -> str:
    """The reader's words under `box`, left to right (what Bea is shown for this blur)."""
    return " ".join(str(it[4]) for it in sorted(items, key=lambda i: i[0]) if overlap(it[:4], box)
                    and (it[0] + it[2] > box[0] + 1 and box[0] + box[2] > it[0] + 1))


def near_misses(items: list, blurred: list) -> list[tuple[str, list]]:
    """Lines the rules left alone that might still hold a secret: a hint word (key, token, password, @...)
    and a word of 6+ characters with a digit in it, none of it blurred. [(line text, line box)]."""
    out = []
    for ln in _lines(items):
        text, _ = _line_text(ln)
        if not HINT_RE.search(text):
            continue
        if not any(len(str(it[4])) >= 6 and re.search(r"\d", str(it[4])) for it in ln):
            continue
        box = _union([it[:4] for it in ln])
        if any(overlap(box, b) for b in blurred):
            continue
        out.append((text, box))
    return out


HOLD_KINDS = ("secret", "known", "masked")


def _same_spot(a: Path, b: Path, box: list, cache: dict) -> bool:
    """Are the pixels under `box` (frame px) the same in frames a and b (jpeg noise aside)?"""
    import numpy as np
    from PIL import Image
    for f in (a, b):
        if f not in cache:
            if len(cache) >= 48:          # ~2.5 MB a frame: keep the RAM small on long videos
                for old in [x for x in cache if x not in (a, b)][:24]:
                    del cache[old]
            cache[f] = np.asarray(Image.open(f).convert("L"), dtype=np.int16)
    x, y, w, h = (int(round(v)) for v in box)
    A, B_ = cache[a][max(y, 0):y + h + 1, max(x, 0):x + w + 1], cache[b][max(y, 0):y + h + 1, max(x, 0):x + w + 1]
    return A.shape == B_.shape and A.size > 0 and int((np.abs(A - B_) > DIFF_LEVEL).sum()) < DIFF_PIXELS


def hold_unchanged(shots: list[tuple], held: list[tuple], size: dict) -> list[tuple]:
    """A key seen in frame n (held: (n, kind, frame-px box)) stays blurred in the next frames of the same
    shot while the pixels under it do not change: tiny keys read right once and then garble (2026-11-21)."""
    out, cache = [], {}
    for n, kind, box, *said in held:
        src, _, p = shots[n]
        k = size[src][0] / WIDTH
        for m in range(n + 1, len(shots)):
            s2, t2, p2 = shots[m]
            if s2 != src or p2.parent != p.parent or not _same_spot(p, p2, box, cache):
                break
            out.append((src, kind, round(t2, 2), [v * k for v in box], *said))
    return out


END_GAP_S = 0.2


def _ffmpeg_frames(path: Path, ss: float, t: float, vf: str, pat: Path) -> None:
    cmd = ["ffmpeg", "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
           # software decode: VideoToolbox is ~8x slower on the screen recordings (see slides.py)
           "-ss", f"{ss:.3f}", "-t", f"{max(t, 0.05):.3f}", "-i", str(path), "-map", "0:v:0", "-an",
           "-vf", f"{vf},scale={WIDTH}:-2,format=yuvj420p", "-fps_mode", "vfr", "-q:v", "3", str(pat)]
    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"blur_find: ffmpeg failed: {proc.stderr.strip()[-300:]}")


def sample_frames(path: Path, start: float, dur: float, step: float, work: Path) -> list[tuple[float, Path]]:
    """Frames of `path` at start, start+step, ... inside [start, start+dur), each labelled with the time
    it shows, plus the shot's last moment when the last sample is more than END_GAP_S before the end
    (a 2.5 s visit to a private folder must still be read). One decode per shot, plus one for the end."""
    work.mkdir(parents=True, exist_ok=True)
    # select the first frame, then the first frame at least `step` after the last one picked
    sel = f"select='isnan(prev_selected_t)+gte(t-prev_selected_t\\,{step - 0.004:.3f})'"
    _ffmpeg_frames(path, start, dur, sel, work / "s_%04d.jpg")
    shots = [(round(start + k * step, 3), f) for k, f in enumerate(sorted(work.glob("s_*.jpg")))]
    end = start + dur
    if not shots or shots[-1][0] < end - END_GAP_S:
        t = round(max(start, end - 0.1), 3)
        _ffmpeg_frames(path, t, 0.05, "null", work / "e_%04d.jpg")
        last = sorted(work.glob("e_*.jpg"))[:1]
        shots += [(t, f) for f in last]
    return shots


def profile_blur() -> dict:
    """The profile's "blur" block: {"sensitive": bool, "step_s": float, "private_folders": [names]}."""
    prof = HERE.parent.parent / "profiles" / "tutorial.json"
    try:
        return json.loads(prof.read_text()).get("blur") or {}
    except (OSError, ValueError):
        return {}


def find(edit_dir: Path, words: list[str], step: float = 1.0, sensitive: bool = False,
         private: list[str] | None = None, spans: list[dict] | None = None, append: bool = False) -> dict:
    """Scan `spans` (default: every kept screen range) every `step` s and write E/blurs.json. Two passes:
    every secret any frame reveals (secret_values) is remembered and blurred wherever it shows up again
    (2026-11-20). append=True (a spot scan for a blur note) keeps every blur already in the file."""
    edl = json.loads((edit_dir / "edl.json").read_text())
    wanted = {norm(w) for w in words if norm(w)}
    if not wanted and not sensitive:
        raise RuntimeError("blur_find: give at least one --text word, or --sensitive")
    work = edit_dir / "_blur_find"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    try:
        spans = spans if spans is not None else screen_spans(edl, edit_dir)
        size = {}
        shots = []
        for i, s in enumerate(spans):
            if s["source"] not in size:
                info = C.probe_video(s["path"])
                size[s["source"]] = (info["w"], info["h"])
            for t, f in sample_frames(s["path"], s["start"], s["dur"], step, work / f"r{i:04d}"):
                shots.append((s["source"], t, f))
        res = read_frames(_groups(shots), work)
        sightings = []
        if sensitive and private:
            # frames inside a private folder, also those where only its ID in the address bar shows it
            texts = [(res.get(str(p)) or {}).get("text") or [] for _, _, p in shots]
            for i in private_frames(texts, private, round(WIDTH * size[shots[0][0]][1] / size[shots[0][0]][0])):
                src, t, _ = shots[i]
                sightings.append((src, "private", round(t, 2), [0, 0, size[src][0], size[src][1]]))
        known: set[str] = set()
        if sensitive:
            for _, _, p in shots:
                known |= secret_values((res.get(str(p)) or {}).get("text") or [])
        held, hints = [], []
        for n, (src, t, p) in enumerate(shots):
            k = size[src][0] / WIDTH
            for item in (res.get(str(p)) or {}).get("text") or []:
                hit = matches(str(item[4]), wanted) if wanted else None
                if hit:
                    sightings.append((src, hit, round(t, 2), [v * k for v in item[:4]]))
            if sensitive:
                items = (res.get(str(p)) or {}).get("text") or []
                fw, fh = WIDTH, round(size[src][1] / k)
                boxes = sensitive_boxes(items, fw, fh, private or [], known=known)
                for kind, box in boxes:
                    said = said_under(items, box) if kind in JUDGED else None
                    sightings.append((src, kind, round(t, 2), [v * k for v in box], said))
                    if kind in HOLD_KINDS:
                        held.append((n, kind, box, said))
                for said, box in near_misses(items, [b for _, b in boxes]):
                    hints.append((src, "hint", round(t, 2), [v * k for v in box], said))
        sightings += hold_unchanged(shots, held, size)
        new = group(sightings, step, size)
        if sensitive:
            ht = edit_dir / "blur_hints.json.tmp"
            ht.write_text(json.dumps({"version": 1, "hints": group(hints, step, size)}, indent=1))
            os.replace(ht, edit_dir / "blur_hints.json")
        path = edit_dir / "blurs.json"
        old = json.loads(path.read_text()).get("blurs", []) if path.is_file() else []
        kept = [b for b in old if append or not b.get("auto")]    # a full scan replaces the old auto blurs
        out = {"version": 1, "blurs": kept + [b for b in new if b not in kept],
               "searched": sorted(wanted) + (["<sensitive>"] if sensitive else []), "frames": len(shots)}
        tmp = edit_dir / "blurs.json.tmp"
        tmp.write_text(json.dumps(out, indent=1))
        os.replace(tmp, path)
        return out
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _apply_blurs(frame: Path, rects: list, scale: float, out: Path) -> Path:
    """`frame` with the render's own blur (blur.blur_graph) over each source-pixel rect."""
    g = C.blur_graph("0:v", "o", [{"a": 0.0, "b": 1e6, "rect": r} for r in rects], scale=scale)
    cmd = ["ffmpeg", "-y", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(frame),
           "-filter_complex", ";".join(g), "-map", "[o]", "-q:v", "3", str(out)]
    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"blur_find: blur check ffmpeg failed: {proc.stderr.strip()[-300:]}")
    return out


def check(edit_dir: Path, step: float = 0.5, spans: list[dict] | None = None, fix: bool = False,
          private: list[str] | None = None) -> list[dict]:
    """The blur check (2026-11-20): read every `step` s of `spans` (default: the whole edit) AFTER the
    blurs in E/blurs.json are applied the way the render applies them, and list what is still readable
    and sensitive: [{"source", "t", "rect", "kind"}] (source s, source px). The secrets to look for are
    learned from the same frames before blurring. fix=True appends a tight blur for each leak."""
    edl = json.loads((edit_dir / "edl.json").read_text())
    path = edit_dir / "blurs.json"
    doc = json.loads(path.read_text()) if path.is_file() else {"version": 1, "blurs": []}
    blurs = doc.get("blurs") or []
    work = edit_dir / "_blur_check"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    try:
        spans = spans if spans is not None else screen_spans(edl, edit_dir)
        size, shots = {}, []
        for i, s in enumerate(spans):
            if s["source"] not in size:
                info = C.probe_video(s["path"])
                size[s["source"]] = (info["w"], info["h"])
            for t, f in sample_frames(s["path"], s["start"], s["dur"], step, work / f"r{i:04d}"):
                shots.append((s["source"], t, f))
        raw = read_frames(_groups(shots), work)
        kept = {b.get("said") for b in doc.get("judged_keep") or []} - {None}   # Bea said: not secret
        known: set[str] = set()
        for _, _, p in shots:
            known |= secret_values([it for it in (raw.get(str(p)) or {}).get("text") or [] if str(it[4]) not in kept])
        seen = []
        for n, (src, t, p) in enumerate(shots):
            rects = [b["rect"] for b in blurs if b.get("source") == src and b["start"] <= t <= b["end"]]
            seen.append(_apply_blurs(p, rects, WIDTH / size[src][0], work / f"b{n:05d}.jpg") if rects else p)
        res = read_frames(_groups(shots, seen), work)
        leaks, sightings = [], []
        for (src, t, _), p in zip(shots, seen):
            k = size[src][0] / WIDTH
            items = (res.get(str(p)) or {}).get("text") or []
            for kind, box in sensitive_boxes(items, WIDTH, round(size[src][1] / k), private or [], known=known):
                if kept and kind in JUDGED and said_under(items, box) in kept:
                    continue
                rect = [round(v * k, 1) for v in box]
                leaks.append({"source": src, "t": round(t, 2), "rect": rect, "kind": kind})
                sightings.append((src, kind, round(t, 2), rect))
        if fix and leaks:
            doc["blurs"] = blurs + [b for b in group(sightings, step, size) if b not in blurs]
            tmp = edit_dir / "blurs.json.tmp"
            tmp.write_text(json.dumps(doc, indent=1))
            os.replace(tmp, path)
        return leaks
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------- the blur judge (2026-11-21)
JUDGED = ("secret", "known", "masked", "key_value", "email", "phone")   # private folders and Gmail stay rules


def _mmss(t: float) -> str:
    return f"{int(t // 60)}:{int(t % 60):02d}"


def candidates(edit_dir: Path) -> list[dict]:
    """The unique on-screen texts to judge: what the rules blurred (auto blurs with a "said") and the near
    misses (E/blur_hints.json), in order of first sight. Writes E/blur_candidates.md for Bea."""
    edit_dir = Path(edit_dir)
    doc = json.loads((edit_dir / "blurs.json").read_text()) if (edit_dir / "blurs.json").is_file() else {}
    hp = edit_dir / "blur_hints.json"
    hints = json.loads(hp.read_text()).get("hints", []) if hp.is_file() else []
    seen: dict[str, dict] = {}
    # blurs Bea already removed (judged_keep) still count, so the ids never shift between runs
    rows = [(b, b["text"]) for b in doc.get("blurs", []) + doc.get("judged_keep", [])
            if b.get("auto") and b.get("said") and b["text"] in JUDGED]
    rows += [(h, "near miss") for h in hints if h.get("said")]
    rows.sort(key=lambda r: (r[0]["start"], r[0]["said"]))
    for b, rule in rows:
        c = seen.get(b["said"])
        if c is None:
            seen[b["said"]] = {"said": b["said"], "rule": rule, "seen": 1, "source": b["source"], "first": b["start"]}
        else:
            c["seen"] += 1
            if b["start"] < c["first"]:
                c["first"], c["source"] = b["start"], b["source"]
    out = list(seen.values())
    for i, c in enumerate(out, 1):
        c["id"] = f"c{i}"
    lines = ["# Blur candidates", "",
             "One line per unique on-screen text: id | why the rules flagged it | times seen | first seen (clip m:ss) | text", ""]
    lines += [f"{c['id']} | {c['rule']} | {c['seen']}x | {c['source']} {_mmss(c['first'])} | {c['said']}" for c in out]
    (edit_dir / "blur_candidates.md").write_text("\n".join(lines) + "\n")
    return out


def apply_labels(edit_dir: Path) -> dict:
    """Bea's labels (E/blur_labels.json: {id: {"call": "blur"|"keep", "why"}}, ids from candidates()) applied
    to E/blurs.json: a "keep" text loses its auto blurs (they are listed under "judged_keep"), a near miss
    called "blur" becomes a blur with the reader's own box. Unlabelled texts stay as the rules left them;
    hand blurs are never touched. Returns {"removed": n, "added": n}."""
    edit_dir = Path(edit_dir)
    ids = {c["id"]: c for c in candidates(edit_dir)}
    lp = edit_dir / "blur_labels.json"
    labels = json.loads(lp.read_text()) if lp.is_file() else {}
    call = {ids[i]["said"]: (v.get("call") if isinstance(v, dict) else v) for i, v in labels.items() if i in ids}
    path = edit_dir / "blurs.json"
    doc = json.loads(path.read_text())
    keep = [b for b in doc.get("blurs", []) if b.get("auto") and call.get(b.get("said")) == "keep" and b["text"] in JUDGED]
    blurs = [b for b in doc["blurs"] if b not in keep]
    hp = edit_dir / "blur_hints.json"
    hints = json.loads(hp.read_text()).get("hints", []) if hp.is_file() else []
    add = [{**{k: v for k, v in h.items() if k != "text"}, "text": "judged", "auto": True}
           for h in hints if call.get(h.get("said")) == "blur"]
    add = [b for b in add if b not in blurs]
    doc["blurs"] = blurs + add
    doc["judged_keep"] = doc.get("judged_keep", []) + keep
    tmp = edit_dir / "blurs.json.tmp"
    tmp.write_text(json.dumps(doc, indent=1))
    os.replace(tmp, path)
    return {"removed": len(keep), "added": len(add)}


SHEET_COLS, SHEET_ROWS, TILE = 3, 8, (500, 170)
SHEET_MAX = 3      # sheets per edit: Bea looks at up to 72 blurs (the riskiest kinds first)
SHEET_ORDER = ("masked", "known", "judged", "key_value", "secret", "email", "phone")


def sheet(edit_dir: Path) -> list[Path]:
    """Contact sheets for Bea's eye check: one tile per judged text (its first blur), the frame with every
    blur applied as the render applies them, cropped around that blur with some context and labelled with
    its candidate id. Writes E/blur_sheets/sheet_N.png and E/blur_sheet.md (tile -> id, time, text)."""
    from PIL import Image, ImageDraw
    edit_dir = Path(edit_dir)
    edl = json.loads((edit_dir / "edl.json").read_text())
    blurs = json.loads((edit_dir / "blurs.json").read_text()).get("blurs", [])
    ids = {c["said"]: c["id"] for c in candidates(edit_dir)}
    first: dict[str, dict] = {}
    for b in sorted(blurs, key=lambda b: b["start"]):
        if b.get("said") in ids and b["said"] not in first:
            first[b["said"]] = b
    order = sorted(first.values(), key=lambda b: (SHEET_ORDER.index(b["text"]) if b["text"] in SHEET_ORDER else 9,
                                                   b["start"]))[:SHEET_COLS * SHEET_ROWS * SHEET_MAX]
    out_dir = edit_dir / "blur_sheets"
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir()
    work = edit_dir / "_blur_sheet"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir()
    tiles, lines, sizes = [], ["# Blur eye check", "", "tile | id | clip time | rule | text", ""], {}
    try:
        for n, b in enumerate(order):
            src = b["source"]
            path = Path(edl["sources"][src])
            path = path if path.is_absolute() else (edit_dir / path).resolve()
            if src not in sizes:
                info = C.probe_video(path)
                sizes[src] = (info["w"], info["h"])
            t = (b["start"] + b["end"]) / 2
            _ffmpeg_frames(path, t, 0.05, "null", work / f"f{n:03d}_%02d.jpg")
            got = sorted(work.glob(f"f{n:03d}_*.jpg"))
            if not got:
                continue
            k = WIDTH / sizes[src][0]
            rects = [x["rect"] for x in blurs if x["source"] == src and x["start"] <= t <= x["end"]]
            img = Image.open(_apply_blurs(got[0], rects, k, work / f"b{n:03d}.jpg")).convert("RGB")
            x, y, w, h = (v * k for v in b["rect"])
            box = (max(0, int(x - 120)), max(0, int(y - 50)), min(img.width, int(x + w + 120)), min(img.height, int(y + h + 50)))
            crop = img.crop(box)
            crop.thumbnail((TILE[0], TILE[1] - 18))
            tile = Image.new("RGB", TILE, (255, 255, 255))
            tile.paste(crop, (0, 18))
            ImageDraw.Draw(tile).text((4, 3), f"{ids[b['said']]}  {src} {_mmss(t)}  {b['text']}", fill=(200, 0, 0))
            tiles.append(tile)
            lines.append(f"{len(tiles)} | {ids[b['said']]} | {src} {_mmss(t)} | {b['text']} | {b['said']}")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    per = SHEET_COLS * SHEET_ROWS
    sheets = []
    for s0 in range(0, len(tiles), per):
        part = tiles[s0:s0 + per]
        rows = (len(part) + SHEET_COLS - 1) // SHEET_COLS
        sh = Image.new("RGB", (SHEET_COLS * (TILE[0] + 6), rows * (TILE[1] + 6)), (60, 60, 60))
        for i, tile in enumerate(part):
            sh.paste(tile, ((i % SHEET_COLS) * (TILE[0] + 6), (i // SHEET_COLS) * (TILE[1] + 6)))
        sheets.append(out_dir / f"sheet_{len(sheets) + 1}.png")
        sh.save(sheets[-1])
    (edit_dir / "blur_sheet.md").write_text("\n".join(lines) + "\n")
    return sheets


def _out_time(tc: str) -> float:
    secs = 0.0
    for part in tc.split(":"):
        secs = secs * 60 + float(part)
    return secs


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--edit-dir", type=Path, required=True)
    ap.add_argument("--text", action="append", default=[])
    ap.add_argument("--sensitive", action="store_true",
                    help="also blur emails, phone numbers, keys/IDs, KEY=value lines, open Gmail and private folders")
    ap.add_argument("--private", action="append", default=None,
                    help="a private folder name (default: the profile's blur.private_folders)")
    ap.add_argument("--step", type=float, default=None)
    ap.add_argument("--around", action="append", default=[], metavar="OUT_TIME",
                    help="spot scan for a blur note: the --window s either side of this output time (M:SS), "
                         "every --step s (default 0.1), appended to blurs.json")
    ap.add_argument("--window", type=float, default=30.0)
    ap.add_argument("--check", action="store_true",
                    help="blur check: read the frames with the blurs applied and list what is still readable")
    ap.add_argument("--fix", action="store_true", help="with --check: add a tight blur for each leak")
    ap.add_argument("--candidates", action="store_true",
                    help="blur judge: write E/blur_candidates.md (the unique texts to label) from the last scan")
    ap.add_argument("--apply-labels", action="store_true",
                    help="blur judge: apply E/blur_labels.json to E/blurs.json")
    ap.add_argument("--sheet", action="store_true",
                    help="blur judge: write the eye-check contact sheets E/blur_sheets/*.png + E/blur_sheet.md")
    a = ap.parse_args(argv)
    if a.candidates:
        n = len(candidates(a.edit_dir.resolve()))
        print(f"blur judge: {n} candidate(s) -> {a.edit_dir / 'blur_candidates.md'}")
        return 0
    if a.apply_labels:
        r = apply_labels(a.edit_dir.resolve())
        print(f"blur judge: removed {r['removed']} blur(s), added {r['added']} -> blurs.json")
        return 0
    if a.sheet:
        for p in sheet(a.edit_dir.resolve()):
            print(f"sheet: {p}")
        return 0
    cfg = profile_blur()
    private = a.private if a.private is not None else list(cfg.get("private_folders") or [])
    E = a.edit_dir.resolve()
    edl = json.loads((E / "edl.json").read_text())
    spans = None
    if a.around:
        spans = [s for tc in a.around for s in spot_spans(edl, E, _out_time(tc), a.window)]
    if a.check:
        step = a.step or (0.1 if a.around else float(cfg.get("check_step_s") or 0.5))
        leaks = check(E, step, spans=spans, fix=a.fix, private=private)
        for l in leaks:
            print(f"leak {l['kind']}: {l['source']} {l['t']:.2f} rect {l['rect']}")
        print(f"blur check: {len(leaks)} leak(s){' (fixed)' if a.fix and leaks else ''}")
        return 0
    step = a.step or (0.1 if a.around else float(cfg.get("step_s") or 1.0))
    out = find(E, a.text, step, sensitive=a.sensitive, private=private, spans=spans, append=bool(a.around))
    for b in out["blurs"]:
        print(f"blur {b.get('text', '?')}: {b['source']} {b['start']:.2f}-{b['end']:.2f} rect {b['rect']}")
    print(f"blur_find: {len(out['blurs'])} blur(s) from {out['frames']} frames -> blurs.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
