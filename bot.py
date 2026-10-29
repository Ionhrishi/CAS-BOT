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
MIN_DETAILS = 1                             # need at least this many of: mode, fee, deadline, dates, venue
QUEUE_KEEP_DAYS = 3                         # drop unsent items older than this

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")
WA_PHONE = os.getenv("WHATSAPP_PHONE", "")
WA_KEY = os.getenv("CALLMEBOT_APIKEY", "")
GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "") or "gemini-2.5-flash-lite"
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
    # anything else (awards, news, general items) is not sent
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
    "Workshop / Lecture series / Webinar": ["workshop", "lecture series", "webinar", "hands-on training",
        "seminar"],
    "Conference": ["conference", "symposium", "congress"],
    "Journal / Call for papers": ["call for papers", "special issue", "call for submissions"],
    "APC waiver / discount": ["apc waiver", "article processing charge", "apc discount", "fee waiver"],
    "Research grant / Project": ["call for proposals", "research grant", "minor research project",
        "major research project", "research project", "funding opportunity", "serb", "anrf"],
}

EMOJI = {
    "Training (FIP/Refresher/FDP/STC)": "🎓", "Workshop / Lecture series / Webinar": "🧪",
    "Conference": "🌐", "Journal / Call for papers": "📄", "Book chapter / Editor / Reviewer": "📚",
    "APC waiver / discount": "💸", "Patent / IPR": "💡", "Research grant / Project": "💰",
    "Travel grant": "✈️", "MOOC / e-content creation": "🖥️",
    "Consultancy / Industry collaboration": "🤝", "Award / Fellowship": "🏆", "Watched page updated": "🔄", "Other": "📌",
}

# ---- strict filter rules (used in both AI and keyword mode) ----
# 1) News reports about things that already happened -> always rejected
NEWS_BLOCK = re.compile(
    r"\b(wins?|won|bags?|bagged|clinch\w*|secures?d?|receives?d?|received|awarded|felicitat\w*|honou?red|"
    r"conferred|congratulat\w*|appointed|elected|nominated as|takes charge|joins|"
    r"signs?|signed|mou|ties? up|tie-up with|partners with|inaugurat\w*|held|concluded|concludes|"
    r"organi[sz]ed|conducted|celebrat\w*|observed|marks|visits?|visited|addresse[sd]|"
    r"passes away|dies|died|obituary|ranked|rankings?|nirf|naac|accredit\w*|results?|"
    r"admissions?|students?|pupils|school|cbse|neet|jee|exam|placements?|alumni|convocation|fest|"
    r"arrested|scam|protest|controversy|alleg\w*|says|said|urges|calls on)\b", re.I)

# 2) Must look like an OPEN opportunity you can still act on
OPEN_CALL = re.compile(
    r"(call for|calls for|invit\w*|apply|applications?|register|registration|enrol\w*|last date|"
    r"deadline|submit|submission|proposals?|nominations?|abstracts?|open(s|ed)? for|now open|"
    r"upcoming|announce\w*|scheme|opportunit\w*|seeks|seeking|vacanc\w*|join us|participat\w*)", re.I)

# 3) For these categories the topic must be chemistry / allied science
SUBJECT_NEEDED = {"Conference", "Journal / Call for papers", "Book chapter / Editor / Reviewer",
                  "Workshop / Lecture series / Webinar", "Award / Fellowship",
                  "Consultancy / Industry collaboration"}
SUBJECT = re.compile(
    r"(chemi|catal|nano|material|polymer|spectro|electrochem|photo|green chem|sustainable chem|"
    r"analytical|organic|inorganic|physical science|pharma|drug|synthes|mxene|graphene|"
    r"crystal|corrosion|surface science|interdisciplinary|multidisciplinary|science and technology|"
    r"applied science|basic science)", re.I)


def has_details(c):
    if c.get("category") == "Watched page updated":
        return True
    return sum(bool(c.get(k)) for k in ("mode", "fee", "deadline", "dates", "venue")) >= MIN_DETAILS


