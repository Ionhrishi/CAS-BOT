"""
CAS Bot - opportunities for faculty development & CAS promotion.

7:00 AM IST  -> scans Google News, Google Alerts, Telegram channels, watched pages,
                filters + extracts details, and puts them in a queue.
8 AM - 8 PM  -> every 2 hours sends a few items from the queue, one short message each,
                so alerts are spread through the day.
State (sent items + queue) is kept in seen.json.
"""
import hashlib
import html
import json
import math
import os
import re
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

import feedparser
import requests

# ---------------- settings ----------------
SEEN_FILE = "seen.json"
QUERIES_FILE = "queries.txt"
FEEDS_FILE = "feeds.txt"
PAGES_FILE = "watch_pages.txt"
TG_CHANNELS_FILE = "telegram_channels.txt"

SCAN_CRON = "30 1 * * *"                    # must match the first cron in the workflow
SEND_HOURS_IST = [8, 10, 12, 14, 16, 18, 20]  # must match the second cron in the workflow
MAX_PER_SLOT = 8                            # at most this many messages per sending time
MAX_AGE_DAYS = int(os.getenv("MAX_AGE_DAYS", "7"))
MIN_SCORE = int(os.getenv("MIN_SCORE", "3"))
QUEUE_KEEP_DAYS = 3                         # drop unsent items older than this

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")
WA_PHONE = os.getenv("WHATSAPP_PHONE", "")
WA_KEY = os.getenv("CALLMEBOT_APIKEY", "")
GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest")
CLAUDE_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-5-5")
SCHEDULE = os.getenv("SCHEDULE", "")        # filled by GitHub (which cron started this run)

PROFILE = """Assistant Professor of Chemistry at an autonomous college in Thane, Maharashtra,
affiliated to University of Mumbai. PhD in chemistry. Preparing for Career Advancement Scheme (CAS)
promotion under UGC Regulations 2018. Research interests: heterogeneous and green catalysis,
photocatalysis (doped CeO2, g-C3N4), nanomaterials, MXenes, biocatalysis, synthesis of
heterocycles, PET plastic waste recycling. Also coordinates NSS / outreach activities."""

CATEGORIES = [
    "Training (FIP/Refresher/FDP/STC)",
    "Workshop / Lecture series / Webinar",
    "Conference",
    "Journal / Call for papers",
    "Book chapter / Editor / Reviewer",
    "APC waiver / discount",
    "Patent / IPR",
    "Research grant / Project",
    "Travel grant",
    "MOOC / e-content creation",
    "Consultancy / Industry collaboration",
    "Award / Fellowship",
    "Other",
]

KEYWORDS = {
    "Travel grant": ["travel grant", "travel support", "international travel", "travel fellowship",
        "conference travel", "itsg", "ctep"],
    "MOOC / e-content creation": ["mooc", "swayam course", "e-content", "econtent", "nptel",
        "course coordinator", "e-pg pathshala", "cec ", "course development", "online course proposal"],
    "Consultancy / Industry collaboration": ["industry-academia", "industry academia",
        "industry collaboration", "industry partnership", "consultancy", "sponsored research",
        "testing services", "technology transfer", "csr funding", "industry-sponsored"],
    "Training (FIP/Refresher/FDP/STC)": ["faculty induction", "refresher", "faculty development",
        "fdp", "short term course", "short-term course", "guru dakshta", "guru dakshata",
        "malaviya", "mmttc", "mmttp", "msfda", "atal fdp", "orientation programme"],
    "Book chapter / Editor / Reviewer": ["call for chapters", "call for book chapters",
        "book chapter", "chapter proposal", "edited volume", "edited book", "book proposal",
        "call for editors", "guest editor", "editorial board", "call for reviewers",
        "reviewer board", "become a reviewer", "topic editor", "volume editor"],
    "Patent / IPR": ["patent", "ipr", "intellectual property", "kapila", "nipam",
        "design registration", "patent drafting", "patent filing"],
    "Workshop / Lecture series / Webinar": ["workshop", "lecture series", "webinar", "hands-on training"],
    "Conference": ["conference", "symposium", "seminar", "congress"],
    "Journal / Call for papers": ["call for papers", "special issue", "call for submissions"],
    "APC waiver / discount": ["apc waiver", "article processing charge", "apc discount", "fee waiver"],
    "Research grant / Project": ["call for proposals", "research grant", "minor research project",
        "major research project", "research project", "funding opportunity", "serb", "anrf"],
    "Award / Fellowship": ["award", "fellowship"],
}

