#!/usr/bin/env python3
"""
Improved scraper + Telegram notifier
- Robust network handling (retries/backoff, cert fallback)
- Article-level parsing for title/link/date (handles cases like jobshikhar.in)
- Date parsing via dateparser (supports natural dates, "September 11, 2025", "Today", "Tomorrow")
- Keeps original AI zero-shot filter logic; falls back to keyword filter if model unavailable
- Sends notifications for items with date == today OR date == tomorrow OR contains words 'today'/'tomorrow'
"""

import os
import re
import time
import logging
from datetime import datetime, timedelta
from urllib.parse import urlparse

import requests
import certifi
import dateparser
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from requests.exceptions import RequestException, SSLError, ConnectTimeout, ReadTimeout, ConnectionError

# Optional HF model
try:
    from transformers import pipeline
except Exception:
    pipeline = None

# If you keep urls in a separate module like original:
try:
    from urls import urls  # expect urls to be a list of URL strings
except Exception:
    urls = []  # fallback empty list

# ---------- Basic prints & logging ----------
print("[1] Starting script")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ---------- Config & env ----------
TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
SENT_FILE = "sent_links.txt"

# Time logic
TODAY = datetime.now().date()
TOMORROW = TODAY + timedelta(days=1)

print("[2] Environment variables loaded (TOKEN set? {})".format(bool(TOKEN)))

# ---------- Telegram helper ----------
def send_telegram(message):
    """Send a message only if TOKEN/CHAT_ID exist. Handles exceptions."""
    if not TOKEN or not CHAT_ID:
        logging.warning("Telegram TOKEN or CHAT_ID not set — skipping send_telegram")
        return False
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    try:
        logging.info("[7] Sending Telegram message: %.60s...", message)
        r = requests.post(url, data=payload, timeout=15)
        logging.info("[7.1] Telegram API response: %s", r.text)
        return r.ok
    except Exception as e:
        logging.error("[ERROR] Telegram Error: %s", e)
        return False

# Notify start (best-effort)
send_telegram("✅ Script has started")

