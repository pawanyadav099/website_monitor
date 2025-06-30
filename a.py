# main.py
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

# Load AI classifier for smart filtering and date inference
print("[3] Loading AI model... please wait")
classifier = pipeline("zero-shot-classification", model="valhalla/distilbart-mnli-12-1")
print("[4] AI model loaded successfully")

# Load previously sent links to avoid duplicates
def load_sent_links():
    try:
        with open(SENT_FILE, "r") as f:
            links = set(f.read().splitlines())
            print(f"[5] Loaded {len(links)} sent links")
            return links
    except FileNotFoundError:
        print("[5] sent_links.txt not found, starting fresh")
        return set()

# Save newly sent link to file
def save_sent_link(link):
    with open(SENT_FILE, "a") as f:
        f.write(link + "\n")
    print(f"[6] Saved link: {link}")

# Send message to Telegram bot
def send_telegram(message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }
    try:
        print(f"[7] Sending Telegram message: {message[:60]}...")
        r = requests.post(url, data=payload)
        print(f"[7.1] Telegram API response: {r.text}")  # Show response for debugging
    except Exception as e:
        print("[ERROR] Telegram Error:", e)

# Try to extract a date from the notification text
def extract_date(text):
    text = text.lower()
    match = re.search(r'(\d{1,2})[-/](\d{1,2})[-/](\d{4})', text)
    if match:
        day, month, year = map(int, match.groups())
        return datetime(year, month, day).date()

    match = re.search(r'(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d{4})', text)
    if match:
        month_str, year = match.groups()
        month = datetime.strptime(month_str, "%B").month
        return datetime(int(year), month, 1).date()

    return None

# Check each website and process valid notifications
def check_site(url, sent_links):
    print(f"[8] Checking site: {url}")
    try:
        r = requests.get(url, timeout=10)
        soup = BeautifulSoup(r.text, "html.parser")
        links = soup.find_all("a")
        print(f"[9] Found {len(links)} links")

        current_month = datetime.now().month
        current_year = datetime.now().year

        keywords = [
            "notification", "recruitment", "notice", "result", "results",
            "admission", "admit card", "released", "vacancy", "post",
            "posts", "examination", "vacancies", "declared", "interview",
            "answer key", "important"
        ]

        for link in links:
            text = link.get_text(strip=True)
            href = link.get("href")

            if href and any(kw in text.lower() for kw in keywords):
                full_link = requests.compat.urljoin(url, href)
                print(f"[10] Potential match: {text}")

                date = extract_date(text)
                if date:
                    print(f"[11] Extracted date: {date}")
                    if date.month != current_month or date.year != current_year:
                        print("[12] Skipped due to non-current month")
                        continue
                else:
                    prompt = f"Is this a job notification for the current month ({datetime.now().strftime('%B %Y')})? Text: '{text}'"
                    result = classifier(prompt, candidate_labels=["Yes", "No"])
                    print(f"[13] AI decision: {result['labels'][0]} ({result['scores'][0]:.2f})")
                    if result['labels'][0] != "Yes":
                        print("[14] Skipped by AI")
                        continue

                if full_link not in sent_links:
                    send_telegram(f"<b>{text}</b>\n{full_link}")
                    save_sent_link(full_link)
                    sent_links.add(full_link)
                else:
                    print("[15] Skipped duplicate link")

    except Exception as e:
        print(f"[ERROR] Failed to scrape {url}: {e}")

# Run the monitoring job
def run_monitor():
    sent_links = load_sent_links()
    for url in urls:
        check_site(url, sent_links)

if __name__ == "__main__":
    run_monitor()
    print("[16] Script finished")
