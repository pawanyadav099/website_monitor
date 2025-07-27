import requests
from bs4 import BeautifulSoup
import os
import re
from datetime import datetime
from urls import urls
from transformers import pipeline

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

# Extract date from any text or URL
def extract_date(text):
    text = text.lower()

    # DD-MM-YYYY or DD/MM/YYYY
    match = re.search(r'(\d{1,2})[-/](\d{1,2})[-/](\d{4})', text)
    if match:
        day, month, year = map(int, match.groups())
        return datetime(year, month, day).date()

    # Month YYYY
    match = re.search(r'(january|february|march|april|may|june|july|august|september|october|november|december)[\s-]+(\d{4})', text)
    if match:
        month_str, year = match.groups()
        month = datetime.strptime(month_str, "%B").month
        return datetime(int(year), month, 1).date()

    # YYYYMMDD or YYYY-MM-DD
    match = re.search(r'(\d{4})[-/]?(\d{2})[-/]?(\d{2})', text)
    if match:
        year, month, day = map(int, match.groups())
        if 1 <= month <= 12 and 1 <= day <= 31:
            return datetime(year, month, day).date()

    return None

# Process each site
def check_site(url, sent_links):
    print(f"[8] Checking site: {url}")
    try:
        r = requests.get(url, timeout=10)
        soup = BeautifulSoup(r.text, "html.parser")
        links = soup.find_all("a")
        print(f"[9] Found {len(links)} links")

        today = datetime.now().date()

        keywords = [
            "notification", "recruitment", "notice", "result", "results",
            "admission", "admit card", "released", "vacancy", "post",
            "posts", "examination", "vacancies", "declared", "interview", "Advt.",
            "answer key", "important"
        ]

        for link in links:
            text = link.get_text(strip=True)
            href = link.get("href")

            if href and any(kw in text.lower() for kw in keywords):
                full_link = requests.compat.urljoin(url, href)
                print(f"[10] Potential match: {text}")

                # Extract date from title and URL
                date_text = extract_date(text)
                date_url = extract_date(full_link)
                date = date_text or date_url

                if date:
                    print(f"[11] Extracted date: {date}")
                    if date != today:
                        print("[12] Skipped because it's not today's date")
                        continue
                else:
                    print("[12] Skipped due to no date found")
                    continue

                if full_link not in sent_links:
                    send_telegram(f"<b>{text}</b>\n{full_link}")
                    save_sent_link(full_link)
                    sent_links.add(full_link)
                else:
                    print("[15] Skipped duplicate link")

    except Exception as e:
        print(f"[ERROR] Failed to scrape {url}: {e}")

# Main entry
def run_monitor():
    sent_links = load_sent_links()
    for url in urls:
        check_site(url, sent_links)

if __name__ == "__main__":
    run_monitor()
    print("[16] Script finished")
