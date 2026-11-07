#!/usr/bin/env python3
"""Popular Bible verses (Kreol Morisien via YouVersion Platform API) -> Instagram poster.

Normal run:  guard -> pick next popular verse available in the Bible -> fetch text LIVE from YouVersion
             -> Gemini (prompt + caption) -> Cloudflare FLUX background -> Pillow compose (1080x1350 JPEG)
             -> commit image -> Instagram Graph API -> record the post in popular_verses.csv.

Helper modes (need only YVP_APP_KEY):
  python run.py --list-bibles          show Bibles the API has for language mfe (Kreol Morisien)
  python run.py --check [--apply]      test every verse in popular_verses.csv against YV_BIBLE_IDS

Secrets / env:
  YVP_APP_KEY, GEMINI_API_KEY, CF_ACCOUNT_ID, CF_API_TOKEN, IG_USER_ID, IG_TOKEN
Optional env:
  YV_BIBLE_IDS=344            comma-separated Bible ids, tried in order (verify with --list-bibles)
  CAPTION_LANG="Kreol Morisien (Mauritian Creole)"   EXTRA_HASHTAGS="#kreolmorisien #ilemaurice ..."
  REPEAT_DAYS=90              a verse is eligible again this many days after it was posted
  MAX_VERSE_CHARS=420         longer verses are skipped (status=toolong)
  DRY_RUN=1  FORCE=1  MAX_POSTS_PER_DAY=3  MIN_GAP_MINUTES=120  POST_TZ_OFFSET_HOURS=0
  GEMINI_MODEL  CF_STEPS  IG_API_BASE  IG_API_VERSION  IG_HANDLE  FONT_PATH
"""
from __future__ import annotations

import base64
import csv
import html
import io
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
CSV_PATH = ROOT / "popular_verses.csv"
IMG_DIR = ROOT / "images"
FIELDS = ["rank", "ref", "label", "theme", "status", "last_published_at", "times_posted", "media_id"]
W, H = 1080, 1350  # 4:5 portrait

DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"
FORCE = os.environ.get("FORCE", "0") == "1"
MAX_PER_DAY = int(os.environ.get("MAX_POSTS_PER_DAY", "3"))
MIN_GAP_MIN = int(os.environ.get("MIN_GAP_MINUTES", "120"))
TZ_OFFSET = float(os.environ.get("POST_TZ_OFFSET_HOURS", "0"))
REPEAT_DAYS = int(os.environ.get("REPEAT_DAYS", "90"))
MAX_VERSE_CHARS = int(os.environ.get("MAX_VERSE_CHARS", "420"))
MAX_NEW_UNAVAILABLE = int(os.environ.get("MAX_NEW_UNAVAILABLE", "15"))

YV_BASE = os.environ.get("YV_API_BASE", "https://api.youversion.com/v1").rstrip("/")
YV_BIBLE_IDS = [x.strip() for x in os.environ.get("YV_BIBLE_IDS", "344").split(",") if x.strip()]
YV_LANG = os.environ.get("YV_LANG", "mfe")  # ISO 639-3 for Kreol Morisien

CAPTION_LANG = os.environ.get("CAPTION_LANG", "Kreol Morisien (Mauritian Creole)")
EXTRA_TAGS = os.environ.get("EXTRA_HASHTAGS", "#kreolmorisien #ilemaurice #morisien")

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
CF_MODEL = "@cf/black-forest-labs/flux-1-schnell"
CF_STEPS = int(os.environ.get("CF_STEPS", "4"))
IG_BASE = os.environ.get("IG_API_BASE", "https://graph.instagram.com").rstrip("/")
IG_VER = os.environ.get("IG_API_VERSION", "v23.0")
IG_HANDLE = os.environ.get("IG_HANDLE", "")

