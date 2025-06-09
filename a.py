import requests
import httpx
from bs4 import BeautifulSoup
import urllib3
import os
from datetime import datetime
from dateutil.parser import parse as date_parse

from url import URLS  # ✅ yahi import hai URL list ke liye

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
    data = {"chat_id": CHAT_ID, "text": message}
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

def extract_post_date(a_tag):
    now = datetime.now()
    candidates = []

    title_attr = a_tag.get('title')
    if title_attr:
        candidates.append(title_attr)

    next_sibling = a_tag.find_next_sibling(text=True)
    if next_sibling and isinstance(next_sibling, str):
        candidates.append(next_sibling.strip())

    parent_text = a_tag.parent.get_text(" ", strip=True) if a_tag.parent else ""
    candidates.append(parent_text)

    for text in candidates:
        try:
            dt = date_parse(text, fuzzy=True, dayfirst=True)
            if dt.year == now.year and dt.month == now.month:
                return dt
        except Exception:
            continue

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

        # ✅ More reliable post_id: text + href
        post_id = f"{text.strip()}|{href.strip()}"

        if post_id in sent_posts:
            continue

        is_pdf = href.lower().endswith(".pdf")
        post_date = extract_post_date(a)
        if not post_date:
            continue

        if is_pdf:
            message = f"New PDF detected on {url}:\nTitle: {text}\nPDF Link: {href}\nDate: {post_date.strftime('%Y-%m-%d')}"
        else:
            message = f"New post on {url}:\nTitle: {text}\nLink: {href}\nDate: {post_date.strftime('%Y-%m-%d')}"

        new_posts.append((post_id, message))

    if not new_posts:
        print(f"No new relevant posts found on {url} for current month")
        return

    for post_id, message in new_posts:
        send_telegram(message)
        save_sent_post(post_id)
        sent_posts.add(post_id)  # ✅ important to prevent same run duplicates
        print(f"Sent notification for: {post_id}")

def main():
    print("Monitoring started...")
    sent_posts = load_sent_posts()
    for url in URLS:
        parse_and_notify(url, sent_posts)

if __name__ == "__main__":
    main()
