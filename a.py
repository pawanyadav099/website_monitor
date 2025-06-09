import requests
import httpx
from bs4 import BeautifulSoup
import urllib3
import os
from datetime import datetime
from dateutil.parser import parse as date_parse
from urllib.parse import urljoin, urlparse
import fitz  # PyMuPDF

from url import URLS  # Make sure this file exists with valid URL list

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
    return any(keyword in text.lower() for keyword in KEYWORDS)

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
    except requests.exceptions.SSLError:
        try:
            with httpx.Client(verify=False, timeout=10) as client:
                resp = client.get(url)
                resp.raise_for_status()
                return resp.text
        except:
            return None
    except:
        return None

def extract_date_from_text(text):
    try:
        return date_parse(text, fuzzy=True, dayfirst=True)
    except:
        return None

def extract_date_from_pdf(pdf_url):
    try:
        r = requests.get(pdf_url, stream=True, timeout=10)
        if r.status_code != 200:
            return None
        with open("temp.pdf", "wb") as f:
            f.write(r.content)

        doc = fitz.open("temp.pdf")
        text = ""
        for page in doc:
            text += page.get_text()

        doc.close()
        os.remove("temp.pdf")

        return extract_date_from_text(text)
    except Exception as e:
        print(f"PDF extract error: {e}")
        return None

def extract_date(a_tag, url):
    now = datetime.now()
    possible_sources = []

    title_attr = a_tag.get('title')
    if title_attr:
        possible_sources.append(title_attr)

    sibling = a_tag.find_next_sibling(text=True)
    if sibling:
        possible_sources.append(sibling.strip())

    if a_tag.parent:
        possible_sources.append(a_tag.parent.get_text(" ", strip=True))
    if a_tag.parent and a_tag.parent.parent:
        possible_sources.append(a_tag.parent.parent.get_text(" ", strip=True))

    for text in possible_sources:
        dt = extract_date_from_text(text)
        if dt and dt.year >= now.year and dt.month >= now.month:
            return dt

    href = a_tag['href']
    if href.lower().endswith(".pdf"):
        pdf_date = extract_date_from_pdf(href)
        if pdf_date and pdf_date.year >= now.year and pdf_date.month >= now.month:
            return pdf_date

    # Try URL
    path = urlparse(href).path
    for part in path.split("/"):
        dt = extract_date_from_text(part)
        if dt and dt.year >= now.year and dt.month >= now.month:
            return dt

    return "Date not found"

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
        if not text or not href or not contains_keyword(text):
            continue

        if href.startswith("/"):
            href = urljoin(url, href)

        post_id = text.strip() + "|" + href.strip()
        if post_id in sent_posts:
            continue

        date_info = extract_date(a, url)
        date_str = date_info.strftime("%Y-%m-%d") if isinstance(date_info, datetime) else date_info

        if href.lower().endswith(".pdf"):
            message = f"📄 New PDF:\nTitle: {text}\nLink: {href}\nDate: {date_str}"
        else:
            message = f"📝 New Post:\nTitle: {text}\nLink: {href}\nDate: {date_str}"

        new_posts.append((post_id, message))

    for post_id, message in new_posts:
        send_telegram(message)
        save_sent_post(post_id)
        print(f"Sent: {post_id}")

def main():
    print("Monitoring started...")
    sent_posts = load_sent_posts()
    for url in URLS:
        parse_and_notify(url, sent_posts)

if __name__ == "__main__":
    main()
