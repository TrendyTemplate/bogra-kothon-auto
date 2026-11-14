#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
বগুড়া কথন — অটো নিউজ সিস্টেম (অ্যাপ্রুভাল সহ)

কাজের ধারা:
  RSS থেকে নিউজ -> ডুপ্লিকেট বাদ -> AI রিরাইট -> ফটো কার্ড -> Telegram-এ ড্রাফট
  -> আপনি রিপ্লাই দেন (APPROVE / REJECT / EDIT: ...) -> Approve হলে Facebook-এ পোস্ট + কমেন্ট

চালানোর উপায় (সাধারণত GitHub Actions নিজেই চালায়):
  python main.py                 স্বাভাবিক রান (রিপ্লাই চেক + সময় হলে নতুন নিউজ)
  python main.py --force-fetch   এখনই নতুন নিউজ আনো
  python main.py --check         সব সেটিং পরীক্ষা + টেস্ট কার্ড Telegram-এ পাঠানো
  python main.py --test-card out.jpg   শুধু একটি নমুনা কার্ড বানিয়ে ফাইলে রাখো
"""
import argparse
import io
import json
import os
import re
import sys
import time
import html as htmllib
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from urllib.parse import urlsplit, urlunsplit

import feedparser
import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageOps, features

# ──────────────────────────────────────────────────────────────
# ধ্রুবক ও পরিবেশ
# ──────────────────────────────────────────────────────────────
ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(ROOT, "data", "state.json")
CONFIG_PATH = os.path.join(ROOT, "config.json")
FONT_DIR = os.path.join(ROOT, "fonts")
LOGO_PATH = next((p for p in (os.path.join(ROOT, "logo.png"), os.path.join(ROOT, "assets", "logo.png")) if os.path.exists(p)),
                 os.path.join(ROOT, "assets", "logo.png"))

GRAPH_VERSION = os.environ.get("GRAPH_VERSION", "v25.0")
GEMINI_MODELS = [m for m in [os.environ.get("GEMINI_MODEL", "").strip(),
                             "gemini-flash-latest", "gemini-2.5-flash", "gemini-flash-lite-latest", "gemini-2.5-flash-lite", "gemini-3.5-flash"] if m]

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept-Language": "bn,en;q=0.8",
}

FONT_URLS = {
    "bold": [
        "https://raw.githubusercontent.com/google/fonts/main/ofl/hindsiliguri/HindSiliguri-Bold.ttf",
        "https://raw.githubusercontent.com/notofonts/notofonts.github.io/main/fonts/NotoSansBengali/full/ttf/NotoSansBengali-Bold.ttf",
    ],
    "medium": [
        "https://raw.githubusercontent.com/google/fonts/main/ofl/hindsiliguri/HindSiliguri-Medium.ttf",
        "https://raw.githubusercontent.com/notofonts/notofonts.github.io/main/fonts/NotoSansBengali/full/ttf/NotoSansBengali-Regular.ttf",
    ],
}
SYSTEM_FONT_FALLBACKS = [
    "/usr/share/fonts/truetype/noto/NotoSansBengali-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSerifBengali-Bold.ttf",
    "/usr/share/fonts/truetype/lohit-bengali/Lohit-Bengali.ttf",
]

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
FB_PAGE = os.environ.get("FACEBOOK_PAGE_ID", "").strip()
FB_TOKEN = os.environ.get("FACEBOOK_ACCESS_TOKEN", "").strip()
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "").strip()


class GeminiQuotaError(Exception):
    pass


def log(*a):
    print(*a, flush=True)


def now_utc():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except Exception:
        return None


BN_DIG = str.maketrans("0123456789", "০১২৩৪৫৬৭৮৯")
BN_MONTH = ["জানুয়ারি", "ফেব্রুয়ারি", "মার্চ", "এপ্রিল", "মে", "জুন", "জুলাই", "আগস্ট",
            "সেপ্টেম্বর", "অক্টোবর", "নভেম্বর", "ডিসেম্বর"]
BN_DAY = ["সোমবার", "মঙ্গলবার", "বুধবার", "বৃহস্পতিবার", "শুক্রবার", "শনিবার", "রবিবার"]


def dhaka_now():
    return now_utc() + timedelta(hours=6)


def bn_date(dt=None):
    dt = dt or dhaka_now()
    return f"{dt.day} {BN_MONTH[dt.month - 1]} {dt.year}".translate(BN_DIG)


def bn_date_long(dt=None):
    dt = dt or dhaka_now()
    return f"{BN_DAY[dt.weekday()]}, {bn_date(dt)}"


# ──────────────────────────────────────────────────────────────
# কনফিগ ও স্টেট
# ──────────────────────────────────────────────────────────────
def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                st = json.load(f)
        except Exception:
            log("⚠️ state.json পড়া যায়নি, নতুন করে শুরু হচ্ছে")
            st = {}
    else:
        st = {}
    st.setdefault("update_offset", 0)
    st.setdefault("next_id", 1)
    st.setdefault("last_fetch", None)
    st.setdefault("seen", [])
    st.setdefault("drafts", {})
    st.setdefault("msg_map", {})
    st.setdefault("daily", {"date": "", "count": 0})
    return st


def save_state(st):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    # পুরনো তথ্য ছাঁটাই
    st["seen"] = st["seen"][-1500:]
    cutoff = now_utc() - timedelta(days=4)
    for did in list(st["drafts"].keys()):
        d = st["drafts"][did]
        if d["status"] in ("pending", "failed", "posted_no_comment"):
            continue
        t = parse_iso(d.get("updated", d.get("created", "")))
        if t and t < cutoff:
            del st["drafts"][did]
    alive = set(st["drafts"].keys())
    st["msg_map"] = {m: str(d) for m, d in st["msg_map"].items() if str(d) in alive}
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_PATH)


# ──────────────────────────────────────────────────────────────
# টেক্সট হেল্পার
# ──────────────────────────────────────────────────────────────
TOKEN_RE = re.compile(r"[\u0980-\u09FFa-zA-Z0-9]+")
URL_RE = re.compile(r"https?://\S+|www\.\S+", re.I)


def clean_text(s):
    s = htmllib.unescape(s or "")
    s = BeautifulSoup(s, "lxml").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", s).strip()


def tokens(s):
    return {t.lower() for t in TOKEN_RE.findall(s or "") if len(t) > 1}


def similar(a, b):
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return False
    jac = len(ta & tb) / len(ta | tb)
    if jac >= 0.6:
        return True
    if jac >= 0.3:
        return SequenceMatcher(None, a, b).ratio() >= 0.85
    return False


def norm_url(u):
    try:
        p = urlsplit(u)
        return urlunsplit((p.scheme, p.netloc.lower(), p.path.rstrip("/"), "", ""))
    except Exception:
        return u


def strip_links(s):
    s = URL_RE.sub("", s or "")
    return re.sub(r"[ \t]+\n", "\n", s).strip()


# ──────────────────────────────────────────────────────────────
# ১. নিউজ কালেকশন
# ──────────────────────────────────────────────────────────────
def find_feed_image(e):
    for key in ("media_content", "media_thumbnail"):
        for m in e.get(key, []) or []:
            if m.get("url"):
                return m["url"]
    for enc in e.get("enclosures", []) or []:
        if str(enc.get("type", "")).startswith("image") and enc.get("href"):
            return enc["href"]
    for l in e.get("links", []) or []:
        if str(l.get("type", "")).startswith("image") and l.get("href"):
            return l["href"]
    blob = ""
    if e.get("content"):
        blob += e["content"][0].get("value", "")
    blob += e.get("summary", "") or ""
    m = re.search(r'<img[^>]+src=["\']([^"\']+)', blob)
    return m.group(1) if m else None


def fetch_feed(feed):
    r = requests.get(feed["url"], headers=HEADERS, timeout=25)
    r.raise_for_status()
    parsed = feedparser.parse(r.content)
    out = []
    for e in parsed.entries[:30]:
        title = clean_text(e.get("title", ""))
        link = e.get("link", "")
        if not title or not link:
            continue
        pub = None
        for k in ("published_parsed", "updated_parsed"):
            if e.get(k):
                pub = datetime(*e[k][:6], tzinfo=timezone.utc)
                break
        content = ""
        if e.get("content"):
            content = clean_text(e["content"][0].get("value", ""))
        out.append({
            "source": feed["name"], "title": title, "link": link,
            "published": pub, "summary": clean_text(e.get("summary", "")),
            "content": content, "image": find_feed_image(e),
        })
    return out


def collect_items(cfg):
    items = []
    for feed in cfg["feeds"]:
        if not feed.get("enabled", True):
            continue
        try:
            got = fetch_feed(feed)
            log(f"  ✓ {feed['name']}: {len(got)}টি")
            items += got
        except Exception as ex:
            log(f"  ✗ {feed['name']}: ব্যর্থ ({str(ex)[:80]})")
    return items


def score_item(it, cfg, now):
    title, text = it["title"], it["title"] + " " + it["summary"]
    score = 0.0
    if any(k.lower() in title.lower() for k in cfg["breaking_keywords"]):
        score += 40
    if any(k.lower() in text.lower() for k in cfg["bogra_keywords"]):
        score += 50
    age_h = ((now - it["published"]).total_seconds() / 3600) if it["published"] else 3
    score += max(0.0, 20 - age_h * 2)
    score += 8 * it.get("cluster", 0)  # একাধিক মিডিয়া ছাপলে গুরুত্ব বেশি
    if it.get("image"):
        score += 3
    return score


def is_seen(it, st):
    key = norm_url(it["link"])
    for s in st["seen"]:
        if s["k"] == key or similar(s["t"], it["title"]):
            return True
    return False


def mark_seen(it, st):
    st["seen"].append({"k": norm_url(it["link"]), "t": it["title"], "ts": iso(now_utc())})


def rank_items(items, cfg, st):
    now = now_utc()
    max_age = cfg["max_article_age_hours"]
    fresh = []
    for it in items:
        if it["published"] and (now - it["published"]).total_seconds() > max_age * 3600:
            continue
        if is_seen(it, st):
            continue
        fresh.append(it)
    # ক্লাস্টার: একই খবর কয়েকটি মিডিয়ায়
    for it in fresh:
        it["cluster"] = sum(1 for o in fresh if o is not it and o["source"] != it["source"]
                            and similar(o["title"], it["title"]))
    fresh.sort(key=lambda x: score_item(x, cfg, now), reverse=True)
    picked = []
    for it in fresh:
        if any(similar(it["title"], p["title"]) for p in picked):
            continue
        picked.append(it)
    return picked


def fetch_article(url):
    """আর্টিকেল পেজ থেকে মূল টেক্সট ও ছবির লিংক আনে। ব্যর্থ হলে খালি ফেরত দেয়।"""
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
        img = None
        for attrs in ({"property": "og:image"}, {"name": "twitter:image"}):
            m = soup.find("meta", attrs=attrs)
            if m and m.get("content"):
                img = requests.compat.urljoin(url, m["content"])
                break
        box = soup.find("article") or soup
        paras = [p.get_text(" ", strip=True) for p in box.find_all("p")]
        text = "\n".join(p for p in paras if len(p) >= 40)
        return {"text": text[:5000], "image": img}
    except Exception as ex:
        log(f"    (আর্টিকেল পেজ পড়া যায়নি: {str(ex)[:60]})")
        return {"text": "", "image": None}


# ──────────────────────────────────────────────────────────────
# ২. AI রিরাইটিং (Gemini — ফ্রি)
# ──────────────────────────────────────────────────────────────
PROMPT = """তুমি "বগুড়া কথন" নিউজ পোর্টালের অভিজ্ঞ বাংলা সম্পাদক। নিচের সংবাদ-সূত্র থেকে নিজের ভাষায় সম্পূর্ণ নতুন করে একটি সংবাদ লেখো।