STYLES = [
    "misty mountain sunrise, soft pastel sky",
    "loose watercolor wash in warm gold and cream",
    "tropical island coast at golden hour, calm turquoise sea",
    "calm ocean at dawn, long exposure, muted blues",
    "soft starry night sky, deep indigo gradient",
    "minimalist botanical, palm leaf shadows on textured paper",
    "quiet forest path with light filtering through trees",
    "sugarcane fields under a warm evening sky",
]

SYSTEM_PROMPT = (
    "You are a creative director and social media writer for a faith-based Instagram account "
    "that posts one Bible verse per post for a Mauritian audience. Your tone is warm, reverent "
    "and encouraging, never preachy, manipulative or sensational. Return ONLY valid JSON "
    "matching the schema. No markdown fences."
)

USER_PROMPT = """Verse reference: <<reference>> (English label: <<label>>)
Verse text in the Bible translation (shown on the image, do not alter, translate or re-quote it): "<<text>>"
Theme: <<theme>>
Visual style for today: <<style>>
Caption language: <<lang>>

Produce:
1. "image_prompt": an ENGLISH prompt for a FLUX text-to-image model describing a serene, aesthetic
   BACKGROUND image for a 4:5 portrait Instagram post. Rules: no text, letters, words, numbers or
   logos in the image; no people's faces and no depictions of Jesus or God; leave calm negative
   space in the center for overlaid text; soft cinematic lighting; muted, cohesive palette;
   40-60 words. Mood should reflect the verse's theme.
2. "caption": 40-90 words in the caption language, with short, simple sentences. Open with a
   one-line hook (not the verse itself), then a brief, genuine reflection, then a gentle call to
   action (e.g. invite people to share it with someone who needs it today). Put the reference
   on its own line. At most 2 emojis.
3. "hashtags": 6-10 relevant faith-based hashtags, lowercase, no spaces within a tag.
4. "alt_text": one sentence in English describing the image for accessibility.

Schema:
{"image_prompt": str, "caption": str, "hashtags": [str], "alt_text": str}"""

IMAGE_SUFFIX = ", no text, no letters, no words, no watermark, no people, no faces"
BANNED = re.compile(r"\b(jesus|christ|god|angel)\b", re.I)


def log(*a):
    print(*a, flush=True)


# --------------------------------------------------------------------------- http
def http(method: str, url: str, retries: int = 3, retry_429: bool = True, **kw) -> requests.Response:
    """requests wrapper with backoff on 5xx/network errors (and optionally 429)."""
    kw.setdefault("timeout", 60)
    last = None
    for attempt in range(retries):
        try:
            r = requests.request(method, url, **kw)
            retryable = r.status_code >= 500 or (retry_429 and r.status_code == 429)
            if r.ok:
                return r
            last = RuntimeError(f"{method} {url.split('?')[0]} -> {r.status_code}: {r.text[:500]}")
            if not retryable:
                raise last
        except requests.RequestException as e:
            last = e
        time.sleep(3 * (attempt + 1) ** 2)
    raise RuntimeError(f"request failed after {retries} attempts: {last}")


# ------------------------------------------------------------------ YouVersion API
class NotAvailable(Exception):
    """The passage/Bible is not available (400/404/422 or empty text)."""


def yv_get(path: str, params: dict | None = None) -> dict | list:
    headers = {"X-YVP-App-Key": os.environ["YVP_APP_KEY"], "Accept": "application/json"}
    last = None
    for attempt in range(4):
        try:
            r = requests.get(f"{YV_BASE}{path}", headers=headers, params=params, timeout=30)
        except requests.RequestException as e:
            last = e
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code in (400, 404, 422):
            raise NotAvailable(f"{r.status_code} for {path}: {r.text[:200]}")
        if r.status_code == 429 or r.status_code >= 500:
            try:
                wait = int(r.headers.get("Retry-After", "0"))
            except ValueError:
                wait = 0
            last = RuntimeError(f"YouVersion {r.status_code}")
            time.sleep(min(wait or 3 * (attempt + 1) ** 2, 60))
            continue
        if not r.ok:  # 401/403 etc: bad key or no permission, never "mark unavailable"
            raise RuntimeError(f"YouVersion {r.status_code} for {path}: {r.text[:300]}")
        return r.json()
    raise RuntimeError(f"YouVersion request failed: {last}")