EMOJI = {
    "Training (FIP/Refresher/FDP/STC)": "🎓", "Workshop / Lecture series / Webinar": "🧪",
    "Conference": "🌐", "Journal / Call for papers": "📄", "Book chapter / Editor / Reviewer": "📚",
    "APC waiver / discount": "💸", "Patent / IPR": "💡", "Research grant / Project": "💰",
    "Travel grant": "✈️", "MOOC / e-content creation": "🖥️",
    "Consultancy / Industry collaboration": "🤝", "Award / Fellowship": "🏆", "Watched page updated": "🔄", "Other": "📌",
}

HEADERS = {"User-Agent": "Mozilla/5.0 (CAS-Bot personal alert script)"}
IST = timezone(timedelta(hours=5, minutes=30))
UNKNOWN = "Check link"


# ---------------- small helpers ----------------
def read_list(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [l.strip() for l in f if l.strip() and not l.strip().startswith("#")]


def load_state():
    data = {}
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, encoding="utf-8") as f:
            data = json.load(f)
    for k, v in (("items", {}), ("pages", {}), ("queue", [])):
        data.setdefault(k, v)
    return data


def save_state(st):
    now = datetime.now(timezone.utc)
    cut_seen = (now - timedelta(days=90)).isoformat()
    cut_queue = (now - timedelta(days=QUEUE_KEEP_DAYS)).isoformat()
    st["items"] = {k: v for k, v in st["items"].items() if v >= cut_seen}
    st["queue"] = [q for q in st["queue"] if q.get("added", "") >= cut_queue]
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=1, ensure_ascii=False)


def clean_title(t):
    t = re.sub(r"\s+", " ", html.unescape(t or "")).strip()
    return re.sub(r"\s+-\s+[^-]{2,60}$", "", t)


def strip_tags(s):
    s = re.sub(r"<(script|style).*?</\1>", " ", s or "", flags=re.S | re.I)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def key_for(title):
    norm = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
    return hashlib.sha1(norm.encode()).hexdigest()[:16]


def real_link(link):
    """Google Alerts links are google.com/url?...&url=REAL -> return REAL."""
    if "google.com/url" in link:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(link).query)
        return q.get("url", q.get("q", [link]))[0]
    return link


def entry_date(e):
    for k in ("published_parsed", "updated_parsed"):
        if e.get(k):
            return datetime(*e[k][:6], tzinfo=timezone.utc)
    return None


def gnews_url(q):
    return "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": f"{q} when:{MAX_AGE_DAYS}d", "hl": "en-IN", "gl": "IN", "ceid": "IN:en"})


# ---------------- collecting ----------------
def collect_from_feed(url, label):
    out = []
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        feed = feedparser.parse(r.content)
    except Exception as ex:
        print("Feed error:", label, ex)
        return out
    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
    for e in feed.entries[:25]:
        d = entry_date(e)
        if d and d < cutoff:
            continue
        title = clean_title(e.get("title", ""))
        if not title:
            continue
        src = e.get("source", {})
        out.append({"title": title, "link": real_link(e.get("link", "")),
                    "source": src.get("title", "") if hasattr(src, "get") else "",
                    "snippet": strip_tags(e.get("summary", ""))[:400], "via": label})
    return out