কঠোর নিয়ম:
১. শুধু সূত্রে থাকা তথ্য ব্যবহার করো। নিজে থেকে কোনো তথ্য, সংখ্যা, নাম, উদ্ধৃতি, কারণ বা ব্যাখ্যা যোগ করবে না।
২. সব নাম, স্থান, তারিখ, সময়, সংখ্যা, পদবি হুবহু ঠিক রাখো।
৩. মূল লেখার বাক্য বা শব্দবিন্যাস কপি করবে না। বাক্যের গঠন ও শব্দচয়ন নতুন করে সাজাও।
৪. সূত্র ইংরেজিতে হলে স্বাভাবিক বাংলায় লেখো।
৫. ভাষা প্রমিত, সহজ ও পেশাদার। অতিরঞ্জন বা ক্লিকবেইট নয়।
৬. কোনো লিংক, ওয়েবসাইট বা সংবাদমাধ্যমের নাম, ইমোজি লিখবে না। headline, intro ও body-তে হ্যাশট্যাগ নয় (শুধু hashtags ফিল্ডে)।
৭. অভিযোগ, দাবি বা সন্দেহকে নিশ্চিত তথ্য হিসেবে লিখবে না ("অভিযোগ করা হয়েছে", "পুলিশ জানিয়েছে" ইত্যাদি ব্যবহার করো)।
৮. ধর্ষণ বা যৌন নিপীড়নের শিকার ব্যক্তি এবং শিশু অপরাধীর পরিচয় প্রকাশ করবে না।
৯. সূত্রে পর্যাপ্ত তথ্য না থাকলে, বা এটি সংবাদ না হয়ে বিজ্ঞাপন/প্রচার হলে, skip=true দাও।