def yv_list_bibles(lang: str = YV_LANG) -> list[dict]:
    j = yv_get("/bibles", {"language_ranges[]": lang, "all_available": "true"})
    if isinstance(j, list):
        return j
    return j.get("data") or j.get("bibles") or j.get("items") or []


def yv_bible_meta(bid: str) -> dict:
    j = yv_get(f"/bibles/{bid}")
    books = set()
    for b in j.get("books") or []:
        code = b if isinstance(b, str) else (b.get("id") or b.get("usfm") or "")
        if code:
            books.add(code.upper())
    return {
        "id": bid,
        "abbr": j.get("abbreviation") or j.get("localized_abbreviation") or "",
        "title": j.get("localized_title") or j.get("title") or "",
        "copyright": (j.get("copyright") or "").strip(),
        "books": books,  # empty set = unknown, don't prefilter
    }


REF_RE = re.compile(r"^([1-3]?[A-Z]{2,3}[A-Z0-9]?)\.(\d+)\.(\d+)(?:-(\d+))?$")


def parse_ref(ref: str) -> tuple[str, int, int, int]:
    m = REF_RE.match(ref.strip())
    if not m:
        raise ValueError(f"bad USFM reference: {ref!r} (use BOOK.CH.V or BOOK.CH.V1-V2)")
    book, ch, v1, v2 = m.group(1), int(m.group(2)), int(m.group(3)), m.group(4)
    return book, ch, v1, int(v2) if v2 else v1


def clean_text(s: str, verse_no: int) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = html.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    return re.sub(rf"^{verse_no}\s+", "", s)  # drop a leading verse number if the API includes one


def fetch_verse(bid: str, ref: str) -> dict:
    """Fetch verse(s) one at a time (avoids verse-number noise) and join them."""
    book, ch, v1, v2 = parse_ref(ref)
    texts, first_ref = [], ""
    for v in range(v1, v2 + 1):
        j = yv_get(f"/bibles/{bid}/passages/{book}.{ch}.{v}", {"format": "text"})
        t = clean_text(j.get("content", ""), v)
        if not t:
            raise NotAvailable(f"empty text for {book}.{ch}.{v}")
        texts.append(t)
        first_ref = first_ref or (j.get("reference") or "").strip()
    reference = first_ref or f"{book} {ch}:{v1}"
    if v2 > v1:
        reference = f"{reference}-{v2}"
    return {"text": " ".join(texts), "reference": reference}


# ------------------------------------------------------------------ verse state CSV
def load_rows() -> list[dict]:
    with CSV_PATH.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        for k in FIELDS:
            if r.get(k) is None:
                r[k] = ""
    return rows


def save_rows(rows: list[dict]) -> None:
    with CSV_PATH.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def parse_ts(s: str) -> datetime | None:
    try:
        t = datetime.fromisoformat((s or "").strip())
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def skip_reason(rows: list[dict]) -> str | None:
    """Idempotency guard so delayed / duplicate triggers never double-post."""
    if FORCE:
        return None
    now = datetime.now(timezone.utc)
    times = [t for t in (parse_ts(r["last_published_at"]) for r in rows) if t]
    if times:
        gap = now - max(times)
        if gap < timedelta(minutes=MIN_GAP_MIN):
            return f"last post was {int(gap.total_seconds() // 60)} min ago (< {MIN_GAP_MIN})"
    day = lambda t: (t + timedelta(hours=TZ_OFFSET)).date()  # noqa: E731
    n_today = sum(1 for t in times if day(t) == day(now))
    if n_today >= MAX_PER_DAY:
        return f"already posted {n_today}x today (max {MAX_PER_DAY})"
    return None