def collect_telegram(channel):
    channel = channel.strip().lstrip("@").replace("https://t.me/", "").replace("s/", "").strip("/")
    out = []
    try:
        r = requests.get(f"https://t.me/s/{channel}", headers=HEADERS, timeout=30)
    except Exception as ex:
        print("Telegram channel error:", channel, ex)
        return out
    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
    for block in r.text.split('class="tgme_widget_message_wrap')[1:]:
        post = re.search(r'data-post="([^"]+)"', block)
        when = re.search(r'<time[^>]*datetime="([^"]+)"', block)
        body = re.search(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', block, re.S)
        if not (post and body):
            continue
        if when:
            try:
                if datetime.fromisoformat(when.group(1)) < cutoff:
                    continue
            except ValueError:
                pass
        text = strip_tags(re.sub(r"<br\s*/?>", " ", body.group(1)))
        if len(text) < 20:
            continue
        out.append({"title": text[:140] + ("…" if len(text) > 140 else ""),
                    "link": f"https://t.me/{post.group(1)}", "source": f"Telegram @{channel}",
                    "snippet": text[:1500], "via": "telegram"})
    return out[-20:]


def check_pages(st):
    changed = []
    for line in read_list(PAGES_FILE):
        name, _, url = line.partition("|")
        if not url:
            name, url = "", name
        name, url = name.strip(), url.strip()
        try:
            text = strip_tags(requests.get(url, headers=HEADERS, timeout=30).text)
        except Exception as ex:
            print("Page error:", url, ex)
            continue
        h = hashlib.sha1(text.encode()).hexdigest()
        old = st["pages"].get(url)
        if old and old != h:
            changed.append(new_card(name or url, url, "Watched page updated", 3,
                                    summary="New notice/update on this page"))
        st["pages"][url] = h
    return changed


def page_text(link):
    if "t.me/" in link:
        return ""
    try:
        r = requests.get(link, headers=HEADERS, timeout=20, allow_redirects=True)
        if "news.google.com" in r.url or "html" not in r.headers.get("content-type", ""):
            return ""
        return strip_tags(r.text)[:3000]
    except Exception:
        return ""


# ---------------- details extraction ----------------
def new_card(name, link, category, score, **kw):
    card = {"name": name, "link": link, "category": category, "score": score,
            "organiser": "", "mode": "", "location": "", "fee": "", "deadline": "",
            "dates": "", "summary": ""}
    card.update({k: v for k, v in kw.items() if v})
    return card


MONTH = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*"
DATE = (rf"(\d{{1,2}}(st|nd|rd|th)?[\s\-]*{MONTH}[,\s\-]*(\d{{4}})?|{MONTH}\s+\d{{1,2}}(st|nd|rd|th)?,?\s*(\d{{4}})?"
        r"|\d{1,2}[./-]\d{1,2}[./-]\d{2,4})")


def regex_fill(card, text):
    t = (text or "").lower()
    if not card["mode"]:
        if re.search(r"\bhybrid\b", t):
            card["mode"] = "Hybrid"
        elif re.search(r"\b(online|virtual|webinar|zoom|google meet|ms teams)\b", t):
            card["mode"] = "Online"
        elif re.search(r"\b(offline|in-person|in person|physical mode|venue)\b", t):
            card["mode"] = "Offline"
    if not card["fee"]:
        m = re.search(r"(₹|rs\.?|inr)\s?[\d,]{2,}(/-)?|(usd|us\$|\$)\s?[\d,]+", t)
        if m:
            amt = re.search(r"[\d,]+", m.group(0)).group(0).strip(",")
            cur = "$" if re.match(r"(usd|us\$|\$)", m.group(0)) else "₹"
            card["fee"] = f"{cur}{amt}"
        elif re.search(r"\b(free of cost|no registration fee|free registration|no fee|free)\b", t):
            card["fee"] = "Free"
    if not card["deadline"]:
        m = re.search(r"(last date|deadline|register by|apply by|registration closes|submission[^.]{0,25}?by|"
                      r"last day)[^0-9a-z]{0,15}" + DATE, t)
        if m:
            d = re.search(DATE, m.group(0)[len(m.group(1)):]).group(0).strip(" ,")
            card["deadline"] = re.sub(r"(\d)(St|Nd|Rd|Th)\b", lambda x: x.group(1) + x.group(2).lower(), d.title())
    return card


def ask_ai(prompt, tries=2):
    for attempt in range(tries):
        try:
            return _ask_ai_once(prompt)
        except Exception as ex:
            print(f"AI attempt {attempt + 1} failed:", ex)
            if attempt + 1 == tries:
                raise
            time.sleep(20)


def _ask_ai_once(prompt):
    if GEMINI_KEY:
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
            headers={"x-goog-api-key": GEMINI_KEY, "content-type": "application/json"},
            json={"contents": [{"parts": [{"text": prompt}]}],
                  "generationConfig": {"responseMimeType": "application/json"}},
            timeout=240)
        r.raise_for_status()
        time.sleep(15)  # stay inside free-tier limits
        return "".join(p.get("text", "") for p in r.json()["candidates"][0]["content"]["parts"])
    r = requests.post("https://api.anthropic.com/v1/messages",
                      headers={"x-api-key": CLAUDE_KEY, "anthropic-version": "2023-06-01",
                               "content-type": "application/json"},
                      json={"model": CLAUDE_MODEL, "max_tokens": 3000,
                            "messages": [{"role": "user", "content": prompt}]}, timeout=120)
    r.raise_for_status()
    return "".join(b.get("text", "") for b in r.json().get("content", []))


def parse_json_list(text):
    m = re.search(r"\[.*\]", text or "", re.S)
    try:
        return json.loads(m.group(0)) if m else []
    except json.JSONDecodeError:
        return []