আউটপুট ফরম্যাট (শুধু JSON):
- skip: true/false
- headline: ফেসবুক ফটো কার্ডের শিরোনাম। ৬–১২ শব্দ, সর্বোচ্চ ৭০ অক্ষর, সরাসরি ও আকর্ষণীয়, শেষে দাঁড়ি নয়।
- category: ১ শব্দের ট্যাগ (যেমন: বগুড়া, রাজনীতি, দুর্ঘটনা, অর্থনীতি, খেলা, আন্তর্জাতিক, শিক্ষা, স্বাস্থ্য, বিনোদন, জাতীয়)
- intro: ফেসবুক ক্যাপশনের জন্য ২–৩ লাইনের সারকথা (সর্বোচ্চ ২২০ অক্ষর)। শিরোনামের পরিপূরক, কিন্তু পুরো খবর ফাঁস করবে না; আগ্রহ জাগিয়ে কমেন্টে যেতে উৎসাহ দেবে।
- hashtags: ৫–৬টি হ্যাশট্যাগ, শুধু ইংরেজি অক্ষরে (বাংলা অক্ষর নয়), প্রতিটি # দিয়ে শুরু, ভেতরে স্পেস নয়, CamelCase। বিষয়, স্থান ও জনপ্রিয় সংশ্লিষ্ট শব্দভিত্তিক। যেমন #Bogura #RoadAccident #BangladeshNews।
- body: বিস্তারিত সংবাদ। ৩–৪টি ছোট অনুচ্ছেদ (অনুচ্ছেদের মাঝে ফাঁকা লাইন), মোট ১২০–২২০ শব্দ, প্রথম অনুচ্ছেদে মূল ঘটনা।

সূত্রের শিরোনাম: {title}

