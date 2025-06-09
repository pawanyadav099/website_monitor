import requests
import httpx
from bs4 import BeautifulSoup
import urllib3
import os
from datetime import datetime
from dateutil.parser import parse as date_parse
from io import BytesIO
import re
import PyPDF2
from urllib.parse import urljoin, urlparse
from dotenv import load_dotenv

from url import URLS  # URL list

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Load environment variables from .env file
load_dotenv()

TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
}

SENT_POSTS_FILE = "sent_posts.txt"

KEYWORDS = [
    "job", "result", "notification", "admit card", "notice", "exam",
    "interview", "vacancy", "recruitment", "call letter", "merit list"
]

def contains_keyword(text):
    if not text:
        return False
    text_lower = text.lower()
    return any(keyword in text_lower for keyword in KEYWORDS)

def load_sent_posts():
    if not os.path.exists(SENT_POSTS_FILE):
        print(f"{SENT_POSTS_FILE} not found, starting fresh")
        return set()
    try:
        with open(SENT_POSTS_FILE, "r", encoding="utf-8") as f:
            posts = set(line.strip() for line in f if line.strip())
            print(f"Loaded {len(posts)} sent posts")
            return posts
    except Exception as e:
        print(f"Error reading {SENT_POSTS_FILE}: {e}")
        return set()

def save_sent_post(post_id):
    try:
        with open(SENT_POSTS_FILE, "a", encoding="utf-8") as f:
            f.write(post_id + "\n")
    except Exception as e:
        print(f"Error saving post {post_id} to {SENT_POSTS_FILE}: {e}")

def send_telegram(message):
    if not TOKEN or not CHAT_ID:
        print("Telegram TOKEN or CHAT_ID missing")
        return
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    data = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": False
    }
    try:
        r = requests.post(url, data=data)
        if r.status_code != 200:
            print(f"Telegram send failed: {r.text}")
    except Exception as e:
        print(f"Telegram error: {e}")

def fetch(url):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10, verify=False)
        resp.raise_for_status()
        return resp.text
    except requests.exceptions.SSLError as ssl_err:
        print(f"SSL Error for {url}: {ssl_err}. Trying httpx fallback...")
        try:
            with httpx.Client(verify=False, timeout=10) as client:
                resp = client.get(url)
                resp.raise_for_status()
                return resp.text
        except Exception as e:
            print(f"HTTPX fallback failed for {url}: {e}")
            return None
    except Exception as e:
        print(f"Error fetching {url}: {e}")
        return None

def extract_date_from_text(text):
    if not text:
        return None
    date_patterns = [
        r'\b(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\b',  # e.g., 01/06/2025, 01-06-25
        r'\b(\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)[a-z]*\s+\d{2,4})\b',  # e.g., 01 June 2025
        r'\b(\d{1,2}(?:st|nd|rd|th)?\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{2,4})\b',  # e.g., 1st Jun 2025
        r'\b(\d{4}[/-]\d{1,2}[/-]\d{1,2})\b',  # e.g., 2025-06-01
    ]
    for pattern in date_patterns:
        m = re.search(pattern, text, re.I)
        if m:
            try:
                dt = date_parse(m.group(1), dayfirst=True)
                return dt
            except:
                continue
    try:
        dt = date_parse(text, fuzzy=True, dayfirst=True)
        return dt
    except:
        return None
    return None

def extract_date_from_pdf(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=15, verify=False)
        r.raise_for_status()
        pdf_file = BytesIO(r.content)
        reader = PyPDF2.PdfReader(pdf_file)
        full_text = ""
        for page in reader.pages:
            page_text = page.extract_text()
            if page_text:
                full_text += page_text + "\n"

        lines = full_text.splitlines()
        date_candidates = []
        posts_count = None
        application_fee = None
        publish_date = None

        for line in lines:
            dt = extract_date_from_text(line)
            if dt:
                date_candidates.append(dt)

            if posts_count is None:
                m = re.search(r'(total|vacancy|posts?|positions?)[:\s]*([0-9,]+)', line, re.I)
                if m:
                    posts_count = m.group(2).strip()

            if application_fee is None:
                m = re.search(r'(application fee|fee)[:\s]*₹?\s*([\d,]+)', line, re.I)
                if m:
                    application_fee = m.group(2).strip()

            if publish_date is None:
                m = re.search(r'(published|date of issue|notification date|publish date)[:\s]*(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{2,4})', line, re.I)
                if m:
                    try:
                        publish_date = date_parse(m.group(2), dayfirst=True)
                    except:
                        pass

        post_date = min(date_candidates) if date_candidates else None
        print(f"Extracted PDF date: {post_date} from {url}")

        return {
            "post_date": post_date,
            "posts_count": posts_count,
            "application_fee": application_fee,
            "publish_date": publish_date,
            "pdf_url": url,
            "pdf_text_snippet": full_text[:500]
        }
    except Exception as e:
        print(f"Error extracting PDF from {url}: {e}")
        return {"pdf_url": url}