FIELDS_SPEC = '''"name": "<short clear name of the opportunity, max 12 words>",
"organiser": "<organising body, short, or empty>",
"mode": "<Online | Offline | Hybrid | empty if unknown>",
"location": "<city if offline/hybrid, else empty>",
"fee": "<Free | amount with currency e.g. ₹1,000 | empty if unknown>",
"deadline": "<last date to apply/register/submit, e.g. 10 Nov 2026, or empty>",
"dates": "<event/programme dates if given, or empty>"'''


def keyword_classify(items):
    cards = []
    for it in items:
        text = (it["title"] + " " + it["snippet"]).lower()
        for cat, words in KEYWORDS.items():
            if any(w in text for w in words):
                c = new_card(it["title"], it["link"], cat, 3)
                c["_text"] = it["snippet"]
                cards.append(c)
                break
    return cards


def ai_classify(items):
    cards = []
    for start in range(0, len(items), 15):
        batch = items[start:start + 15]
        lines = [f"{i}. {it['title']} | {it['source']} | {it['snippet'][:250]}" for i, it in enumerate(batch)]
        prompt = f"""You filter opportunity alerts for this user:
{PROFILE}

Keep only real, current opportunities useful for this user's professional development or CAS
promotion: faculty induction / refresher / FDP / short-term courses, workshops, lecture series,
chemistry or interdisciplinary conferences, calls for papers or special issues, calls for book
chapters / edited volumes / book proposals, calls for guest editors, editorial board members or
reviewers, APC waivers, patent / IPR opportunities (filing support schemes, IPR or patent drafting
workshops, KAPILA, NIPAM), research project grants from ANY government body (central or state,
e.g. ANRF/SERB, DST, DBT, CSIR, ICMR, DAE-BRNS, DRDO, ISRO, MoES, MeitY, AICTE, UGC, PM-USHA/RUSA,
Maharashtra govt / RGSTC), university or publisher grants, travel grants for international
conferences, calls to create MOOCs / SWAYAM / NPTEL / e-content courses, industry-academia
collaboration / consultancy / sponsored research / testing tie-ups, MSFDA programmes, awards,
fellowships. Reject general news, results, student admissions, school-level items, and anything
not useful to a chemistry faculty member in India.

Categories: {" ; ".join(CATEGORIES)}

Return ONLY a JSON array. One object per KEPT item:
{{"i": <number>, "category": "<one category exactly as listed>", "score": <1-5 usefulness>,
{FIELDS_SPEC}}}
Use empty strings when information is not given. Do not guess.

Items:
""" + "\n".join(lines)
        try:
            results = parse_json_list(ask_ai(prompt))
        except Exception as ex:
            print("AI error, keyword filter used for this batch:", ex)
            cards += keyword_classify(batch)
            continue
        for res in results:
            try:
                it = batch[int(res["i"])]
                score = int(res.get("score", 3))
            except (KeyError, ValueError, IndexError, TypeError):
                continue
            if score < MIN_SCORE:
                continue
            cat = res.get("category", "Other")
            c = new_card(str(res.get("name") or it["title"]), it["link"],
                         cat if cat in CATEGORIES else "Other", score,
                         **{k: str(res.get(k) or "") for k in
                            ("organiser", "mode", "location", "fee", "deadline", "dates")})
            c["_text"] = it["snippet"]
            cards.append(c)
    return cards


def enrich(cards):
    """Open each item's web page and fill in mode / fee / deadline where missing."""
    need_ai = []
    for c in cards[:40]:
        text = c.get("_text", "")
        if not all(c[k] for k in ("mode", "fee", "deadline")):
            text = (text + " " + page_text(c["link"])).strip()
            c["_text"] = text
        regex_fill(c, text)
        if (GEMINI_KEY or CLAUDE_KEY) and len(text) > 200 and not all(c[k] for k in ("mode", "fee", "deadline")):
            need_ai.append(c)
    for start in range(0, len(need_ai), 5):
        batch = need_ai[start:start + 5]
        blocks = [f"### {i}\nTitle: {c['name']}\nText: {c['_text'][:1500]}" for i, c in enumerate(batch)]
        prompt = ("Extract details for each opportunity below. Return ONLY a JSON array of objects:\n"
                  f'{{"i": <number>, {FIELDS_SPEC}}}\n'
                  "Use empty strings when not stated. Do not guess.\n\n" + "\n\n".join(blocks))
        try:
            for res in parse_json_list(ask_ai(prompt)):
                c = batch[int(res["i"])]
                for k in ("organiser", "mode", "location", "fee", "deadline", "dates"):
                    if res.get(k) and not c[k]:
                        c[k] = str(res[k])
        except Exception as ex:
            print("Detail AI error:", ex)
    for c in cards:
        c.pop("_text", None)
    return cards