# ---------- Network session with retries ----------
def create_session(retries=3, backoff_factor=1, status_forcelist=(429, 500, 502, 503, 504)):
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/115.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })
    retry = Retry(
        total=retries,
        connect=retries,
        read=retries,
        status=retries,
        status_forcelist=list(status_forcelist),
        allowed_methods=frozenset(["HEAD", "GET", "OPTIONS"]),
        backoff_factor=backoff_factor,
        raise_on_status=False
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session

session = create_session(retries=3, backoff_factor=1)

# ---------- Safe GET with SSL fallback ----------
def ensure_scheme(url):
    parsed = urlparse(url)
    if not parsed.scheme:
        return "https://" + url
    return url

def safe_get(session, url, timeout=(10, 30), allow_insecure_fallback=True):
    """
    Returns (response, error_message). If SSL cert fails and allow_insecure_fallback True,
    retry with verify=False (INSECURE). We use certifi's CA bundle by default.
    """
    url = ensure_scheme(url)
    try:
        # Prefer HEAD to quickly check, but some servers reject HEAD -> fallback to GET
        try:
            h = session.head(url, timeout=timeout, allow_redirects=True, verify=certifi.where())
            if h.status_code and h.status_code < 400:
                r = session.get(url, timeout=timeout, allow_redirects=True, verify=certifi.where())
                r.raise_for_status()
                return r, None
        except RequestException:
            # HEAD failed or not allowed -> continue to GET
            pass

        r = session.get(url, timeout=timeout, allow_redirects=True, verify=certifi.where())
        r.raise_for_status()
        return r, None

    except SSLError as e:
        logging.warning("SSLError for %s: %s", url, e)
        if allow_insecure_fallback:
            logging.warning("Retrying %s with verify=False (insecure fallback enabled)", url)
            try:
                r = session.get(url, timeout=timeout, allow_redirects=True, verify=False)
                r.raise_for_status()
                return r, None
            except Exception as e2:
                return None, f"SSLError and insecure fallback failed: {e2}"
        return None, f"SSLError: {e}"

    except ConnectTimeout as e:
        logging.warning("ConnectTimeout for %s: %s", url, e)
        return None, f"ConnectTimeout: {e}"

    except ReadTimeout as e:
        logging.warning("ReadTimeout for %s: %s", url, e)
        return None, f"ReadTimeout: {e}"

    except ConnectionError as e:
        logging.warning("ConnectionError for %s: %s", url, e)
        return None, f"ConnectionError: {e}"

    except RequestException as e:
        logging.warning("RequestException for %s: %s", url, e)
        return None, f"RequestException: {e}"

# ---------- Sent links persistence ----------
def load_sent_links():
    try:
        with open(SENT_FILE, "r", encoding="utf-8") as f:
            links = set(line.strip() for line in f if line.strip())
            print(f"[5] Loaded {len(links)} sent links")
            return links
    except FileNotFoundError:
        print("[5] sent_links.txt not found, starting fresh")
        return set()
    except Exception as e:
        logging.error("Error reading sent_links file: %s", e)
        return set()

def save_sent_link(link):
    try:
        with open(SENT_FILE, "a", encoding="utf-8") as f:
            f.write(link + "\n")
        print(f"[6] Saved link: {link}")
    except Exception as e:
        logging.error("Failed to save link %s: %s", link, e)

# ---------- Date extraction (improved) ----------
def extract_date_from_text(text):
    """
    Use dateparser to extract date from free text.
    Returns a date object or None.
    """
    if not text:
        return None
    # Quick check for 'today'/'tomorrow' words
    lower = text.lower()
    if "today" in lower:
        return TODAY
    if "tomorrow" in lower:
        return TOMORROW

    # Use dateparser with DMY preference (many websites use DMY)
    try:
        dt = dateparser.parse(text, settings={'PREFER_DAY_OF_MONTH': 'first', 'DATE_ORDER': 'DMY'})
        if dt:
            return dt.date()
    except Exception:
        pass

    # fallback: try limited regex patterns (legacy)
    patterns = [
        r'(\d{1,2})[-/](\d{1,2})[-/](\d{4})',       # 06/08/2025 or 06-08-2025
        r'(\d{4})[-/](\d{2})[-/](\d{2})',           # 2025/08/06
        r'(\d{1,2})[.](\d{1,2})[.](\d{4})',        # 06.08.2025
        r'(january|february|march|april|may|june|july|august|september|october|november|december)[\s\-]+(\d{1,2}),?\s*(\d{4})'
    ]
    text_lower = text.lower()
    for pattern in patterns:
        m = re.search(pattern, text_lower)
        if m:
            try:
                dt = dateparser.parse(m.group(0), settings={'DATE_ORDER': 'DMY'})
                if dt:
                    return dt.date()
            except Exception:
                continue
    return None

# ---------- Classifier (zero-shot) with fallback ----------
def load_classifier():
    if pipeline is None:
        logging.warning("transformers.pipeline not available - skipping model load")
        return None
    try:
        print("[3] Loading AI model... please wait")
        classifier = pipeline("zero-shot-classification", model="valhalla/distilbart-mnli-12-1")
        print("[4] AI model loaded successfully")
        return classifier
    except Exception as e:
        logging.error("Failed to load AI model: %s", e)
        return None

classifier = load_classifier()

KEYWORD_FALLBACK = [
    "notification", "result", "admit", "admit card", "apply", "recruitment", "vacancy",
    "shortlist", "interview", "answer key", "notice", "counselling", "merit list",
]

def is_recent_notification(text):
    """
    Uses zero-shot classifier if available; otherwise keyword-based heuristic.
    Returns True if should be considered recent.
    """
    if not text:
        return False
    # Normalize
    txt = text.strip()
    if classifier:
        try:
            labels = ["recent notification", "old notification"]
            res = classifier(txt, labels)
            if isinstance(res, dict):
                # HF pipeline returns labels sorted by score
                top_label = res.get('labels', [None])[0]
                top_score = res.get('scores', [0])[0]
                if top_label == "recent notification" and top_score > 0.7:
                    return True
            else:
                # Some versions return list of dicts; attempt safe access
                if res and isinstance(res, list) and res[0].get('label') == "recent notification" and res[0].get('score', 0) > 0.7:
                    return True
        except Exception as e:
            logging.warning("[AI ERROR] classifier failed: %s. Falling back to keyword check.", e)

    # Keyword fallback
    lowered = txt.lower()
    for kw in KEYWORD_FALLBACK:
        if kw in lowered:
            return True
    return False

# ---------- Article parsing ----------
def find_articles(soup):
    """
    Try to find article/post containers. Returns list of article-like tags.
    Fallback to empty list if none found.
    """
    articles = []
    # Common container tags
    for tagname in ("article", "div", "li"):
        found = soup.find_all(tagname, class_=re.compile(r"(post|article|entry|elementor-post|news|notice|blog)", re.I))
        if found:
            articles.extend(found)
    # If none found via class heuristics, try any <article> tags
    if not articles:
        articles = soup.find_all("article")
    return articles

def extract_from_article(article, base_url):
    """
    Given an article/container element, find a link, title, and date.
    Returns tuple (title, full_link, date_text, parsed_date) or (None, None, None, None)
    """
    # find link
    link_tag = article.find("a", href=True)
    if not link_tag:
        # maybe anchor in heading
        heading = article.find(re.compile("^h[1-6]$"))
        if heading:
            link_tag = heading.find("a", href=True)

    if not link_tag:
        return None, None, None, None

    title = link_tag.get_text(strip=True) or link_tag.get("title") or ""
    href = link_tag.get("href")
    full_link = requests.compat.urljoin(base_url, href)

    # Try multiple date selectors inside container
    date_text = None
    # 1. <time datetime="...">
    time_tag = article.find("time")
    if time_tag:
        date_text = time_tag.get("datetime") or time_tag.get_text(strip=True)

    # 2. span/div with class containing 'date' or 'post-date' or 'elementor-post-date'
    if not date_text:
        date_like = article.find(["span", "div"], class_=re.compile(r"(date|post-date|elementor-post-date|entry-date|posted-on)", re.I))
        if date_like:
            date_text = date_like.get_text(strip=True)

    # 3. meta property e.g. <meta itemprop="datePublished" content="2025-09-11">
    if not date_text:
        meta_date = article.find("meta", attrs={"itemprop": "datePublished"}) or article.find("meta", attrs={"name": "date"})
        if meta_date and meta_date.get("content"):
            date_text = meta_date.get("content")

    # 4. fallback: search within article text for date-like substring
    if not date_text:
        # look for month names or digit patterns in the article's text
        text_snippet = article.get_text(" ", strip=True)
        # try to find a substring that likely contains a date (month name or dd/mm/yyyy)
        m = re.search(r'((?:\d{1,2}[-/\.]\d{1,2}[-/\.]\d{2,4})|(?:\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\b [\d]{1,2},? ?\d{2,4}))', text_snippet, re.I)
        if m:
            date_text = m.group(1)

    parsed_date = extract_date_from_text(date_text) if date_text else None

    return title, full_link, date_text, parsed_date

# ---------- Main site checker ----------
def check_site(url, sent_links):
    url = ensure_scheme(url)
    logging.info("[8] Checking site: %s", url)
    r, err = safe_get(session, url, timeout=(10, 30), allow_insecure_fallback=True)
    if r is None:
        logging.error("Failed to scrape %s: %s", url, err)
        return

    soup = BeautifulSoup(r.text, "html.parser")

    # Prefer article-level parsing
    articles = find_articles(soup)
    processed = 0
    found_links = set()
    if articles:
        logging.info("[9] Found %d article-like containers", len(articles))
        for art in articles:
            title, full_link, date_text, parsed_date = extract_from_article(art, url)
            if not full_link:
                continue
            if full_link in found_links:
                continue
            found_links.add(full_link)
            processed += 1

            # Decide date validity: today OR tomorrow OR contains 'today'/'tomorrow'
            date_ok = False
            reason_debug = ""
            if date_text and isinstance(date_text, str) and re.search(r'\b(today|tomorrow)\b', date_text, re.I):
                date_ok = True
                reason_debug = "text had today/tomorrow"
            elif parsed_date:
                if parsed_date == TODAY or parsed_date == TOMORROW:
                    date_ok = True
                    reason_debug = f"parsed_date {parsed_date}"
            # If no date info, skip (this preserves original logic)
            if not date_ok:
                logging.debug("Skipping (not today/tomorrow): %s | date_text=%s parsed=%s", title, date_text, parsed_date)
                continue

            # Dedup check
            if full_link in sent_links:
                logging.info("[11] Skipped duplicate link: %s", full_link)
                continue

            # Relevance check via AI or keyword fallback
            check_text = title or date_text or full_link
            if not is_recent_notification(check_text):
                logging.info("[10] Skipped by AI/keyword filter: %s", title)
                continue

            # Prepare message and send
            message = (
                f"<b>{title}</b>\n"
                f"🔗 <a href=\"{full_link}\">Open Notification</a>\n"
                f"📅 {date_text if date_text else parsed_date}\n"
                f"🌐 Source Page: <a href=\"{url}\">{url}</a>"
            )
            send_telegram(message)
            save_sent_link(full_link)
            sent_links.add(full_link)

    else:
        # Fallback: scan all links (original approach) but try to find date in nearby siblings/parents
        links = soup.find_all("a", href=True)
        logging.info("[9] Found %d links", len(links))
        for link in links:
            text = link.get_text(strip=True)
            href = link.get("href")
            if not href:
                continue
            full_link = requests.compat.urljoin(url, href)
            if full_link in found_links:
                continue
            found_links.add(full_link)

            # Try to find date from surrounding elements: parent, siblings
            date_text = None
            parsed_date = None
            # check immediate parent containers
            parent = link.parent
            search_containers = [parent, parent.parent if parent is not None else None]
            for cont in search_containers:
                if not cont:
                    continue
                # time tag
                t = cont.find("time")
                if t:
                    date_text = t.get("datetime") or t.get_text(strip=True)
                    break
                # span with date-like class
                d = cont.find(["span", "div"], class_=re.compile(r"(date|post-date|elementor-post-date|entry-date|posted-on)", re.I))
                if d:
                    date_text = d.get_text(strip=True)
                    break
                # meta tags
                m = cont.find("meta", attrs={"itemprop": "datePublished"})
                if m and m.get("content"):
                    date_text = m.get("content")
                    break
            if not date_text:
                # check siblings
                sib_prev = link.find_previous(string=re.compile(r'\b(today|tomorrow|[A-Za-z]{3,}\s\d{1,2}|[0-9]{1,2}/[0-9]{1,2}/[0-9]{2,4})', re.I))
                if sib_prev:
                    date_text = sib_prev.strip()
            if date_text:
                parsed_date = extract_date_from_text(date_text)

            # Accept if parsed_date is today/tomorrow or date_text contains words
            date_ok = False
            if date_text and re.search(r'\b(today|tomorrow)\b', date_text, re.I):
                date_ok = True
            elif parsed_date and (parsed_date == TODAY or parsed_date == TOMORROW):
                date_ok = True

            if not date_ok:
                continue

            # Dedup
            if full_link in sent_links:
                logging.info("[11] Skipped duplicate link: %s", full_link)
                continue

            # AI check
            check_text = text or date_text or full_link
            if not is_recent_notification(check_text):
                logging.info("[10] Skipped by AI/keyword filter: %s", check_text[:80])
                continue

            # Send
            message = (
                f"<b>{text}</b>\n"
                f"🔗 <a href=\"{full_link}\">Open Notification</a>\n"
                f"📅 {date_text if date_text else parsed_date}\n"
                f"🌐 Source Page: <a href=\"{url}\">{url}</a>"
            )
            send_telegram(message)
            save_sent_link(full_link)
            sent_links.add(full_link)

    logging.info("[done] Processed %d candidate links on %s", processed, url)

# ---------- Main run ----------
def run_monitor():
    sent_links = load_sent_links()
    if not urls:
        logging.warning("No URLs provided in `urls` list. Exiting.")
        return
    for u in urls:
        try:
            check_site(u, sent_links)
            # polite delay between sites
            time.sleep(1)
        except Exception as e:
            logging.exception("Unexpected error while checking %s: %s", u, e)

if __name__ == "__main__":
    run_monitor()
    print("[12] Script finished")