সূত্রের বিষয়বস্তু:
{text}
"""

SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "skip": {"type": "BOOLEAN"},
        "headline": {"type": "STRING"},
        "category": {"type": "STRING"},
        "body": {"type": "STRING"},
        "intro": {"type": "STRING"},
        "hashtags": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["skip", "headline", "category", "body", "intro", "hashtags"],
}


def gemini_rewrite(title, text):
    prompt = PROMPT.format(title=title, text=text[:5000])
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.5,
            "maxOutputTokens": 4096,
            "responseMimeType": "application/json",
            "responseSchema": SCHEMA,
        },
    }
    last_err = "unknown"
    for model in GEMINI_MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for attempt in range(3):
            try:
                r = requests.post(url, json=body, timeout=90,
                                  headers={"x-goog-api-key": GEMINI_KEY, "Content-Type": "application/json"})
            except Exception as ex:
                last_err = str(ex)
                time.sleep(3)
                continue
            if r.status_code == 200:
                try:
                    data = r.json()
                    txt = data["candidates"][0]["content"]["parts"][0]["text"]
                    out = json.loads(txt)
                    return out
                except Exception as ex:
                    last_err = f"উত্তর পার্স হয়নি: {ex}"
                    break
            if r.status_code == 429:
                last_err = "429 (ফ্রি লিমিট শেষ/ব্যস্ত)"
                time.sleep(8 * (attempt + 1))
                continue
            if r.status_code == 404:
                last_err = f"404 {model}: " + r.text[:120].replace(chr(10), " ")
                break
            if r.status_code in (500, 503):
                last_err = f"{r.status_code} সার্ভার ব্যস্ত"
                time.sleep(5 * (attempt + 1))
                continue
            last_err = f"{r.status_code}: {r.text[:200]}"
            break
    if "429" in last_err:
        raise GeminiQuotaError(last_err)
    raise RuntimeError("Gemini ব্যর্থ: " + last_err)


def validate_ai(out, cfg):
    if not isinstance(out, dict) or out.get("skip"):
        return None
    headline = strip_links(re.sub(r"\s+", " ", str(out.get("headline", ""))).strip()).rstrip("।.").strip()
    body = strip_links(str(out.get("body", "")).strip())
    body = re.sub(r"\n{3,}", "\n\n", body)
    category = re.sub(r"[^\u0980-\u09FFa-zA-Z ]", "", str(out.get("category", ""))).strip()[:14] or "খবর"
    if len(headline) < 8 or len(body) < 80:
        return None
    if len(headline) > 110:
        headline = headline[:107].rstrip() + "…"
    intro = strip_links(re.sub(r"\s+", " ", str(out.get("intro", ""))).strip())[:260]
    tags = []
    for t in (out.get("hashtags") or []):
        t = "#" + re.sub(r"[^a-zA-Z0-9_]", "", str(t))
        if len(t) > 3 and t not in tags:
            tags.append(t)
    return {"headline": headline, "body": body, "category": category, "intro": intro, "hashtags": tags[:6]}


# ──────────────────────────────────────────────────────────────
# ৩. ফটো কার্ড (১০৮০×১০৮০)
# ──────────────────────────────────────────────────────────────
def hex_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


_FONT_PATHS = {}


def ensure_fonts():
    os.makedirs(FONT_DIR, exist_ok=True)
    for kind, urls in FONT_URLS.items():
        path = None
        for u in urls:
            local = os.path.join(FONT_DIR, os.path.basename(u))
            if os.path.exists(local) and os.path.getsize(local) > 50000:
                path = local
                break
            try:
                r = requests.get(u, timeout=40)
                if r.status_code == 200 and len(r.content) > 50000:
                    with open(local, "wb") as f:
                        f.write(r.content)
                    path = local
                    break
            except Exception:
                pass
        _FONT_PATHS[kind] = path
    if not _FONT_PATHS.get("bold"):
        for p in SYSTEM_FONT_FALLBACKS:
            if os.path.exists(p):
                _FONT_PATHS["bold"] = p
                break
    if not _FONT_PATHS.get("medium"):
        _FONT_PATHS["medium"] = _FONT_PATHS.get("bold")
    if not _FONT_PATHS.get("bold"):
        raise RuntimeError("বাংলা ফন্ট পাওয়া যায়নি (ইন্টারনেট/ফন্ট লিংক সমস্যা)")
    return _FONT_PATHS


def font(kind, size):
    if not _FONT_PATHS:
        ensure_fonts()
    layout = ImageFont.Layout.RAQM if features.check("raqm") else ImageFont.Layout.BASIC
    return ImageFont.truetype(_FONT_PATHS[kind], size, layout_engine=layout)


def wrap_text(text, fnt, max_w):
    lines, cur = [], ""
    for w in text.split():
        t = (cur + " " + w).strip()
        if fnt.getlength(t) <= max_w or not cur:
            cur = t
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def fit_headline(text, max_w, max_h, max_lines=5):
    for size in range(108, 49, -4):
        f = font("bold", size)
        lines = wrap_text(text, f, max_w)
        lh = int(size * 1.16)
        if len(lines) <= max_lines and len(lines) * lh <= max_h:
            return f, lines, lh
    f = font("bold", 50)
    lines = wrap_text(text, f, max_w)[:max_lines]
    lines[-1] = lines[-1].rstrip("… ") + "…"
    return f, lines, int(50 * 1.16)


def cover_crop(img, size):
    return ImageOps.fit(img, (size, size), method=Image.LANCZOS, centering=(0.5, 0.4))


def brand_background(size, green):
    """ছবি না থাকলে ব্র্যান্ড কালারের গ্র্যাডিয়েন্ট ব্যাকগ্রাউন্ড।"""
    top = tuple(min(255, int(c * 1.9)) for c in green)
    grad = Image.linear_gradient("L").resize((size, size))
    a = Image.new("RGB", (size, size), top)
    b = Image.new("RGB", (size, size), green)
    return Image.composite(b, a, grad)


def draw_fallback_logo(size, green, red, cream):
    """assets/logo.png না থাকলে সাময়িক মাইক্রোফোন লোগো আঁকে।"""
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=int(size * 0.22), fill=cream)
    cx = size // 2
    mw, mh = int(size * 0.26), int(size * 0.40)
    top = int(size * 0.14)
    d.rounded_rectangle([cx - mw // 2, top, cx + mw // 2, top + mh], radius=mw // 2, fill=green)
    d.arc([cx - int(size * 0.24), top + int(size * 0.14), cx + int(size * 0.24), top + int(size * 0.14) + int(size * 0.40)],
          start=0, end=180, fill=red, width=max(4, size // 24))
    d.line([cx, top + int(size * 0.54), cx, int(size * 0.80)], fill=red, width=max(4, size // 24))
    d.line([cx - int(size * 0.14), int(size * 0.80), cx + int(size * 0.14), int(size * 0.80)], fill=red,
           width=max(4, size // 24))
    return im


def load_logo(box, brand):
    green, red, cream = (hex_rgb(brand[k]) for k in ("green", "red", "cream"))
    if os.path.exists(LOGO_PATH):
        try:
            lg = Image.open(LOGO_PATH).convert("RGBA")
            lg.thumbnail((box, box), Image.LANCZOS)
            return lg
        except Exception as ex:
            log("  (logo.png পড়া যায়নি, সাময়িক লোগো ব্যবহার হচ্ছে):", ex)
    return draw_fallback_logo(box, green, red, cream)


def render_card(headline, category, photo_bytes, cfg, variant=0):
    S = 1080
    brand = cfg["brand"]
    green, red, cream = (hex_rgb(brand[k]) for k in ("green", "red", "cream"))

    # ব্যাকগ্রাউন্ড
    bg = None
    if photo_bytes:
        try:
            im = Image.open(io.BytesIO(photo_bytes))
            im = ImageOps.exif_transpose(im).convert("RGB")
            if min(im.size) >= 300:
                bg = ImageOps.fit(im, (S, S), method=Image.LANCZOS, centering=((0.5, 0.2, 0.8)[variant % 3], 0.4))
                # কার্ডের নিজস্ব ডিজাইন-ট্রিটমেন্ট: হালকা জুম, রঙ-ঘনত্ব ও কনট্রাস্ট
                z = 1.08
                bg = bg.crop((int(S * (z - 1) / 2 / z), int(S * (z - 1) / 3 / z), int(S * (1 - (z - 1) / 2 / z)),
                              int(S * (1 - (z - 1) * 2 / 3 / z)))).resize((S, S), Image.LANCZOS)
                bg = ImageEnhance.Contrast(ImageEnhance.Color(bg).enhance(1.08)).enhance(1.06)
        except Exception as ex:
            log("  (ছবি খোলা যায়নি, ব্র্যান্ড ব্যাকগ্রাউন্ড হবে):", ex)
    if bg is None:
        bg = brand_background(S, green)
    card = bg.convert("RGBA")

    # নিচ থেকে গাঢ় সবুজ গ্র্যাডিয়েন্ট (লেখা পড়ার জন্য)
    tint = Image.new("RGBA", (S, S), green + (46,))  # পুরো ছবিতে হালকা সবুজ আবরণ
    card.alpha_composite(tint)
    grad_h = 860
    mask = Image.linear_gradient("L").resize((S, grad_h))  # উপরে 0 -> নিচে 255
    mask = mask.point(lambda v: int(min(255, (v / 255) ** 0.85 * 255 * 0.97)))
    overlay = Image.new("RGBA", (S, grad_h), green + (255,))
    overlay.putalpha(mask)
    card.alpha_composite(overlay, (0, S - grad_h))

    # উপরে হালকা ছায়া (লোগোর জন্য)
    top_mask = Image.linear_gradient("L").transpose(Image.FLIP_TOP_BOTTOM).resize((S, 260))
    top_mask = top_mask.point(lambda v: int(v * 0.45))
    top = Image.new("RGBA", (S, 260), (0, 0, 0, 255))
    top.putalpha(top_mask)
    card.alpha_composite(top, (0, 0))

    d = ImageDraw.Draw(card)

    # লোগো (উপরে বামে)
    logo = load_logo(128, brand)
    pad = 12
    plate_w, plate_h = logo.width + pad * 2, logo.height + pad * 2
    plate = Image.new("RGBA", (plate_w, plate_h), (0, 0, 0, 0))
    ImageDraw.Draw(plate).rounded_rectangle([0, 0, plate_w - 1, plate_h - 1], radius=26, fill=cream + (255,))
    plate.alpha_composite(logo, (pad, pad))
    card.alpha_composite(plate, (56, 56))

    # তারিখ (উপরে ডানে)
    dfont = font("medium", 30)
    dtxt = bn_date()
    dw = int(d.textlength(dtxt, font=dfont)) + 40
    dx1 = S - 56
    d.rounded_rectangle([dx1 - dw, 78, dx1, 78 + 52], radius=14, fill=(0, 0, 0, 120))
    d.text((dx1 - dw + 20, 78 + 26), dtxt, font=dfont, fill=cream + (255,), anchor="lm")

    # বাম মার্জিন ও লেখার এলাকা
    LM, RM = 72, 72
    max_w = S - LM - RM
    footer_y = S - 64
    block_bottom = footer_y - 20

    fnt, lines, lh = fit_headline(headline, max_w, max_h=int(lh_limit(S)))
    text_h = len(lines) * lh
    y0 = block_bottom - text_h

    # ক্যাটাগরি চিপ + লাল দাগ
    cat_font = font("bold", 34)
    cat_w = int(cat_font.getlength(category)) + 44
    chip_h = 58
    chip_y = y0 - chip_h - 26
    d.rounded_rectangle([LM, chip_y, LM + cat_w, chip_y + chip_h], radius=14, fill=red + (255,))
    d.text((LM + 22, chip_y + chip_h // 2), category, font=cat_font, fill=cream + (255,), anchor="lm")

    # শিরোনাম
    for i, ln in enumerate(lines):
        d.text((LM, y0 + i * lh + int(lh * 0.5)), ln, font=fnt, fill=cream + (255,), anchor="lm")

    # ফুটার
    d.rectangle([LM, S - 62, LM + 70, S - 56], fill=red + (255,))

    out = io.BytesIO()
    card.convert("RGB").save(out, format="JPEG", quality=93, optimize=True, subsampling=0)
    return out.getvalue()


def lh_limit(S):
    return S * 0.50


def download_image(url):
    if not url:
        return None
    low = url.lower()
    if any(x in low for x in ("logo", "placeholder", "default-image", "no-image")):
        return None
    try:
        r = requests.get(url, headers=HEADERS, timeout=25)
        if r.status_code == 200 and 5_000 < len(r.content) < 15_000_000:
            return r.content
    except Exception:
        pass
    return None


# ──────────────────────────────────────────────────────────────
# ৪. Telegram
# ──────────────────────────────────────────────────────────────
def tg(method, data=None, files=None, timeout=60):
    r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/{method}", data=data, files=files, timeout=timeout)
    try:
        j = r.json()
    except Exception:
        raise RuntimeError(f"Telegram {method}: অবোধ্য উত্তর ({r.status_code})")
    if not j.get("ok"):
        raise RuntimeError(f"Telegram {method}: {j.get('description')}")
    return j["result"]


def tg_text(text, reply_to=None):
    ids = []
    chunks = [text[i:i + 3900] for i in range(0, len(text), 3900)] or [""]
    for c in chunks:
        data = {"chat_id": TG_CHAT, "text": c, "disable_web_page_preview": "true"}
        if reply_to:
            data["reply_to_message_id"] = reply_to
            data["allow_sending_without_reply"] = "true"
        ids.append(tg("sendMessage", data)["message_id"])
    return ids


def draft_keyboard(did):
    return json.dumps({"inline_keyboard": [
        [{"text": "✅ পোস্ট করুন", "callback_data": f"a:{did}"},
         {"text": "❌ বাতিল", "callback_data": f"r:{did}"}],
        [{"text": "✏️ শিরোনাম এডিট", "callback_data": f"e:{did}"},
         {"text": "🔄 ছবি/কার্ড নতুন করে", "callback_data": f"n:{did}"}]]})


def tg_photo(img_bytes, caption="", markup=None):
    data = {"chat_id": TG_CHAT, "caption": caption[:1000]}
    if markup:
        data["reply_markup"] = markup
    res = tg("sendPhoto", data,
             files={"photo": ("card.jpg", img_bytes, "image/jpeg")})
    return res["message_id"], res["photo"][-1]["file_id"]


def tg_download_file(file_id):
    info = tg("getFile", {"file_id": file_id})
    r = requests.get(f"https://api.telegram.org/file/bot{TG_TOKEN}/{info['file_path']}", timeout=60)
    r.raise_for_status()
    return r.content


HELP_TEXT = (
    "📖 কমান্ড (ড্রাফটের মেসেজে রিপ্লাই করে লিখুন):\n"
    "• APPROVE — ফেসবুকে পোস্ট হবে\n"
    "• REJECT — বাতিল\n"
    "• EDIT: নতুন শিরোনাম — কার্ডের শিরোনাম বদলাবে (তারপর আবার APPROVE দিতে হবে)\n"
    "• EDITBODY: নতুন বিস্তারিত — কমেন্টের লেখা বদলাবে\n"
    "• STATUS — অপেক্ষমাণ ড্রাফটের তালিকা\n"
    "রিপ্লাই না করলে নম্বর দিন, যেমন: APPROVE 12  বা  EDIT 12: নতুন শিরোনাম"
)


def send_draft(d, img_bytes, st):
    """ড্রাফট Telegram-এ পাঠায় এবং message_id গুলো ম্যাপ করে।"""
    cap = (f"{d['caption'][:850]}\n\n"
           f"— ড্রাফট #{d['id']} · {d['category']} · সূত্র: {d['source']}")
    pid, file_id = tg_photo(img_bytes, cap, draft_keyboard(d["id"]))
    d["photo_file_id"] = file_id
    body_msg = (f"📄 কমেন্টে যা যাবে (ড্রাফট #{d['id']}):\n\n{d['body']}\n\n"
                f"🔗 মূল সূত্র (শুধু আপনার যাচাইয়ের জন্য): {d['link']}\n\n"
                f"👆 উপরের কার্ডের নিচের বাটন চাপুন।")
    bids = tg_text(body_msg, reply_to=pid)
    for mid in [pid] + bids:
        st["msg_map"][str(mid)] = d["id"]
        d["tg_msgs"] = d.get("tg_msgs", []) + [mid]


# ──────────────────────────────────────────────────────────────
# ৫. Facebook
# ──────────────────────────────────────────────────────────────
def fb_error(r):
    try:
        e = r.json().get("error", {})
        return f"{e.get('message')} (code {e.get('code')}, sub {e.get('error_subcode')})"
    except Exception:
        return f"HTTP {r.status_code}: {r.text[:200]}"


def fb_post_photo(img_bytes, caption):
    r = requests.post(f"https://graph.facebook.com/{GRAPH_VERSION}/{FB_PAGE}/photos",
                      data={"caption": caption, "published": "true", "access_token": FB_TOKEN},
                      files={"source": ("card.jpg", img_bytes, "image/jpeg")}, timeout=120)
    if r.status_code != 200:
        raise RuntimeError("Facebook পোস্ট ব্যর্থ: " + fb_error(r))
    j = r.json()
    return j.get("post_id") or j.get("id"), j.get("id")


def fb_comment(object_id, message):
    r = requests.post(f"https://graph.facebook.com/{GRAPH_VERSION}/{object_id}/comments",
                      data={"message": message[:7900], "access_token": FB_TOKEN}, timeout=60)
    if r.status_code != 200:
        raise RuntimeError("Facebook কমেন্ট ব্যর্থ: " + fb_error(r))
    return r.json().get("id")


def build_caption(d, cfg):
    parts = [d.get("intro") or d["headline"]]
    parts.append(f"📅 {bn_date_long()}")
    parts.append(cfg["caption_suffix"] + " 👇")
    tags = list(d.get("hashtags") or [])
    for t in cfg.get("fixed_hashtags", []):
        if t not in tags:
            tags.append(t)
    if tags:
        parts.append(" ".join(tags))
    return "\n\n".join(parts)


def comment_text(d, cfg):
    txt = f"{cfg.get('dateline', 'বগুড়া কথন অফিস প্রতিনিধি')} | {bn_date_long()}\n\n" + strip_links(d["body"])
    if cfg.get("credit_source_in_comment"):
        txt += f"\n\n(তথ্যসূত্র: {d['source']})"
    return txt


def publish(d, st, cfg):
    """অ্যাপ্রুভ হওয়া ড্রাফট ফেসবুকে পোস্ট করে এবং কমেন্টে বিস্তারিত দেয়।"""
    if not d.get("fb_post_id"):
        img = None
        if d.get("photo_file_id"):
            try:
                img = tg_download_file(d["photo_file_id"])
            except Exception as ex:
                log("  Telegram থেকে ছবি আনা যায়নি, নতুন করে বানানো হচ্ছে:", ex)
        if img is None:
            img = render_card(d["headline"], d["category"], download_image(d.get("image_url")), cfg)
        post_id, photo_id = fb_post_photo(img, d["caption"])
        d["fb_post_id"], d["fb_photo_id"] = post_id, photo_id
        d["status"] = "posted_no_comment"
        d["updated"] = iso(now_utc())
        save_state(st)  # পোস্ট হয়ে গেছে — সাথে সাথে সেভ (ডাবল পোস্ট ঠেকাতে)
    try:
        fb_comment(d["fb_post_id"], comment_text(d, cfg))
    except Exception as ex:
        # পোস্ট আইডি দিয়ে না হলে ফটো আইডি দিয়ে চেষ্টা
        if d.get("fb_photo_id") and d["fb_photo_id"] != d["fb_post_id"]:
            fb_comment(d["fb_photo_id"], comment_text(d, cfg))
        else:
            raise ex
    d["status"] = "posted"
    d["updated"] = iso(now_utc())


def fb_link(d):
    return f"https://www.facebook.com/{d['fb_post_id']}" if d.get("fb_post_id") else ""


# ──────────────────────────────────────────────────────────────
# ৬. অ্যাপ্রুভাল লজিক (Telegram রিপ্লাই প্রসেস)
# ──────────────────────────────────────────────────────────────
CMD_RE = re.compile(
    r"^\s*(editbody|edit|approve|reject|status|help|ok|অনুমোদন|বাতিল)(?=[\s:：\-#\d]|$)"
    r"(?:\s+#?(\d+))?\s*[:：]?\s*(.*)$", re.I | re.S)


def parse_command(text):
    """('approve', 12 বা None, 'অতিরিক্ত টেক্সট') অথবা None ফেরত দেয়।"""
    m = CMD_RE.match(text or "")
    if not m:
        return None
    cmd = m.group(1).lower()
    cmd = {"ok": "approve", "অনুমোদন": "approve", "বাতিল": "reject"}.get(cmd, cmd)
    did = m.group(2)
    rest = (m.group(3) or "").strip()
    # "APPROVE #12" ছাড়া "APPROVE 12" — ইতিমধ্যে ধরা হয়েছে; "reject 5 কারণ" -> rest="কারণ"
    return cmd, (int(did) if did else None), rest


def open_drafts(st):
    return [d for d in st["drafts"].values() if d["status"] in ("pending", "failed", "posted_no_comment")]


def resolve_draft(st, did_in_text, reply_to_id):
    if reply_to_id is not None and str(reply_to_id) in st["msg_map"]:
        return st["drafts"].get(str(st["msg_map"][str(reply_to_id)]))
    if did_in_text is not None:
        return st["drafts"].get(str(did_in_text))
    pend = [d for d in st["drafts"].values() if d["status"] == "pending"]
    if len(pend) == 1:
        return pend[0]
    return None


def handle_message(msg, st, cfg):
    text = (msg.get("text") or "").strip()
    if not text:
        return
    parsed = parse_command(text)
    mid = msg["message_id"]
    rt = (msg.get("reply_to_message") or {}).get("message_id")
    if not parsed and rt is not None and str(rt) in st.get("edit_wait", {}):
        did_w = st["edit_wait"].pop(str(rt))
        parsed = ("edit", int(did_w), text)
    if not parsed:
        tg_text("❓ বুঝতে পারিনি।\n\n" + HELP_TEXT, reply_to=mid)
        return
    cmd, did_in, rest = parsed

    if cmd == "help":
        tg_text(HELP_TEXT, reply_to=mid)
        return
    if cmd == "status":
        pend = sorted(open_drafts(st), key=lambda d: d["id"])
        if not pend:
            tg_text("✅ কোনো অপেক্ষমাণ ড্রাফট নেই।", reply_to=mid)
        else:
            lines = [f"#{d['id']} [{d['status']}] {d['headline'][:60]}" for d in pend]
            tg_text("📋 অপেক্ষমাণ ড্রাফট:\n" + "\n".join(lines), reply_to=mid)
        return

    reply_to = (msg.get("reply_to_message") or {}).get("message_id")
    d = resolve_draft(st, did_in, reply_to)
    if not d:
        pend = [x for x in st["drafts"].values() if x["status"] == "pending"]
        if not pend:
            tg_text("ℹ️ এখন কোনো অপেক্ষমাণ ড্রাফট নেই।", reply_to=mid)
        else:
            ids = ", ".join(f"#{x['id']}" for x in sorted(pend, key=lambda x: x["id"]))
            tg_text(f"কোন ড্রাফট? ড্রাফটের মেসেজে রিপ্লাই করুন, অথবা নম্বর দিন (যেমন: APPROVE {pend[0]['id']})।\n"
                    f"অপেক্ষমাণ: {ids}", reply_to=mid)
        return

    if d["status"] in ("posted", "rejected", "expired"):
        tg_text(f"ℹ️ ড্রাফট #{d['id']} আগেই {d['status']} হয়ে গেছে।", reply_to=mid)
        return

    if cmd == "reject":
        d["status"] = "rejected"
        d["updated"] = iso(now_utc())
        tg_text(f"❌ ড্রাফট #{d['id']} বাতিল করা হয়েছে। কিছু পোস্ট হয়নি।", reply_to=mid)
        return

    if cmd == "edit":
        if not rest:
            tg_text("✏️ নতুন শিরোনাম লিখুন, যেমন:\nEDIT: বগুড়ায় সড়ক দুর্ঘটনায় নিহত ২", reply_to=mid)
            return
        new_h = re.sub(r"\s+", " ", rest).strip()[:120]
        d["headline"] = new_h
        d["headline"] = new_h
        d["caption"] = build_caption(d, cfg)
        img = render_card(new_h, d["category"], download_image(d.get("image_url")), cfg)
        d["updated"] = iso(now_utc())
        tg_text(f"✏️ ড্রাফট #{d['id']} এডিট হয়েছে। নিচের নতুন কার্ড দেখে APPROVE দিন:", reply_to=mid)
        send_draft(d, img, st)
        return

    if cmd == "editbody":
        if len(rest) < 20:
            tg_text("✏️ নতুন বিস্তারিত লিখুন, যেমন:\nEDITBODY: এখানে পুরো নিউজ…", reply_to=mid)
            return
        d["body"] = strip_links(rest)
        d["updated"] = iso(now_utc())
        tg_text(f"✏️ ড্রাফট #{d['id']}-এর বিস্তারিত বদলানো হয়েছে।\n\n{d['body']}\n\nএবার APPROVE দিন।",
                reply_to=mid)
        return

    if cmd == "approve":
        tg_text(f"⏳ ড্রাফট #{d['id']} ফেসবুকে পোস্ট হচ্ছে…", reply_to=mid)
        try:
            publish(d, st, cfg)
        except Exception as ex:
            if d.get("fb_post_id"):
                d["status"] = "posted_no_comment"
                tg_text(f"⚠️ ড্রাফট #{d['id']}: পোস্ট হয়েছে কিন্তু কমেন্ট যায়নি।\n{ex}\n"
                        f"পরের রানে আবার চেষ্টা হবে।\n{fb_link(d)}")
            else:
                d["status"] = "failed"
                d["error"] = str(ex)[:300]
                tg_text(f"⚠️ ড্রাফট #{d['id']} পোস্ট হয়নি।\nকারণ: {ex}\n"
                        f"ঠিক করে আবার APPROVE লিখুন।")
            return
        tg_text(f"✅ ড্রাফট #{d['id']} ফেসবুকে পোস্ট হয়েছে (কমেন্টসহ)।\n{fb_link(d)}")


def tg_clear_buttons(msg):
    try:
        tg("editMessageReplyMarkup", {"chat_id": TG_CHAT, "message_id": msg["message_id"],
                                      "reply_markup": json.dumps({"inline_keyboard": []})})
    except Exception:
        pass


def handle_callback(cb, st, cfg):
    data = cb.get("data", "")
    msg = cb.get("message") or {}
    try:
        act, did = data.split(":", 1)
    except ValueError:
        tg("answerCallbackQuery", {"callback_query_id": cb["id"]})
        return
    d = st["drafts"].get(str(did))
    if not d:
        tg("answerCallbackQuery", {"callback_query_id": cb["id"], "text": "ড্রাফট পাওয়া যায়নি", "show_alert": "true"})
        return
    if d["status"] in ("posted", "rejected", "expired") and act != "n":
        tg("answerCallbackQuery", {"callback_query_id": cb["id"], "text": f"আগেই {d['status']}"})
        tg_clear_buttons(msg)
        return
    mid = msg.get("message_id")
    if act == "a":
        tg("answerCallbackQuery", {"callback_query_id": cb["id"], "text": "পোস্ট হচ্ছে…"})
        tg_clear_buttons(msg)
        handle_message({"message_id": mid, "text": f"APPROVE {did}", "reply_to_message": {"message_id": mid}}, st, cfg)
    elif act == "r":
        tg("answerCallbackQuery", {"callback_query_id": cb["id"], "text": "বাতিল"})
        tg_clear_buttons(msg)
        handle_message({"message_id": mid, "text": f"REJECT {did}", "reply_to_message": {"message_id": mid}}, st, cfg)
    elif act == "e":
        tg("answerCallbackQuery", {"callback_query_id": cb["id"]})
        ids = tg_text(f"✏️ ড্রাফট #{did}: এই মেসেজে রিপ্লাই করে নতুন শিরোনাম লিখুন।", reply_to=mid)
        for m in ids:
            st["msg_map"][str(m)] = d["id"]
            st.setdefault("edit_wait", {})[str(m)] = d["id"]
    elif act == "n":
        tg("answerCallbackQuery", {"callback_query_id": cb["id"], "text": "নতুন কার্ড বানানো হচ্ছে…"})
        d["variant"] = d.get("variant", 0) + 1
        photo = download_image(d.get("image_url"))
        img = render_card(d["headline"], d["category"], photo, cfg, variant=d["variant"])
        tg_clear_buttons(msg)
        send_draft(d, img, st)


def process_replies(st, cfg):
    try:
        updates = tg("getUpdates", {"offset": st["update_offset"], "timeout": 0,
                                    "allowed_updates": json.dumps(["message", "callback_query"])})
    except Exception as ex:
        log("Telegram আপডেট আনা যায়নি:", ex)
        return
    log(f"Telegram: {len(updates)}টি নতুন মেসেজ")
    for u in updates:
        st["update_offset"] = u["update_id"] + 1
        cb = u.get("callback_query")
        if cb:
            if str((cb.get("message") or {}).get("chat", {}).get("id")) != TG_CHAT:
                continue
            try:
                handle_callback(cb, st, cfg)
            except Exception as ex:
                log("  বাটন প্রসেসে ত্রুটি:", ex)
                try:
                    tg_text(f"⚠️ ত্রুটি: {str(ex)[:300]}")
                except Exception:
                    pass
            save_state(st)
            continue
        msg = u.get("message")
        if not msg:
            continue
        if str(msg.get("chat", {}).get("id")) != TG_CHAT:
            log("  অন্য চ্যাট থেকে মেসেজ — উপেক্ষা করা হলো")
            continue
        try:
            handle_message(msg, st, cfg)
        except Exception as ex:
            log("  মেসেজ প্রসেসে ত্রুটি:", ex)
            try:
                tg_text(f"⚠️ ত্রুটি: {str(ex)[:300]}", reply_to=msg["message_id"])
            except Exception:
                pass
        save_state(st)  # প্রতিটি মেসেজের পরেই সেভ


def retry_comments(st, cfg):
    for d in list(st["drafts"].values()):
        if d["status"] != "posted_no_comment" or not d.get("fb_post_id"):
            continue
        d["comment_tries"] = d.get("comment_tries", 0) + 1
        if d["comment_tries"] > 5:
            d["status"] = "posted"
            tg_text(f"⚠️ ড্রাফট #{d['id']}-এর কমেন্ট ৫ বার চেষ্টা করেও যায়নি। হাতে দিন:\n{fb_link(d)}\n\n{d['body']}")
            continue
        try:
            fb_comment(d["fb_post_id"], comment_text(d, cfg))
            d["status"] = "posted"
            d["updated"] = iso(now_utc())
            tg_text(f"✅ ড্রাফট #{d['id']}-এর কমেন্ট এবার গেছে।\n{fb_link(d)}")
        except Exception as ex:
            log("  কমেন্ট রিট্রাই ব্যর্থ:", ex)


def expire_old(st, cfg):
    cutoff = now_utc() - timedelta(hours=cfg["draft_expire_hours"])
    n = 0
    for d in st["drafts"].values():
        if d["status"] == "pending":
            t = parse_iso(d["created"])
            if t and t < cutoff:
                d["status"] = "expired"
                d["updated"] = iso(now_utc())
                n += 1
    if n:
        tg_text(f"⏰ {n}টি পুরনো ড্রাফটের মেয়াদ শেষ হয়ে গেছে (পোস্ট হয়নি)।")


# ──────────────────────────────────────────────────────────────
# ৭. নতুন ড্রাফট তৈরি
# ──────────────────────────────────────────────────────────────
def fetch_new_drafts(st, cfg, force=False):
    pending = [d for d in st["drafts"].values() if d["status"] == "pending"]
    last = parse_iso(st["last_fetch"]) if st["last_fetch"] else None
    due = force or last is None or (now_utc() - last) >= timedelta(minutes=cfg["fetch_every_minutes"] - 5)
    if not due:
        log("নতুন নিউজ আনার সময় হয়নি — শুধু রিপ্লাই চেক হলো")
        return
    today = dhaka_now().strftime("%Y-%m-%d")
    if st["daily"].get("date") != today:
        st["daily"] = {"date": today, "count": 0}
    h = dhaka_now().hour
    a0, a1 = cfg.get("active_hours", [0, 24])
    if not force and not (a0 <= h < a1):
        log(f"এখন সক্রিয় সময়ের বাইরে ({h}টা) — নতুন ড্রাফট হবে না")
        return
    cap_left = cfg.get("daily_draft_cap", 10) - st["daily"]["count"]
    if cap_left <= 0:
        log("আজকের ড্রাফট সীমা পূর্ণ")
        return
    room = min(cfg["max_drafts_per_run"], cfg["max_pending"] - len(pending), cap_left)
    if room <= 0:
        log(f"অপেক্ষমাণ ড্রাফট {len(pending)}টি — আগে সেগুলো APPROVE/REJECT করুন")
        return
    st["last_fetch"] = iso(now_utc())
    log("📡 নিউজ সংগ্রহ হচ্ছে…")
    items = collect_items(cfg)
    ranked = rank_items(items, cfg, st)
    log(f"নতুন ও ইউনিক খবর: {len(ranked)}টি")
    created, tried = 0, 0
    for it in ranked:
        if created >= room or tried >= 10:
            break
        tried += 1
        log(f"▶ [{it['source']}] {it['title'][:70]}")
        art = fetch_article(it["link"])
        text = max([art["text"], it["content"], it["summary"]], key=len)
        if len(text) < 250:
            log("   তথ্য কম — বাদ")
            mark_seen(it, st)
            continue
        try:
            ai = validate_ai(gemini_rewrite(it["title"], text), cfg)
        except GeminiQuotaError as ex:
            log("   Gemini লিমিট:", ex)
            break
        except Exception as ex:
            log("   AI ত্রুটি:", ex)
            continue
        mark_seen(it, st)
        if not ai:
            log("   AI বাদ দিয়েছে/অসম্পূর্ণ")
            continue
        image_url = it["image"] or art["image"]
        photo = download_image(art["image"]) or download_image(it["image"])
        if photo is None:
            image_url = None
        did = st["next_id"]
        st["next_id"] += 1
        d = {
            "id": did, "status": "pending", "created": iso(now_utc()), "updated": iso(now_utc()),
            "source": it["source"], "link": it["link"], "orig_title": it["title"],
            "headline": ai["headline"], "category": ai["category"], "body": ai["body"],
            "intro": ai.get("intro", ""), "hashtags": ai.get("hashtags", []),
            "image_url": image_url,
        }
        d["caption"] = build_caption(d, cfg)
        st["drafts"][str(did)] = d
        try:
            img = render_card(d["headline"], d["category"], photo, cfg)
            send_draft(d, img, st)
            created += 1
            st["daily"]["count"] += 1
            log(f"   ✅ ড্রাফট #{did} Telegram-এ গেছে")
        except Exception as ex:
            log("   ড্রাফট পাঠানো ব্যর্থ:", ex)
            d["status"] = "failed_to_send"
            d["updated"] = iso(now_utc())
        save_state(st)
    log(f"মোট নতুন ড্রাফট: {created}টি")


# ──────────────────────────────────────────────────────────────
# ৮. চেক মোড (সব সেটিং পরীক্ষা)
# ──────────────────────────────────────────────────────────────
SAMPLE_HEADLINE = "বগুড়ায় নতুন সেতু উদ্বোধন, যান চলাচল শুরু আজ থেকেই"


def run_check(cfg):
    ok_all = True

    def line(ok, msg):
        nonlocal ok_all
        ok_all = ok_all and ok
        log(("✅ " if ok else "❌ ") + msg)

    log("──── সেটিং পরীক্ষা ────")
    for name, val in (("FACEBOOK_PAGE_ID", FB_PAGE), ("FACEBOOK_ACCESS_TOKEN", FB_TOKEN),
                      ("TELEGRAM_BOT_TOKEN", TG_TOKEN), ("TELEGRAM_CHAT_ID", TG_CHAT),
                      ("GEMINI_API_KEY", GEMINI_KEY)):
        line(bool(val), f"Secret {name}: {'আছে' if val else 'নেই — GitHub Secrets-এ যোগ করুন'}")

    line(features.check("raqm"), "বাংলা টেক্সট ইঞ্জিন (raqm): " + ("চালু" if features.check("raqm") else "বন্ধ — বাংলা ভাঙা দেখাবে"))
    try:
        ensure_fonts()
        line(True, f"ফন্ট: {os.path.basename(_FONT_PATHS['bold'])}")
    except Exception as ex:
        line(False, f"ফন্ট: {ex}")
    line(os.path.exists(LOGO_PATH), "লোগো assets/logo.png: " + ("আছে" if os.path.exists(LOGO_PATH) else
         "নেই — সাময়িক মাইক্রোফোন আইকন ব্যবহার হচ্ছে (আপনার লোগো আপলোড করুন)"))

    if TG_TOKEN:
        try:
            me = tg("getMe")
            line(True, f"Telegram বট: @{me.get('username')}")
        except Exception as ex:
            line(False, f"Telegram টোকেন: {ex}")

    if FB_PAGE and FB_TOKEN:
        try:
            r = requests.get(f"https://graph.facebook.com/{GRAPH_VERSION}/{FB_PAGE}",
                             params={"fields": "name,fan_count", "access_token": FB_TOKEN}, timeout=30)
            if r.status_code == 200:
                j = r.json()
                line(True, f"Facebook পেজ: {j.get('name')} (ফলোয়ার/লাইক: {j.get('fan_count', '?')})")
            else:
                line(False, "Facebook টোকেন/পেজ আইডি: " + fb_error(r))
        except Exception as ex:
            line(False, f"Facebook: {ex}")

    if GEMINI_KEY:
        try:
            out = gemini_rewrite("বগুড়ায় সেতু উদ্বোধন",
                                 "বগুড়ায় একটি নতুন সেতুর উদ্বোধন করা হয়েছে। সেতুটি চালু হওয়ায় দুই পাড়ের মানুষের যাতায়াত সহজ হবে বলে স্থানীয়রা জানিয়েছেন। " * 4)
            line(True, "Gemini AI: কাজ করছে" + (" (skip দিয়েছে — স্বাভাবিক)" if out.get("skip") else ""))
        except Exception as ex:
            line(False, f"Gemini AI: {ex}")

    log("──── RSS ফিড ────")
    good = 0
    for feed in cfg["feeds"]:
        if not feed.get("enabled", True):
            log(f"⏸ {feed['name']}: বন্ধ করা আছে")
            continue
        try:
            n = len(fetch_feed(feed))
            if n:
                good += 1
            log(("✅ " if n else "⚠️ ") + f"{feed['name']}: {n}টি খবর" + ("" if n else " (খালি)"))
        except Exception as ex:
            log(f"❌ {feed['name']}: {str(ex)[:90]}  → config.json-এ এটি \"enabled\": false করুন")
    line(good > 0, f"কাজ করা ফিড: {good}টি")

    if TG_TOKEN and TG_CHAT:
        try:
            card = render_card(SAMPLE_HEADLINE, "বগুড়া", None, cfg)
            tg_photo(card, "🧪 টেস্ট কার্ড — বাংলা লেখা ঠিক দেখাচ্ছে কিনা দেখুন।")
            tg_text("✅ টেস্ট সফল! এই চ্যাটেই ড্রাফট আসবে।\n\n" + HELP_TEXT)
            line(True, "Telegram-এ টেস্ট মেসেজ ও কার্ড গেছে — আপনার ফোন দেখুন")
        except Exception as ex:
            line(False, f"Telegram চ্যাট আইডি/মেসেজ: {ex}")
    log("──── ফলাফল: " + ("সব ঠিক আছে 🎉" if ok_all else "কিছু সমস্যা আছে — ❌ লাইনগুলো ঠিক করুন") + " ────")
    return ok_all


# ──────────────────────────────────────────────────────────────
# মেইন
# ──────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--force-fetch", action="store_true")
    ap.add_argument("--test-card", metavar="PATH")
    args = ap.parse_args()
    cfg = load_config()

    if args.test_card:
        ensure_fonts()
        with open(args.test_card, "wb") as f:
            f.write(render_card(SAMPLE_HEADLINE, "বগুড়া", None, cfg))
        log("কার্ড সেভ হয়েছে:", args.test_card)
        return 0

    if args.check:
        return 0 if run_check(cfg) else 1

    missing = [n for n, v in (("TELEGRAM_BOT_TOKEN", TG_TOKEN), ("TELEGRAM_CHAT_ID", TG_CHAT),
                              ("FACEBOOK_PAGE_ID", FB_PAGE), ("FACEBOOK_ACCESS_TOKEN", FB_TOKEN),
                              ("GEMINI_API_KEY", GEMINI_KEY)) if not v]
    if missing:
        log("❌ এই Secrets নেই:", ", ".join(missing))
        return 1

    ensure_fonts()
    st = load_state()
    process_replies(st, cfg)
    expire_old(st, cfg)
    retry_comments(st, cfg)
    save_state(st)
    try:
        fetch_new_drafts(st, cfg, force=args.force_fetch)
    finally:
        save_state(st)
    return 0


if __name__ == "__main__":
    sys.exit(main())