# ---------------- sending ----------------
def esc(s):
    return html.escape(str(s or ""))


def format_card(c):
    lines = [f"{EMOJI.get(c['category'], '📌')} <b>{esc(c['name'])}</b>"]
    if c.get("organiser"):
        lines.append(f"🏛 {esc(c['organiser'])}")
    mode = c.get("mode") or UNKNOWN
    if c.get("location") and mode != "Online":
        mode += f" – {c['location']}"
    lines.append(f"📍 {esc(mode)}   💰 {esc(c.get('fee') or UNKNOWN)}")
    lines.append(f"⏰ Deadline: {esc(c.get('deadline') or UNKNOWN)}")
    if c.get("dates"):
        lines.append(f"📅 {esc(c['dates'])}")
    if c.get("summary"):
        lines.append(f"<i>{esc(c['summary'])}</i>")
    lines.append(f'🔗 <a href="{html.escape(c["link"], quote=True)}">Open link</a>')
    return "\n".join(lines)


def send_telegram(text):
    """Returns True if the message was delivered."""
    if not (TG_TOKEN and TG_CHAT):
        print(text, "\n")
        return False
    r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      data={"chat_id": TG_CHAT, "text": text[:4000], "parse_mode": "HTML",
                            "disable_web_page_preview": "true"}, timeout=30)
    if not r.ok:
        print("Telegram error:", r.text)
    time.sleep(2)
    return r.ok


def send_whatsapp(cards):
    if not (WA_PHONE and WA_KEY and cards):
        return
    parts = []
    for c in cards:
        parts.append(f"{EMOJI.get(c['category'], '📌')} *{c['name']}*\n"
                     f"{c.get('mode') or UNKNOWN} | {c.get('fee') or UNKNOWN} | "
                     f"Deadline: {c.get('deadline') or UNKNOWN}\n{c['link']}")
    try:
        requests.get("https://api.callmebot.com/whatsapp.php?" + urllib.parse.urlencode(
            {"phone": WA_PHONE, "text": "\n\n".join(parts), "apikey": WA_KEY}), timeout=30)
    except Exception as ex:
        print("WhatsApp error:", ex)


# ---------------- the two jobs ----------------
def scan(st):
    raw = []
    for q in read_list(QUERIES_FILE):
        raw += collect_from_feed(gnews_url(q), q)
        time.sleep(1)
    for f in read_list(FEEDS_FILE):
        raw += collect_from_feed(f, "feed")
        time.sleep(1)
    for ch in read_list(TG_CHANNELS_FILE):
        raw += collect_telegram(ch)
        time.sleep(1)

    new, keys = [], set()
    for it in raw:
        k = key_for(it["title"])
        if k in st["items"] or k in keys:
            continue
        keys.add(k)
        new.append(it)
    print(f"Fetched {len(raw)}, new {len(new)}")

    cards = ai_classify(new) if (GEMINI_KEY or CLAUDE_KEY) else keyword_classify(new)
    cards = enrich(cards) + check_pages(st)
    now = datetime.now(timezone.utc).isoformat()
    for k in keys:
        st["items"][k] = now
    for c in cards:
        c["added"] = now
    st["queue"] += cards
    st["queue"].sort(key=lambda c: -c.get("score", 0))
    print(f"Queued {len(cards)} (queue now {len(st['queue'])})")


def deliver(st):
    if not st["queue"]:
        print("Queue empty")
        return
    hour = datetime.now(IST).hour
    slots_left = max(1, len([h for h in SEND_HOURS_IST if h >= hour]))
    n = min(MAX_PER_SLOT, math.ceil(len(st["queue"]) / slots_left))
    batch, rest = st["queue"][:n], st["queue"][n:]
    sent, failed = [], []
    for c in batch:
        (sent if send_telegram(format_card(c)) else failed).append(c)
    st["queue"] = failed + rest          # failed ones are kept and retried next time
    send_whatsapp(sent)
    print(f"Sent {len(sent)}, failed {len(failed)}, left {len(st['queue'])}")


def main():
    st = load_state()
    if SCHEDULE in ("", SCAN_CRON):   # 7 AM run, or manual run
        scan(st)
    if SCHEDULE != SCAN_CRON:         # sending runs, or manual run (sends first batch)
        deliver(st)
    save_state(st)


if __name__ == "__main__":
    main()