def eligible(rows: list[dict]) -> list[dict]:
    """Unposted verses first (by popularity rank), then the longest-ago posted ones."""
    now = datetime.now(timezone.utc)
    out = []
    for r in rows:
        if r["status"] in ("unavailable", "toolong"):
            continue
        t = parse_ts(r["last_published_at"])
        if t and now - t < timedelta(days=REPEAT_DAYS):
            continue
        out.append(r)
    out.sort(key=lambda r: (1 if r["last_published_at"] else 0, r["last_published_at"],
                            int(r["rank"] or 9999)))
    return out


def pick_and_fetch(rows: list[dict], metas: dict[str, dict]) -> tuple[dict, str, dict]:
    """Walk candidates in popularity order until one is available in a configured Bible."""
    changed: list[str] = []
    for row in eligible(rows):
        book = row["ref"].split(".")[0].upper()
        got, too_long = None, False
        for bid, meta in metas.items():
            if meta["books"] and book not in meta["books"]:
                continue
            try:
                v = fetch_verse(bid, row["ref"])
            except NotAvailable as e:
                log(f"  {row['ref']} not in Bible {bid}: {str(e)[:80]}")
                continue
            if len(v["text"]) > MAX_VERSE_CHARS:
                too_long = True
                break
            got = (bid, v)
            break
        if got:
            if changed:
                log(f"Marked {len(changed)} verse(s) unavailable this run: {', '.join(changed)}")
            return row, got[0], got[1]
        row["status"] = "toolong" if too_long else "unavailable"
        changed.append(row["ref"])
        if len(changed) > MAX_NEW_UNAVAILABLE:
            sys.exit(f"Aborting: {len(changed)} verses marked unavailable in one run. That looks like a "
                     f"config/API problem, not missing verses. Run `python run.py --check`. Nothing was saved.")
    sys.exit("No eligible verses available in the configured Bible(s). "
             "Check YV_BIBLE_IDS, lower REPEAT_DAYS, or add more references to popular_verses.csv.")


# ------------------------------------------------------------------ helper CLI modes
def cmd_list_bibles() -> int:
    bibles = yv_list_bibles()
    if not bibles:
        log(f"No Bibles returned for language '{YV_LANG}'. Your App Key may not have access to any.")
        return 1
    for b in bibles:
        log(f"id={b.get('id')}  abbr={b.get('abbreviation')}  title={b.get('localized_title') or b.get('title')}"
            f"  lang={b.get('language_tag')}\n    copyright: {(b.get('copyright') or '')[:160]}")
    return 0


def cmd_check(apply: bool) -> int:
    rows = load_rows()
    metas = {bid: yv_bible_meta(bid) for bid in YV_BIBLE_IDS}
    for m in metas.values():
        log(f"Bible {m['id']}: {m['abbr']} | {m['title']} | books: {', '.join(sorted(m['books'])) or '(unknown)'}")
    ok = bad = 0
    for r in rows:
        book = r["ref"].split(".")[0].upper()
        status = "unavailable"
        for bid, m in metas.items():
            if m["books"] and book not in m["books"]:
                continue
            try:
                v = fetch_verse(bid, r["ref"])
                status = "toolong" if len(v["text"]) > MAX_VERSE_CHARS else "ok"
                break
            except NotAvailable:
                pass
            time.sleep(0.25)
        ok += status == "ok"
        bad += status != "ok"
        log(f"  {r['ref']:<14} {status}")
        if apply:
            r["status"] = "" if status == "ok" else status
    log(f"\n{ok} usable, {bad} not usable out of {len(rows)}")
    if apply:
        save_rows(rows)
        log("popular_verses.csv updated: commit it.")
    return 0


# --------------------------------------------------------------------- LLM content
def _validate(d: dict) -> dict:
    ip = str(d["image_prompt"]).strip()
    cap = str(d["caption"]).strip()
    tags = d["hashtags"]
    alt = str(d.get("alt_text", "")).strip()
    if len(ip) < 20 or len(cap) < 20 or not isinstance(tags, list) or not tags:
        raise ValueError("content too short / malformed")
    if BANNED.search(ip):
        raise ValueError("image_prompt mentions a banned subject")
    tags = ["#" + re.sub(r"[^\w]", "", str(t)).lower() for t in tags]
    tags = [t for t in dict.fromkeys(tags) if len(t) > 1][:15]
    return {"image_prompt": ip, "caption": cap, "hashtags": tags, "alt_text": alt[:900]}


