import requests
from bs4 import BeautifulSoup
import os
import re
from datetime import datetime
from urls import urls
from transformers import pipeline
from dateutil import parser

print("[1] Starting script")

# Load environment variables
TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
SENT_FILE = "sent_links.txt"
print("[2] Environment variables loaded")

# Send message to Telegram bot
def send_telegram(message):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }
    try:
        print(f"[7] Sending Telegram message: {message[:60]}...")
        r = requests.post(url, data=payload)
        print(f"[7.1] Telegram API response: {r.text}")
    except Exception as e:
        print("[ERROR] Telegram Error:", e)

# Notify script has started
send_telegram("✅ Script has started")

# Load AI classifier (currently unused)
print("[3] Loading AI model... please wait")
classifier = pipeline("zero-shot-classification", model="valhalla/distilbart-mnli-12-1")
print("[4] AI model loaded successfully")

# Load sent links
def load_sent_links():
    try:
        with open(SENT_FILE, "r") as f:
            links = set(f.read().splitlines())
            print(f"[5] Loaded {len(links)} sent links")
            return links
    except FileNotFoundError:
        print("[5] sent_links.txt not found, starting fresh")
        return set()

# Save sent link
def save_sent_link(link):
    with open(SENT_FILE, "a") as f:
        f.write(link + "\n")
    print(f"[6] Saved link: {link}")

# Extract date from any text or URL using dateutil (smart parser)
def extract_date(text):
    try:
        date = parser.parse(text, fuzzy=True, dayfirst=True)
        return date.date()
    except Exception:
        return None

# Process each site
def check_site(url, sent_links):
    print(f"[8] Checking site: {url}")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/115.0.0.0 Safari/537.36"
    }
    try:
        r = requests.get(url, timeout=20, headers=headers)
        soup = BeautifulSoup(r.text, "html.parser")
        links = soup.find_all("a")
        print(f"[9] Found {len(links)} links")

        today = datetime.now().date()

        for link in links:
            text = link.get_text(strip=True)
            href = link.get("href")
            if not href:
                continue

            full_link = requests.compat.urljoin(url, href)

            # Extract date from either text or link
            date_text = extract_date(text)
            date_url = extract_date(full_link)
            date = date_text or date_url

            print(f"[10] Link: {text[:60]} | Date found: {date}")

            # Only send if date is today
            if not date or date != today:
                continue

            # Send Telegram if not already sent
            if full_link not in sent_links:
                message = (
                    f"<b>New notification ({today.strftime('%d-%m-%Y')})</b>\n"
                    f"Page: {url}\n"
                    f"Link: {full_link}"
                )
                send_telegram(message)
                save_sent_link(full_link)
                sent_links.add(full_link)
            else:
                print("[15] Skipped duplicate link")

    except Exception as e:
        print(f"[ERROR] Failed to scrape {url}: {e}")

# Main entry point
def run_monitor():
    sent_links = load_sent_links()
    for url in urls:
        check_site(url, sent_links)

if __name__ == "__main__":
    run_monitor()
    print("[16] Script finished")
