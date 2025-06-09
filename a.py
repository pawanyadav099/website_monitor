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

from url import URLS  # URL list

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

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
    text_lower = text.lower()
    return any(keyword in text_lower for keyword in KEYWORDS)

def load_sent_posts():
    if not os.path.exists(SENT_POSTS_FILE):
        return set()
    with open(SENT_POSTS_FILE, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f if line.strip())

def save_sent_post(post_id):
    with open(SENT_POSTS_FILE, "a", encoding="utf-8") as f:
        f.write(post_id + "\n")

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
            print("Telegram send failed:", r.text)
    except Exception as e:
        print("Telegram error:", e)

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
    # Try to find date in given text
    try:
        dt = date_parse(text, fuzzy=True, dayfirst=True)
        return dt
    except Exception:
        return None

def extract_date_from_pdf(url):
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        pdf_file = BytesIO(r.content)
        reader = PyPDF2.PdfReader(pdf_file)
        full_text = ""
        for page in reader.pages:
            page_text = page.extract_text()
            if page_text:
                full_text += page_text + "\n"

        # Search for date candidates in text lines
        lines = full_text.splitlines()
        date_candidates = []
        posts_count = None
        application_fee = None
        publish_date = None

        for line in lines:
            # Extract date
            dt = extract_date_from_text(line)
            if dt and dt.year >= datetime.now().year-1:
                date_candidates.append(dt)

            # Extract total posts/vacancy count (example regex)
            if posts_count is None:
                m = re.search(r'(total|vacancy|posts?|positions?)[:\s]*([0-9,]+)', line, re.I)
                if m:
                    posts_count = m.group(2).strip()

            # Extract application fee (example regex)
            if application_fee is None:
                m = re.search(r'(application fee|fee)[:\s]*₹?\s*([\d,]+)', line, re.I)
                if m:
                    application_fee = m.group(2).strip()

            # Extract publish date if mentioned separately
            if publish_date is None:
                m = re.search(r'(published|date of issue|notification date|publish date)[:\s]*(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})', line, re.I)
                if m:
                    try:
                        publish_date = date_parse(m.group(2), dayfirst=True)
                    except:
                        pass

        # Pick earliest date from candidates as post date
        post_date = min(date_candidates) if date_candidates else None

        return {
            "post_date": post_date,
            "posts_count": posts_count,
            "application_fee": application_fee,
            "publish_date": publish_date,
            "pdf_url": url,
            "pdf_text_snippet": full_text[:500]  # first 500 chars as snippet if needed
        }

    except Exception as e:
        print(f"Error extracting PDF: {e}")
        return None

def extract_post_date(a_tag):
    now = datetime.now()
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
        if dt and (dt.year == now.year and dt.month in [now.month, now.month + 1]):
            return dt

    return None

def extract_date_from_url(url):
    # Example pattern: /2025/06/ or /2025-06/
    m = re.search(r'/(\d{4})[/-](\d{1,2})/', url)
    if m:
        year = int(m.group(1))
        month = int(m.group(2))
        now = datetime.now()
        if year == now.year and month in [now.month, now.month + 1]:
            return datetime(year, month, 1)
    return None

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
            continue

        if not contains_keyword(text):
            continue

        if href.startswith("/"):
            from urllib.parse import urljoin
            href = urljoin(url, href)

        post_id = href.strip()

        if post_id in sent_posts:
            continue

        # Try extract date from <a> tag and surroundings
        post_date = extract_post_date(a)

        # If no date, try PDF extraction if href is PDF
        pdf_data = None
        if href.lower().endswith(".pdf"):
            pdf_data = extract_date_from_pdf(href)
            if pdf_data and pdf_data["post_date"]:
                post_date = pdf_data["post_date"]

        # If still no date, try URL date
        if not post_date:
            post_date = extract_date_from_url(href)

        now = datetime.now()
        # Check date is current or next month or None (allow date not found but notify with message)
        date_ok = False
        if post_date:
            if (post_date.year == now.year) and (post_date.month in [now.month, (now.month % 12) + 1]):
                date_ok = True
        else:
            # Date not found scenario, we still notify but mention it
            date_ok = True

        if not date_ok:
            # Old post, skip
            continue

        # Prepare Telegram message
        if pdf_data:
            msg = f"*New PDF Notification*\n\n*Title:* {text}\n*Link:* [PDF here]({pdf_data['pdf_url']})\n"
            if pdf_data["post_date"]:
                msg += f"*Date:* {pdf_data['post_date'].strftime('%Y-%m-%d')}\n"
            else:
                msg += "_Date not found_\n"

            if pdf_data["posts_count"]:
                msg += f"*Total Posts/Vacancies:* {pdf_data['posts_count']}\n"
            if pdf_data["application_fee"]:
                msg += f"*Application Fee:* ₹{pdf_data['application_fee']}\n"
            if pdf_data["publish_date"]:
                msg += f"*Publish Date:* {pdf_data['publish_date'].strftime('%Y-%m-%d')}\n"

            msg += "\n*Note:* This is PDF-based data extraction."
        else:
            msg = f"*New Post*\n\n*Title:* {text}\n*Link:* [Here]({post_id})\n"
            if post_date:
                msg += f"*Date:* {post_date.strftime('%Y-%m-%d')}\n"
            else:
                msg += "_Date not found_\n"

        new_posts.append((post_id, msg))

    if not new_posts:
        print(f"No new relevant posts found on {url} for current/next month")
        return

    for post_id, message in new_posts:
        send_telegram(message)
        save_sent_post(post_id)
        print(f"Sent notification for: {post_id}")

def main():
    print("Monitoring started...")
    sent_posts = load_sent_posts()
    for url in URLS:
        parse_and_notify(url, sent_posts)

if __name__ == "__main__":
    main()
