import configparser
import logging
import sqlite3
import os
import re
import time
from datetime import datetime
import requests
from bs4 import BeautifulSoup
from charset_normalizer import detect
from dateutil.parser import parse as date_parse
from dotenv import load_dotenv
import structlog
from url import URLS  # List of URLs to monitor

# Configure structured logging
structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.stdlib.add_log_level,
        structlog.processors.JSONRenderer()
    ],
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    wrapper_class=structlog.stdlib.BoundLogger,
    cache_logger_on_first_use=True,
)
logger = structlog.get_logger()

# Load configuration
config = configparser.ConfigParser()
config.read('config.ini')

# Load environment variables
load_dotenv()
TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# Configuration defaults
TIMEOUT = int(config.get('DEFAULT', 'timeout', fallback=60))
HEADERS = {
    "User-Agent": config.get('DEFAULT', 'user_agent', fallback="Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
}
KEYWORDS = config.get('DEFAULT', 'keywords', fallback="job,result,notification,admit card,notice,exam,interview,vacancy,recruitment,call letter,application,schedule").split(',')
SENT_POSTS_DB = config.get('DEFAULT', 'sent_posts_db', fallback="sent_messages.db")

# Validate environment variables
if not TOKEN or not CHAT_ID:
    logger.error("Missing Telegram TOKEN or CHAT_ID", token_set=bool(TOKEN), chat_id_set=bool(CHAT_ID))
    exit(1)
else:
    logger.info("Telegram configured", chat_id=CHAT_ID, token_prefix=TOKEN[:5] + "***")

def init_db():
    """Initialize SQLite database for sent notifications."""
    with sqlite3.connect(SENT_POSTS_DB) as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS notifications (id TEXT PRIMARY KEY, sent_at TIMESTAMP)')
        conn.commit()
    logger.info("Initialized SQLite DB", db_file=SENT_POSTS_DB)

def load_sent_posts() -> set:
    """Load sent notification IDs from SQLite."""
    try:
        with sqlite3.connect(SENT_POSTS_DB) as conn:
            cursor = conn.execute('SELECT id FROM notifications')
            sent = {row[0] for row in cursor.fetchall()}
            logger.info("Loaded sent notifications", count=len(sent))
            return sent
    except sqlite3.Error as e:
        logger.error("Failed to load sent notifications", error=str(e))
        return set()

def save_sent_post(notification_id: str):
    """Save a notification ID to SQLite."""
    try:
        with sqlite3.connect(SENT_POSTS_DB) as conn:
            conn.execute('INSERT INTO notifications (id, sent_at) VALUES (?, ?)',
                         (notification_id, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
            conn.commit()
        logger.info("Saved notification ID", id=notification_id)
    except sqlite3.Error as e:
        logger.error("Failed to save notification ID", id=notification_id, error=str(e))

def escape_markdown(text: str) -> str:
    """Escape Markdown special characters for Telegram."""
    if not text:
        return ""
    characters = r'[*_[\]()~`>#+\-=|{}!.]'
    return re.sub(characters, r'\\1', text)

def send_telegram(message: str) -> bool:
    """Send a message to Telegram."""
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    data = {
        "chat_id": CHAT_ID,
        "text": escape_markdown(message)[:4096],  # Telegram's max message length
        "parse_mode": "Markdown"
    }
    for attempt in range(1, 4):
        try:
            resp = requests.post(url, json=data, timeout=30)
            resp.raise_for_status()
            logger.info("Telegram message sent", message=message[:50])
            time.sleep(1.0)  # Avoid rate limiting
            return True
        except requests.RequestException as e:
            logger.warning("Telegram send failed", attempt=attempt, error=str(e))
            time.sleep(2 ** attempt)
    logger.error("Failed to send Telegram message after retries", message=message[:50])
    return False

def fetch_page(url: str) -> Optional[str]:
    """Fetch HTML content of a URL."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT, verify=False)  # No SSL verification
        resp.raise_for_status()
        detected = detect(resp.content)
        encoding = detected['encoding'] or 'utf-8'
        text = resp.content.decode(encoding, errors='ignore')
        logger.info("Fetched page", url=url)
        time.sleep(2)  # Avoid overwhelming servers
        return text
    except requests.RequestException as e:
        logger.error("Failed to fetch URL", url=url, error=str(e))
        return None

def extract_date_from_text(text: str) -> Optional[datetime]:
    """Extract a date from a text string."""
    if not text:
        return None
    date_patterns = [
        r'\b(\d{1,2}[-/\s]\d{1,2}[-/\s]\d{2,4})\b',  # DD-MM-YYYY or DD/MM/YYYY
        r'\b(\d{4}[-/\s]\d{1,2}[-/\s]\d{1,2})\b',  # YYYY-MM-DD or YYYY/MM/DD
        r'\b(\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{2,4})\b',
        r'\b(\d{1,2}(?:st|nd|rd|th)?\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{2,4})\b',
        r'\b(\d{1,2}-(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)-\d{2,4})\b',
    ]
    for pattern in date_patterns:
        match = re.search(pattern, text, re.I)
        if match:
            try:
                dt = date_parse(match.group(0), dayfirst=True)
                if 2000 <= dt.year <= datetime.now().year + 1:
                    logger.debug("Extracted date", date=dt, text=text[:50])
                    return dt
            except ValueError:
                continue
    try:
        dt = date_parse(text, fuzzy=True, dayfirst=True)
        if 2000 <= dt.year <= datetime.now().year + 1:
            logger.debug("Extracted fuzzy date", date=dt, text=text[:50])
            return dt
    except ValueError:
        pass
    return None

def is_notification(text: str) -> bool:
    """Check if text contains notification keywords."""
    if not text:
        return False
    text_lower = text.lower()
    return any(keyword in text_lower for keyword in KEYWORDS)

def parse_notifications(url: str, sent_posts: set) -> List[Tuple[str, str]]:
    """Parse a URL's HTML for new notifications in the current month."""
    logger.info("Processing URL", url=url)
    html = fetch_page(url)
    if not html:
        send_telegram(f"Error: Failed to fetch {url}")
        return []

    soup = BeautifulSoup(html, 'html.parser')
    notifications = []
    current_month_start = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)  # June 1, 2025
    logger.info("Filtering notifications for current month", start=current_month_start)

    # Check all elements for notifications
    for element in soup.find_all(['p', 'div', 'li', 'span', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6']):
        text = element.get_text(strip=True)
        if not is_notification(text):
            continue

        # Generate a unique ID for the notification
        notification_id = f"{url}:{text[:100]}".lower()
        if notification_id in sent_posts:
            logger.info("Skipping already sent notification", id=notification_id)
            continue

        # Extract date from element or parent
        date = None
        candidates = [text]
        if element.parent:
            candidates.append(element.parent.get_text(strip=True))
        for candidate in candidates:
            dt = extract_date_from_text(candidate)
            if dt:
                date = dt
                break

        # Filter for current month
        if date and date >= current_month_start:
            msg = f"*New Notification*\n\n"
            msg += f"Website: {url}\n"
            msg += f"Text: {text}\n"
            msg += f"Date: {date.strftime('%Y-%m-%d')}\n"
            notifications.append((notification_id, msg))
        elif not date:
            # Send notification if no date is found but it's likely new
            msg = f"*New Notification*\n\n"
            msg += f"Website: {url}\n"
            msg += f"Text: {text}\n"
            notifications.append((notification_id, msg))

    logger.info("Found notifications", url=url, count=len(notifications))
    return notifications

def main(urls: List[str] = URLS):
    """Monitor URLs for new notifications and send to Telegram."""
    init_db()
    logger.info("Monitoring started")
    sent_posts = load_sent_posts()

    for url in urls:
        if not url:
            logger.error("Empty URL, skipping")
            continue
        if not url.startswith(('http://', 'https://')):
            url = f"https://{url}"
            logger.info("Prepended https:// to URL", url=url)

        try:
            notifications = parse_notifications(url, sent_posts)
            for notification_id, message in notifications:
                if send_telegram(message):
                    save_sent_post(notification_id)
                    sent_posts.add(notification_id)
                    logger.info("Sent notification", id=notification_id)
                else:
                    logger.error("Failed to send notification", id=notification_id)
        except Exception as e:
            logger.error("Error processing URL", url=url, error=str(e))
            send_telegram(f"Error processing {url}: {str(e)}")
        time.sleep(2)

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Website Notification Monitor")
    parser.add_argument('--urls', nargs='+', default=URLS, help="URLs to monitor")
    parser.add_argument('--log-level', default='INFO', choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'], help="Logging level")
    args = parser.parse_args()

    logging.getLogger().setLevel(args.log_level)
    main(args.urls)