def gemini_content(row: dict, verse: dict, style: str) -> dict:
    prompt = (
        USER_PROMPT.replace("<<reference>>", verse["reference"])
        .replace("<<label>>", row["label"])
        .replace("<<text>>", verse["text"])
        .replace("<<theme>>", row["theme"] or "encouragement")
        .replace("<<style>>", style)
        .replace("<<lang>>", CAPTION_LANG)
    )
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    body = {
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.9},
    }
    r = http("POST", url, headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]}, json=body, timeout=90)
    text = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    return _validate(json.loads(text))


def fallback_content(verse: dict, style: str) -> dict:
    return {
        "image_prompt": f"serene {style}, soft cinematic light, calm negative space in the center, muted palette",
        "caption": verse["reference"],
        "hashtags": ["#bible", "#faith", "#verseoftheday"],
        "alt_text": "A calm, softly lit landscape with a Bible verse written over it.",
    }


def get_content(row: dict, verse: dict, style: str) -> dict:
    for attempt in (1, 2):
        try:
            return gemini_content(row, verse, style)
        except Exception as e:  # noqa: BLE001
            log(f"Gemini attempt {attempt} failed: {e}")
            time.sleep(5)
    log("Using fallback content")
    return fallback_content(verse, style)


def build_caption(c: dict, meta: dict) -> str:
    attribution = " | ".join(x for x in (meta["title"] or meta["abbr"], meta["copyright"]) if x)[:250]
    tags = [t for t in EXTRA_TAGS.split() if t.startswith("#")]
    tags = list(dict.fromkeys(c["hashtags"] + tags))[:20]
    parts = [c["caption"].strip()]
    if attribution:
        parts.append(attribution)
    parts.append(" ".join(tags))
    return "\n\n".join(parts)[:2200]


# ---------------------------------------------------------------- image generation
def gen_cloudflare(prompt: str) -> Image.Image:
    acct, tok = os.environ["CF_ACCOUNT_ID"], os.environ["CF_API_TOKEN"]
    url = f"https://api.cloudflare.com/client/v4/accounts/{acct}/ai/run/{CF_MODEL}"
    # 429 here means the daily Neuron quota is gone: don't hammer it, fall through to the fallback.
    r = http("POST", url, headers={"Authorization": f"Bearer {tok}"},
             json={"prompt": prompt, "steps": CF_STEPS}, timeout=120, retries=2, retry_429=False)
    b64 = r.json()["result"]["image"]
    return Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB")


def gen_pollinations(prompt: str) -> Image.Image:
    url = "https://image.pollinations.ai/prompt/" + urllib.parse.quote(prompt)
    r = http("GET", url, params={"width": W, "height": H, "nologo": "true", "model": "flux",
                                 "seed": random.randint(1, 10**6)}, timeout=180, retries=2)
    return Image.open(io.BytesIO(r.content)).convert("RGB")


def gen_gradient() -> Image.Image:
    palettes = [((38, 50, 72), (214, 170, 120)), ((24, 40, 60), (120, 150, 170)),
                ((60, 45, 70), (230, 180, 150)), ((30, 55, 50), (170, 190, 150))]
    top, bot = random.choice(palettes)
    img = Image.new("RGB", (W, H))
    px = img.load()
    for y in range(H):
        t = y / (H - 1)
        c = tuple(round(top[i] + (bot[i] - top[i]) * t) for i in range(3))
        for x in range(W):
            px[x, y] = c
    return img


