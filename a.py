# main.py
import requests
from bs4 import BeautifulSoup
import os
import re
from datetime import datetime
from url import urls
from transformers import pipeline

# Load environment variables
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
SENT_FILE = "sent_links.txt"

# Load AI classifier for smart filtering and date inference
classifier = pipeline("zero-shot-classification", model="valhalla/distilbart-mnli-12-1")

# Load previously sent links to avoid duplicates
def load_sent_links():
    try:
        with open(SENT_FILE, "r") as f:
            return set(f.read().splitlines())
    except FileNotFoundError:
        return set()

# Save newly sent link to file
def save_sent_link(link):
    with open(SENT_FILE, "a") as f:
        f.write(link + "\n")

# Send message to Telegram bot
def send_telegram(message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }
    try:
        requests.post(url, data=payload)
    except Exception as e:
        print("Telegram Error:", e)

# Try to extract a date from the notification text
def extract_date(text):
    text = text.lower()
    # Pattern: 23-06-2025 or 23/06/2025
    match = re.search(r'(\d{1,2})[-/](\d{1,2})[-/](\d{4})', text)
    if match:
        day, month, year = map(int, match.groups())
        return datetime(year, month, day).date()

    # Pattern: June 2025
    match = re.search(r'(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d{4})', text)
    if match:
        month_str, year = match.groups()
        month = datetime.strptime(month_str, "%B").month
        return datetime(int(year), month, 1).date()

    return None

# Check each website and process valid notifications
def check_site(url, sent_links):
    try:
        r = requests.get(url, timeout=10)
        soup = BeautifulSoup(r.text, "html.parser")
        links = soup.find_all("a")
        current_month = datetime.now().month
        current_year = datetime.now().year

        for link in links:
            text = link.get_text(strip=True)
            href = link.get("href")

            if href and ("notification" in text.lower() or "recruitment" in text.lower()):
                full_link = requests.compat.urljoin(url, href)

                # Try to extract date
                date = extract_date(text)
                if date:
                    # If date is not from the current month/year, skip
                    if date.month != current_month or date.year != current_year:
                        continue
                else:
                    # If no date, ask AI if this seems like a current month notification
                    prompt = f"Is this a job notification for the current month ({datetime.now().strftime('%B %Y')})? Text: '{text}'"
                    result = classifier(prompt, candidate_labels=["Yes", "No"])
                    if result['labels'][0] != "Yes":
                        continue

                # If link is not already sent, send it
                if full_link not in sent_links:
                    send_telegram(f"<b>{text}</b>\n{full_link}")
                    save_sent_link(full_link)
                    sent_links.add(full_link)

    except Exception as e:
        print(f"Error scraping {url}:", e)

# Run the monitoring job
def run_monitor():
    sent_links = load_sent_links()
    for url in urls:
        check_site(url, sent_links)

if __name__ == "__main__":
    run_monitor()