def extract_post_date(a_tag):
    candidates = []

    title_attr = a_tag.get('title')
    if title_attr:
        candidates.append(title_attr)

    next_sibling = a_tag.find_next_sibling(text=True)
    if next_sibling and isinstance(next_sibling, str):
        candidates.append(next_sibling.strip())

    parent = a_tag.parent
    if parent:
        candidates.append(parent.get_text(" ", strip=True))
        if parent.parent:
            candidates.append(parent.parent.get_text(" ", strip=True))

    for text in candidates:
        dt = extract_date_from_text(text)
        if dt:
            return dt
    return None

def extract_date_from_url(url):
    m = re.search(r'/(\d{4})[/-](\d{1,2})/', url)
    if m:
        year = int(m.group(1))
        month = int(m.group(2))
        return datetime(year, month, 1)
    return None

def normalize_url(url, base_url):
    full_url = urljoin(base_url, url)
    parsed = urlparse(full_url)
    clean_url = parsed.scheme + "://" + parsed.netloc + parsed.path
    return clean_url.rstrip('/')

def is_valid_url(url):
    try:
        result = requests.head(url, headers=HEADERS, timeout=5, allow_redirects=True, verify=False)
        return result.status_code < 400
    except Exception:
        return False

def is_post_link(href, base_url):
    if not href:
        return False
    full_url = normalize_url(href, base_url)
    return full_url.lower().endswith('.pdf') or contains_keyword(href)

def parse_and_notify(url, sent_posts):
    html = fetch(url)
    if not html:
        print(f"No data fetched from {url}")
        return

    soup = BeautifulSoup(html, "html.parser")
    new_posts = []

    for a in soup.find_all('a', href=True):
        text = a.get_text(strip=True)
        href = a['href']

        if not text or not href:
            print(f"Skipping empty link: {href}")
            continue

        if not contains_keyword(text):
            print(f"Skipping non-relevant link: {text}")
            continue

        post_id = normalize_url(href, url)
        print(f"Processing post: {href} -> Resolved to: {post_id}")

        if post_id in sent_posts:
            print(f"Skipping already sent post: {post_id}")
            continue

        post_date = extract_post_date(a)
        pdf_data = None
        if post_id.lower().endswith('.pdf'):
            pdf_data = extract_date_from_pdf(post_id)
            if pdf_data and pdf_data.get("post_date"):
                post_date = pdf_data["post_date"]

        if not post_date:
            post_date = extract_date_from_url(post_id)

        # Assume current month if no date found
        now = datetime.now()
        if not post_date:
            post_date = datetime(now.year, now.month, 1)
            print(f"No date found for {post_id}, assuming current month: {post_date}")

        # Only process posts in current month
        if post_date.year != now.year or post_date.month != now.month:
            print(f"Skipping post {post_id}: Date {post_date} not in current month")
            continue

        msg = f"*New Notification*\n\n"
        msg += f"Website URL: {url}\n"
        msg += f"Notification Title: {text or 'Untitled Notification'}\n"
        msg += f"Notification URL: {post_id if is_valid_url(post_id) else '_Invalid or unreachable URL_'}\n"
        msg += f"Publish Date: {(pdf_data.get('publish_date') or post_date).strftime('%Y-%m-%d') if (pdf_data and pdf_data.get('publish_date')) or post_date else '_Not found_'}\n"
        msg += f"PDF URL: {pdf_data.get('pdf_url') if pdf_data else '_Not applicable_'}\n"

        new_posts.append((post_id, msg))

    if not new_posts:
        print(f"No new relevant posts found on {url} for current month")
        return

    for post_id, message in new_posts:
        send_telegram(message)
        save_sent_post(post_id)
        sent_posts.add(post_id)  # Update in-memory set to prevent duplicates
        print(f"Sent notification for: {post_id}")

def main():
    print("Monitoring started...")
    sent_posts = load_sent_posts()
    for url in URLS:
        parse_and_notify(url, sent_posts)

if __name__ == "__main__":
    main()
