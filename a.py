# main.py
import requests
from bs4 import BeautifulSoup
import os
import re
from datetime import datetime, timedelta
from urls import urls
from transformers import pipeline

print("[1] Starting script")

# Load environment variables
TOKEN = os.getenv("TOKEN")  # Telegram bot token from environment
CHAT_ID = os.getenv("CHAT_ID")  # Telegram chat/channel ID from environment
SENT_FILE = "sent_links.txt"  # File to track already sent links
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

# Load AI classifier
print("[3] Loading AI model... please wait")
classifier = pipeline("zero-shot-classification", model="valhalla/distilbart-mnli-12-1")
print("[4] AI model loaded successfully")

# Load sent links to avoid duplication
def load_sent_links():
    try:
        with open(SENT_FILE, "r") as f:
            links = set(f.read().splitlines())
            print(f"[5] Loaded {len(links)} sent links")
            return links
    except FileNotFoundError:
        print("[5] sent_links.txt not found, starting fresh")
        return set()

# Save a sent link to avoid resending it
def save_sent_link(link):
    with open(SENT_FILE, "a") as f:
        f.write(link + "\n")
    print(f"[6] Saved link: {link}")

# Extract date from any text string
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

# Check if date is in the current week
def is_current_week(date):
    if not date:
        return False
    today = datetime.now().date()
    start_of_week = today - timedelta(days=today.weekday())
    end_of_week = start_of_week + timedelta(days=6)
    return start_of_week <= date <= end_of_week

# Use AI to judge if a notification is important
def is_important_with_ai(text):
    try:
        labels = ["important", "not important"]
        result = classifier(text, candidate_labels=labels)
        score = result['scores'][0]
        print(f"[13] AI decision: {result['labels'][0]} ({score:.2f})")
        return result['labels'][0] == "important" and score > 0.5
    except Exception as e:
        print("[ERROR] AI classification failed:", e)
        return False

# Process each URL
def check_site(url, sent_links):
    print(f"[8] Checking site: {url}")
    try:
        r = requests.get(url, timeout=10)
        soup = BeautifulSoup(r.text, "html.parser")
        links = soup.find_all("a")
        print(f"[9] Found {len(links)} links")

        keywords = [
            "notification", "recruitment", "notice", "result", "results",
            "admission", "admit card", "released", "vacancy", "post",
            "posts", "examination", "vacancies", "declared", "interview",
            "answer key", "important", "advt."
        ]

        for link in links:
            text = link.get_text(strip=True)
            href = link.get("href")

            if href and any(kw in text.lower() for kw in keywords):
                full_link = requests.compat.urljoin(url, href)
                print(f"[10] Potential match: {text}")

                # Try extracting date from both text and URL
                date_text = extract_date(text)
                date_url = extract_date(full_link)
                date = date_text or date_url

                # Send if date is in current week
                if date:
                    print(f"[11] Extracted date: {date}")
                    if not is_current_week(date):
                        print("[12] Skipped due to not in current week")
                        continue
                else:
                    print("[12] No date found, using AI to decide")
                    if not is_important_with_ai(text):
                        print("[12.1] Skipped due to AI decision")
                        continue

                if full_link not in sent_links:
                    send_telegram(f"<b>{text}</b>\n{full_link}")
                    save_sent_link(full_link)
                    sent_links.add(full_link)
                else:
                    print("[15] Skipped duplicate link")

    except Exception as e:
        print(f"[ERROR] Failed to scrape {url}: {e}")

# Main entry point
def run_monitor():
    sent_links = load_sent_links()  # Load already sent links
    for url in urls:
        check_site(url, sent_links)  # Check each site

# Start the script
if __name__ == "__main__":
    run_monitor()
    print("[16] Script finished")