def passes_rules(category, title, text=""):
    if NEWS_BLOCK.search(title or ""):
        return False
    if not OPEN_CALL.search(f"{title} {text}"):
        return False
    if category in SUBJECT_NEEDED and not SUBJECT.search(f"{title} {text}"):
        return False
    return True


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
        photo = re.search(r"tgme_widget_message_photo_wrap[^>]*background-image:url\('([^']+)'\)", block)
        out.append({"title": text[:140] + ("…" if len(text) > 140 else ""),
                    "link": f"https://t.me/{post.group(1)}", "source": f"Telegram @{channel}",
                    "snippet": text[:1500], "via": "telegram",
                    "image": photo.group(1) if photo else ""})
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


def page_info(link):
    """Open the opportunity's page: returns (text, banner image url, brochure url)."""
    if "t.me/" in link:
        return "", "", ""
    try:
        r = requests.get(link, headers=HEADERS, timeout=20, allow_redirects=True)
    except Exception:
        return "", "", ""
    ctype = r.headers.get("content-type", "")
    if "news.google.com" in r.url:
        return "", "", ""
    if "pdf" in ctype:
        return "", "", r.url                      # the link itself is a brochure PDF
    if "html" not in ctype:
        return "", "", ""
    page = r.text
    img = ""
    for pat in (r'<meta[^>]+(?:property|name)=["\'](?:og:image|twitter:image)["\'][^>]*content=["\']([^"\']+)',
                r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*(?:property|name)=["\'](?:og:image|twitter:image)'):
        m = re.search(pat, page, re.I)
        if m:
            img = urllib.parse.urljoin(r.url, html.unescape(m.group(1)))
            break
    brochure = ""
    for href, label in re.findall(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', page, re.I | re.S)[:400]:
        h, l = href.lower(), strip_tags(label).lower()
        if "brochure" in h or "brochure" in l or "flyer" in l or "poster" in l:
            brochure = urllib.parse.urljoin(r.url, html.unescape(href))
            break
        if not brochure and h.split("?")[0].endswith(".pdf"):
            brochure = urllib.parse.urljoin(r.url, html.unescape(href))
    return strip_tags(page)[:3000], img, brochure


# ---------------- details extraction ----------------
def new_card(name, link, category, score, **kw):
    card = {"name": name, "link": link, "category": category, "score": score,
            "organiser": "", "mode": "", "venue": "", "fee": "", "deadline": "",
            "dates": "", "eligibility": "", "certificate": "", "summary": "",
            "image": "", "brochure": ""}
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
    if not card.get("certificate") and re.search(r"e-?certificate|certificate (will|shall) be|certificates? of participation", t):
        card["certificate"] = "Yes"
    if not card.get("venue"):
        m = re.search(r"venue\s*[:\-]\s*([^.|\n]{5,70})", text or "", re.I)
        if m:
            card["venue"] = m.group(1).strip()
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
"venue": "<venue and city if offline/hybrid, else empty>",
"fee": "<Free | registration fee / APC / charges with currency e.g. ₹1,000 | empty if unknown>",
"deadline": "<last date to apply/register/submit, format like 10 Nov 2026, or empty>",
"dates": "<event/programme dates if given, or empty>",
"eligibility": "<who can apply, max 8 words, or empty>",
"certificate": "<Yes if a certificate is given, else empty>",
"summary": "<one line, max 15 words, why it is useful (e.g. counts for CAS), or empty>"'''


DETAIL_KEYS = ("organiser", "mode", "venue", "fee", "deadline", "dates", "eligibility",
               "certificate", "summary")


def keyword_classify(items):
    cards = []
    for it in items:
        text = (it["title"] + " " + it["snippet"]).lower()
        for cat, words in KEYWORDS.items():
            if any(w in text for w in words):
                if not passes_rules(cat, it["title"], it["snippet"]):
                    break
                c = new_card(it["title"], it["link"], cat, 3, image=it.get("image", ""))
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
fellowships. STRICT RULES:
- Keep ONLY open calls / announcements the user can still apply, register or submit to.
- REJECT every news report about something that already happened: someone won or received an
  award, MoU signed, event held / inaugurated / concluded, visits, rankings, appointments.
- REJECT items outside chemistry or closely allied science (e.g. pure environmental science,
  social science, management, humanities, medicine-only) unless it is a general faculty
  training (FIP/refresher/FDP), a general research grant, patent, MOOC or travel scheme.
- REJECT exam results, student admissions, school-level and student-only items.
- When unsure, REJECT. Fewer but perfect items are better.

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
            if score < max(MIN_SCORE, 4) or NEWS_BLOCK.search(it["title"]):
                continue
            cat = res.get("category", "")
            if cat not in CATEGORIES:          # not one of the wanted types -> skip
                continue
            c = new_card(str(res.get("name") or it["title"]), it["link"],
                         cat, score, image=it.get("image", ""),
                         **{k: str(res.get(k) or "") for k in DETAIL_KEYS})
            c["_text"] = it["snippet"]
            cards.append(c)
    return cards


def enrich(cards):
    """Open each item's web page and fill in mode / fee / deadline where missing."""
    need_ai = []
    for c in cards[:40]:
        text = c.get("_text", "")
        ptext, img, brochure = page_info(c["link"])
        text = (text + " " + ptext).strip()
        c["_text"] = text
        c["image"] = c["image"] or img
        c["brochure"] = brochure
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
                for k in DETAIL_KEYS:
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


BANNER_COLORS = {   # (background, accent) per category
    "Training (FIP/Refresher/FDP/STC)": ("#1d3557", "#ffd166"),
    "Workshop / Lecture series / Webinar": ("#264653", "#e9c46a"),
    "Conference": ("#3a0ca3", "#4cc9f0"),
    "Journal / Call for papers": ("#283618", "#dda15e"),
    "Book chapter / Editor / Reviewer": ("#5f0f40", "#fb8b24"),
    "APC waiver / discount": ("#005f73", "#94d2bd"),
    "Patent / IPR": ("#5a189a", "#ffd60a"),
    "Research grant / Project": ("#9d0208", "#ffba08"),
    "Travel grant": ("#023e8a", "#90e0ef"),
    "MOOC / e-content creation": ("#2b2d42", "#8ecae6"),
    "Consultancy / Industry collaboration": ("#344e41", "#b5e48c"),
    "Award / Fellowship": ("#6a040f", "#ffd700"),
}


def parse_date(s):
    """Turn '10th Nov 2026', 'Nov 10, 2026', '10/11/2026' etc. into a date (or None)."""
    if not s:
        return None
    t = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", str(s), flags=re.I)
    t = re.sub(r"(?i)\bsept\w*", "Sep", t)
    t = re.sub(r"\s+", " ", t.replace(",", " ")).strip()
    today = datetime.now(IST).date()
    for f in ("%d %B %Y", "%d %b %Y", "%B %d %Y", "%b %d %Y", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y",
              "%d/%m/%y", "%d-%m-%y", "%Y-%m-%d", "%d-%b-%Y", "%d-%B-%Y"):
        try:
            return datetime.strptime(t, f).date()
        except ValueError:
            pass
    for f in ("%d %B", "%d %b", "%B %d", "%b %d"):
        try:
            d = datetime.strptime(f"{t} {today.year}", f + " %Y").date()
            return d if d >= today - timedelta(days=30) else d.replace(year=today.year + 1)
        except ValueError:
            pass
    return None


def days_left(c):
    d = parse_date(c.get("deadline"))
    return (d - datetime.now(IST).date()).days if d else None


def deadline_badge(c):
    n = days_left(c)
    if n is None:
        return ""
    if n < 0:
        return "⛔ Closed"
    if n == 0:
        return "🔴 Closes TODAY"
    if n <= 3:
        return f"🔴 {n} day{'s' if n > 1 else ''} left"
    if n <= 7:
        return f"🟠 {n} days left"
    return f"🟢 {n} days left"


def caption(c):
    mode = c.get("mode", "")
    fee = c.get("fee", "")
    mode_icon = {"Online": "💻", "Offline": "🏫", "Hybrid": "🔀"}.get(mode, "❔")
    fee_icon = "🆓" if re.match(r"(free|no\b|nil|zero|waived)", fee.lower()) else "💰"
    lines = [f"{EMOJI.get(c['category'], '📌')} <b>{esc(c['category'].upper())}</b>",
             f"<b>{esc(c['name'][:150])}</b>",
             "━━━━━━━━━━━━━━━"]
    if c.get("organiser"):
        lines.append(f"🏛 {esc(c['organiser'][:90])}")
    lines.append(f"{mode_icon} <b>{esc(mode or 'Mode: ' + UNKNOWN)}</b>     "
                 f"{fee_icon} <b>{esc(fee[:60] or 'Fee: ' + UNKNOWN)}</b>")
    if c.get("venue") and mode != "Online":
        lines.append(f"📍 {esc(c['venue'][:90])}")
    if c.get("dates"):
        lines.append(f"📅 {esc(c['dates'][:60])}")
    badge = deadline_badge(c)
    lines.append(f"⏰ <b>Deadline: {esc(c.get('deadline') or UNKNOWN)}</b>" + (f"   {badge}" if badge else ""))
    if c.get("eligibility"):
        lines.append(f"👥 {esc(c['eligibility'][:80])}")
    if c.get("certificate"):
        lines.append("🎖 Certificate: Yes")
    if c.get("summary"):
        lines.append(f"💡 <i>{esc(c['summary'][:120])}</i>")
    return "\n".join(lines)[:1024]


def buttons(c):
    ok = lambda u: isinstance(u, str) and u.startswith(("http://", "https://"))
    row = []
    if ok(c.get("link")):
        row.append({"text": "🔗 Details / Register", "url": c["link"]})
    if ok(c.get("brochure")):
        row.append({"text": "📄 Brochure", "url": c["brochure"]})
    rows = [row] if row else []
    d = parse_date(c.get("deadline"))
    if d:
        cal = "https://calendar.google.com/calendar/render?" + urllib.parse.urlencode({
            "action": "TEMPLATE", "text": "Deadline: " + c["name"][:80],
            "dates": f"{d:%Y%m%d}/{d + timedelta(days=1):%Y%m%d}", "details": c.get("link", "")})
        rows.append([{"text": "📅 Add deadline to Google Calendar", "url": cal}])
    return {"inline_keyboard": rows}


# ---- banner picture (made when the opportunity has no brochure image of its own) ----
def _font(size, bold=False):
    from PIL import ImageFont
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    for path in ("/usr/share/fonts/truetype/dejavu/" + name, name):
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _wrap(draw, text, font, width, max_lines):
    words, lines, cur = text.split(), [], ""
    for w in words:
        test = (cur + " " + w).strip()
        if draw.textlength(test, font=font) <= width:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip(".,;:") + "…"
    return lines


def make_banner(c):
    from PIL import Image, ImageDraw
    W, H = 1200, 675
    bg, acc = BANNER_COLORS.get(c["category"], ("#22223b", "#f2cc8f"))
    img = Image.new("RGBA", (W, H), bg)
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    g = ImageDraw.Draw(glow)
    g.ellipse((W - 420, -260, W + 260, 420), fill=(255, 255, 255, 28))
    g.ellipse((-200, H - 180, 260, H + 260), fill=(255, 255, 255, 18))
    img = Image.alpha_composite(img, glow)
    d = ImageDraw.Draw(img)

    f_lab = _font(30, True)
    label = c["category"].upper()
    tw = d.textlength(label, font=f_lab)
    d.rounded_rectangle((60, 50, 60 + tw + 56, 112), radius=31, fill=acc)
    d.text((88, 63), label, font=f_lab, fill=bg)

    n = days_left(c)
    urgent = n is not None and 0 <= n <= 3
    dl = c.get("deadline") or "check link"
    if n is not None and n >= 0:
        dl += f"  ({'today' if n == 0 else str(n) + 'd left'})"
    chips = [(c.get("mode") or "Mode: check link", "white", bg),
             (c.get("fee") or "Fee: check link", "white", bg),
             ("Deadline: " + dl, "#e63946" if urgent else acc, "white" if urgent else bg)]
    f_chip = _font(32, True)
    rows, cur, x = [], [], 60
    for text, fill, col in chips:
        text = text[:34]
        w = d.textlength(text, font=f_chip) + 52
        if x + w > W - 60 and cur:
            rows.append(cur)
            cur, x = [], 60
        cur.append((text, fill, col, w))
        x += w + 18
    rows.append(cur)
    yb = H - 70 - 88 * len(rows)

    # title + organiser, using only the space left above the chips
    room = yb - 150 - (62 if c.get("organiser") else 0) - 20
    for size in (62, 54, 46, 40):            # shrink the title until it fits
        f_title, step = _font(size, True), int(size * 1.22)
        allowed = max(1, room // step)
        lines = _wrap(d, c["name"], f_title, W - 140, 99)
        if len(lines) <= allowed:
            break
    lines = _wrap(d, c["name"], f_title, W - 160, allowed)
    y = 150
    for line in lines:
        d.text((60, y), line, font=f_title, fill="white")
        y += step
    if c.get("organiser"):
        f_org = _font(34)
        org = _wrap(d, c["organiser"], f_org, W - 120, 1)[0]
        d.text((60, y + 10), org, font=f_org, fill=acc)

    for row in rows:
        x = 60
        for text, fill, col, w in row:
            d.rounded_rectangle((x, yb, x + w, yb + 70), radius=35, fill=fill)
            d.text((x + 26, yb + 16), text, font=f_chip, fill=col)
            x += w + 18
        yb += 88
    d.text((60, H - 50), "CAS Bot  •  tap the buttons below for details", font=_font(24), fill=acc)

    import io
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "PNG")
    return buf.getvalue()


def get_image(url):
    """Download the opportunity's own banner/brochure image if it is a real, big picture."""
    if not url:
        return None
    try:
        import io
        from PIL import Image
        r = requests.get(url, headers=HEADERS, timeout=20)
        ctype = r.headers.get("content-type", "")
        if not r.ok or not ctype.startswith("image/") or "svg" in ctype or len(r.content) > 9_000_000:
            return None
        w, h = Image.open(io.BytesIO(r.content)).size
        return r.content if (w >= 400 and h >= 250) else None
    except Exception:
        return None


def _tg(method, data, files=None):
    r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/{method}", data=data, files=files, timeout=60)
    if not r.ok:
        print("Telegram error:", r.text[:300])
    return r.ok


def send_card(c):
    """Picture (brochure or made banner) + details + buttons. Returns True if delivered."""
    cap = caption(c)
    if not (TG_TOKEN and TG_CHAT):
        print(cap, "\n", json.dumps(buttons(c)), "\n")
        return False
    markup = json.dumps(buttons(c))
    pictures = []
    original = get_image(c.get("image"))
    if original:
        pictures.append(original)
    try:
        pictures.append(make_banner(c))
    except Exception as ex:
        print("Banner error:", ex)
    data = {"chat_id": TG_CHAT, "caption": cap, "parse_mode": "HTML", "reply_markup": markup}
    ok = False
    for pic in pictures:
        if _tg("sendPhoto", data, files={"photo": ("photo.jpg", pic)}):
            ok = True
            break
    if not ok:
        text = {"chat_id": TG_CHAT, "text": cap, "parse_mode": "HTML", "disable_web_page_preview": "true"}
        ok = _tg("sendMessage", dict(text, reply_markup=markup)) or _tg("sendMessage", text)
    time.sleep(2)
    return ok


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
    cards = [c for c in enrich(cards) if has_details(c)] + check_pages(st)
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
    st["queue"] = [c for c in st["queue"] if (days_left(c) or 0) >= 0
                   and (c["category"] == "Watched page updated"
                        or (c["category"] in CATEGORIES and not NEWS_BLOCK.search(c["name"]) and has_details(c)))]
    st["queue"].sort(key=lambda c: (-(c.get("score", 0)), days_left(c) if days_left(c) is not None else 999))
    hour = datetime.now(IST).hour
    slots_left = max(1, len([h for h in SEND_HOURS_IST if h >= hour]))
    n = min(MAX_PER_SLOT, math.ceil(len(st["queue"]) / slots_left))
    batch, rest = st["queue"][:n], st["queue"][n:]
    sent, failed = [], []
    for c in batch:
        if (days_left(c) or 0) < 0:          # deadline already over -> skip it
            continue
        (sent if send_card(c) else failed).append(c)
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
