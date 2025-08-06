import requests
from bs4 import BeautifulSoup
import os
import re
from datetime import datetime
from transformers import pipeline
from urls import urls

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

# Load AI classifier for date relevance
def load_classifier():
    print("[3] Loading AI model... please wait")
    classifier = pipeline("zero-shot-classification", model="valhalla/distilbart-mnli-12-1")
    print("[4] AI model loaded successfully")
    return classifier

classifier = load_classifier()

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

# Extract date from text or URL
def extract_date(text):
    text = text.lower()
    patterns = [
        r'(\d{1,2})[-/](\d{1,2})[-/](\d{4})',       # 06/08/2025 or 06-08-2025
        r'(\d{4})[-/](\d{2})[-/](\d{2})',           # 2025/08/06
        r'(january|february|march|april|may|june|july|august|september|october|november|december)[\s-]+(\d{4})',
        r'(\d{1,2})[.](\d{1,2})[.](\d{4})'          # 06.08.2025
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            try:
                if len(match.groups()) == 3:
                    nums = list(match.groups())
                    if nums[0].isdigit() and nums[1].isdigit():
                        day, month, year = map(int, nums)
                        return datetime(year, month, day).date()
                    elif nums[2].isdigit():
                        year = int(nums[2])
                        month_str = nums[0]
                        month = datetime.strptime(month_str, "%B").month
                        return datetime(year, month, 1).date()
            except Exception:
                continue
    return None

# Check relevance using AI
def is_recent_notification(text):
    try:
        labels = ["recent notification", "old notification"]
        result = classifier(text, labels)
        if result['labels'][0] == "recent notification" and result['scores'][0] > 0.7:
            return True
    except Exception as e:
        print("[AI ERROR]", e)
    return False

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

            date_text = extract_date(text)
            date_url = extract_date(full_link)
            date = date_text or date_url

            if date != today:
                continue

            if full_link not in sent_links:
                if not is_recent_notification(text):
                    print("[10] Skipped by AI filter")
                    continue

                message = (
                    f"<b>{text}</b>\n"
                    f"🔗 <a href=\"{full_link}\">Open Notification</a>\n"
                    f"🌐 Source Page: <a href=\"{url}\">{url}</a>"
                )
                send_telegram(message)
                save_sent_link(full_link)
                sent_links.add(full_link)
            else:
                print("[11] Skipped duplicate link")

    except Exception as e:
        print(f"[ERROR] Failed to scrape {url}: {e}")

# Main entry
def run_monitor():
    sent_links = load_sent_links()
    for url in urls:
        check_site(url, sent_links)

if __name__ == "__main__":
    run_monitor()
    print("[12] Script finished")