def make_background(image_prompt: str) -> Image.Image:
    prompt = image_prompt.rstrip(". ") + IMAGE_SUFFIX
    for name, fn in (("cloudflare", gen_cloudflare), ("pollinations", gen_pollinations)):
        try:
            img = fn(prompt)
            log(f"Background from {name}: {img.size}")
            return img
        except Exception as e:  # noqa: BLE001
            log(f"{name} failed: {e}")
    log("Using local gradient background")
    return gen_gradient()


# --------------------------------------------------------------------- compositing
def cover_crop(img: Image.Image, w: int, h: int) -> Image.Image:
    s = max(w / img.width, h / img.height)
    nw, nh = round(img.width * s), round(img.height * s)
    img = img.resize((nw, nh), Image.LANCZOS)
    l, t = (nw - w) // 2, (nh - h) // 2
    return img.crop((l, t, l + w, t + h))


def font_path() -> str | None:
    for p in (os.environ.get("FONT_PATH", ""), str(ROOT / "fonts" / "font.ttf"),
              "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
              "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf"):
        if p and Path(p).exists():
            return p
    return None


def load_font(size: int) -> ImageFont.FreeTypeFont:
    p = font_path()
    return ImageFont.truetype(p, size) if p else ImageFont.load_default(size=size)


def wrap(draw: ImageDraw.ImageDraw, text: str, font, max_w: int) -> list[str]:
    lines, cur = [], ""
    for word in text.split():
        t = f"{cur} {word}".strip()
        if draw.textlength(t, font=font) <= max_w:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def compose(bg: Image.Image, text: str, ref_line: str) -> Image.Image:
    img = cover_crop(bg, W, H)
    img = Image.blend(img, Image.new("RGB", (W, H), (0, 0, 0)), 0.38)  # darken for contrast
    d = ImageDraw.Draw(img)

    quote = f"\u201c{text.strip()}\u201d"
    max_w, max_h = W - 240, 720
    for size in range(88, 35, -2):
        font = load_font(size)
        lines = wrap(d, quote, font, max_w)
        line_h = int(size * 1.38)
        if len(lines) * line_h <= max_h:
            break
    block_h = len(lines) * line_h
    ref_font = load_font(40)
    total = block_h + 90 + 40
    y = (H - total) // 2 - 20

    for line in lines:
        d.text((W / 2 + 3, y + 3), line, font=font, fill=(0, 0, 0), anchor="ma")
        d.text((W / 2, y), line, font=font, fill=(255, 255, 255), anchor="ma")
        y += line_h
    y += 36
    d.line([(W / 2 - 60, y), (W / 2 + 60, y)], fill=(230, 210, 170), width=3)
    y += 28
    d.text((W / 2, y), ref_line, font=ref_font, fill=(235, 215, 175), anchor="ma")
    if IG_HANDLE:
        d.text((W / 2, H - 80), IG_HANDLE, font=load_font(30), fill=(220, 220, 220), anchor="ma")
    return img


# --------------------------------------------------------------------------- git
def git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], check=check, capture_output=True, text=True, cwd=ROOT)


def commit_push(paths: list[str], msg: str) -> None:
    git("add", *paths)
    if git("diff", "--cached", "--quiet", check=False).returncode == 0:
        return
    git("commit", "-m", msg)
    last = None
    for i in range(4):
        last = git("push", check=False)
        if last.returncode == 0:
            return
        git("pull", "--rebase", "--autostash", check=False)
        time.sleep(2 * (i + 1))
    raise RuntimeError(f"git push failed: {last.stderr[:300]}")


def wait_for_url(url: str, timeout: int = 120) -> None:
    end = time.time() + timeout
    while time.time() < end:
        try:
            if requests.get(url, timeout=20, stream=True).status_code == 200:
                return
        except requests.RequestException:
            pass
        time.sleep(5)
    raise RuntimeError(f"image URL never became reachable: {url}")


# ------------------------------------------------------------------------ Instagram
def ig_url(path: str) -> str:
    ver = f"/{IG_VER}" if IG_VER else ""
    return f"{IG_BASE}{ver}/{path.lstrip('/')}"


def publish_instagram(image_url: str, caption: str, alt: str) -> str:
    uid = os.environ["IG_USER_ID"]
    hdr = {"Authorization": f"Bearer {os.environ['IG_TOKEN']}"}
    data = {"image_url": image_url, "caption": caption}
    if alt:
        data["alt_text"] = alt
    cid = http("POST", ig_url(f"{uid}/media"), headers=hdr, data=data).json()["id"]
    for _ in range(12):
        st = http("GET", ig_url(cid), headers=hdr, params={"fields": "status_code"}).json().get("status_code")
        if st in (None, "FINISHED"):
            break
        if st in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"container status {st}")
        time.sleep(5)
    return http("POST", ig_url(f"{uid}/media_publish"), headers=hdr, data={"creation_id": cid}).json()["id"]


# ----------------------------------------------------------------------------- main
def main(argv: list[str]) -> int:
    if "--list-bibles" in argv or "--check" in argv:
        if not os.environ.get("YVP_APP_KEY"):
            sys.exit("Set YVP_APP_KEY first")
        return cmd_list_bibles() if "--list-bibles" in argv else cmd_check("--apply" in argv)

    needed = ["YVP_APP_KEY", "GEMINI_API_KEY"] + ([] if DRY_RUN else ["IG_USER_ID", "IG_TOKEN", "GITHUB_REPOSITORY"])
    missing = [k for k in needed if not os.environ.get(k)]
    if missing:
        sys.exit(f"Missing env vars: {', '.join(missing)}")

    rows = load_rows()
    reason = skip_reason(rows)
    if reason:
        log(f"Skipping: {reason}")
        return 0

    metas = {}
    for bid in YV_BIBLE_IDS:
        try:
            metas[bid] = yv_bible_meta(bid)
        except NotAvailable:
            log(f"Bible {bid} is not available to this App Key (run `python run.py --list-bibles`)")
    if not metas:
        sys.exit("None of YV_BIBLE_IDS is accessible with this App Key.")

    row, bid, verse = pick_and_fetch(rows, metas)
    meta = metas[bid]
    ref_line = f"{verse['reference']}  \u00b7  {meta['abbr']}".strip(" \u00b7")
    style = STYLES[sum(int(r["times_posted"] or 0) for r in rows) % len(STYLES)]
    log(f"Verse {row['ref']} (rank {row['rank']}) from Bible {bid} | style: {style}")

    content = get_content(row, verse, style)
    img = compose(make_background(content["image_prompt"]), verse["text"], ref_line)

    now = datetime.now(timezone.utc)
    IMG_DIR.mkdir(exist_ok=True)
    slug = re.sub(r"[^A-Za-z0-9]", "", row["ref"]) or "v"
    name = f"{'dryrun-' if DRY_RUN else ''}{now:%Y%m%d-%H%M}-{slug}.jpg"
    path = IMG_DIR / name
    img.convert("RGB").save(path, "JPEG", quality=92, optimize=True)  # Instagram requires JPEG
    caption = build_caption(content, meta)

    if DRY_RUN:
        log(f"DRY RUN: wrote {path}\n--- verse ---\n{verse['text']}\n{ref_line}\n"
            f"--- caption ---\n{caption}\n--- alt ---\n{content['alt_text']}")
        return 0

    branch = os.environ.get("GITHUB_REF_NAME", "main")
    image_url = f"https://raw.githubusercontent.com/{os.environ['GITHUB_REPOSITORY']}/{branch}/images/{name}"
    commit_push([f"images/{name}"], f"image: {row['label']}")
    wait_for_url(image_url)

    media_id = publish_instagram(image_url, caption, content["alt_text"])
    log(f"Published media {media_id}")

    # Record immediately after a successful publish so a later failure cannot cause a double post.
    row["last_published_at"] = now.isoformat(timespec="seconds")
    row["times_posted"] = str(int(row["times_posted"] or 0) + 1)
    row["media_id"] = media_id
    row["status"] = ""
    save_rows(rows)
    commit_push(["popular_verses.csv"], f"published: {row['label']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
