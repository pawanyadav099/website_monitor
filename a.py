import configparser
import logging
import sqlite3
import os
import re
import time
from datetime import datetime
from typing import Optional, List, Tuple
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
KEYWORDS = config.get('DEFAULT', 'keywords', fallback="recruitment,vacancy,exam,admit card,notification,interview,application,results,answer key").split(',')
EXCLUDE_WORDS = ["home", "about us", "contact", "main navigation", "menu"]
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

def send_telegram(message: str, url: str) -> bool:
    """Send a message to Telegram without escaping text or date."""
    telegram_url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    lines = message.split('\n')
    formatted_lines = [lines[0]]  # *New Notification*
    formatted_lines.append("")  # Blank line
    for line in lines[2:]:
        if line.startswith("Website:"):
            formatted_lines.append(f"Website: {url}")
        else:
            formatted_lines.append(line)  # No escaping
    formatted_message = "\n".join(formatted_lines)[:4096]  # Telegram's max length
    data = {
        "chat_id": CHAT_ID,
        "text": formatted_message,
        "parse_mode": "Markdown"
    }
    for attempt in range(1, 4):
        try:
            resp = requests.post(telegram_url, json=data, timeout=30)
            resp.raise_for_status()
            logger.info("Telegram message sent", message=formatted_message[:50])
            time.sleep(1.0)  # Avoid rate limiting
            return True
        except requests.RequestException as e:
            logger.warning("Telegram send failed", attempt=attempt, error=str(e))
            time.sleep(2 ** attempt)
    logger.error("Failed to send Telegram message after retries", message=formatted_message[:50])
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
    """Extract a date from a text string with strict patterns."""
    if not text:
        return None
    # Month names for validation
    month_names = r'(?:January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)'
    date_patterns = [
        r'\b(\d{1,2}[-/\s]\d{1,2}[-/\s]\d{4})\b',  # DD-MM-YYYY or DD/MM/YYYY
        r'\b(\d{4}[-/\s]\d{1,2}[-/\s]\d{1,2})\b',  # YYYY-MM-DD or YYYY/MM/DD
        rf'\b(\d{{1,2}}\s+{month_names}\s+\d{{4}})\b',  # DD Month YYYY
        rf'\b(\d{{1,2}}(?:st|nd|rd|th)?\s+{month_names}\s+\d{{4}})\b',  # DD(st/nd) Month YYYY
        rf'\b(\d{{1,2}}-{month_names}-\d{{4}})\b',  # DD-Month-YYYY
        rf'\b({month_names}\s+\d{{1,2}},?\s+\d{{4}})\b',  # Month DD, YYYY
        r'\b(\d{4}/\d{2}/\d{2})\b',  # YYYY/MM/DD
        r'\b(\d{2}/\d{2}/\d{4})\b',  # MM/DD/YYYY or DD/MM/YYYY
    ]
    for pattern in date_patterns:
        match = re.search(pattern, text, re.I)
        if match:
            try:
                dt = date_parse(match.group(0), dayfirst=True)
                if 2000 <= dt.year <= datetime.now().year + 1:
                    # Validate month in text
                    text_lower = text.lower()
                    month_str = dt.strftime('%B').lower()[:3]
                    if month_str in text_lower or dt.strftime('%m') in text_lower:
                        logger.debug("Extracted date", date=dt, text=text[:50])
                        return dt
            except ValueError:
                continue
    logger.debug("No valid date found", text=text[:50])
    return None

def is_notification(text: str) -> bool:
    """Check if text is a valid notification."""
    if not text or len(text) < 20 or len(text) > 500:
        return False
    text_lower = text.lower()
    # Exclude navigational or generic text
    if any(word in text_lower for word in EXCLUDE_WORDS):
        return False
    # Require at least one primary keyword
    primary_keywords = ["recruitment", "vacancy", "exam", "admit card", "interview", "application", "results", "answer key"]
    has_primary = any(keyword in text_lower for keyword in primary_keywords)
    return has_primary

def normalize_text(text: str) -> str:
    """Normalize text for duplicate checking."""
    return re.sub(r'\s+', ' ', text.strip()).lower()

def parse_notifications(url: str, sent_posts: set) -> List[Tuple[str, str]]:
    """Parse a URL's HTML for new notifications in the current month."""
    logger.info("Processing URL", url=url)
    html = fetch_page(url)
    if not html:
        logger.error("No HTML content fetched, skipping notifications", url=url)
        return []

    soup = BeautifulSoup(html, 'html.parser')
    notifications = []
    now = datetime.now()
    current_year, current_month = now.year, now.month
    logger.info("Filtering notifications for current month", year=current_year, month=current_month)

    # Focus on notification-specific tags
    for element in soup.find_all(['li', 'p', 'a']):
        text = element.get_text(strip=True)
        if not is_notification(text):
            continue

        # Normalize text for duplicate checking
        normalized_text = normalize_text(text)
        if not normalized_text:
            continue

        # Generate a unique ID
        notification_id = f"{url}:{hash(normalized_text)}".lower()
        if notification_id in sent_posts:
            logger.info("Skipping already sent notification", id=notification_id, text=text[:50])
            continue

        # Extract date from element or <time> tag
        date = None
        candidates = [text]
        time_tag = element.find('time') or element.find_parent('time') or element.find_next('time')
        if time_tag:
            candidates.append(time_tag.get_text(strip=True))
            if time_tag.get('datetime'):
                candidates.append(time_tag.get('datetime'))
        for candidate in candidates:
            dt = extract_date_from_text(candidate)
            if dt:
                date = dt
                break

        # Only include notifications in the current month
        if date and date.year == current_year and date.month == current_month:
            # Validate month in text
            text_lower = text.lower()
            month_str = date.strftime('%B').lower()[:3]
            if month_str in text_lower or date.strftime('%m') in text_lower:
                msg = f"*New Notification*\n\n"
                msg += f"Website: {url}\n"
                msg += f"Text: {text}\n"
                msg += f"Date: {date.strftime('%Y-%m-%d')}\n"
                notifications.append((notification_id, msg))
            else:
                logger.debug("Skipping notification with mismatched month in text", text=text[:50], date=date)
        else:
            logger.debug("Skipping notification not in current month", text=text[:50], date=date)

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
            # Basic URL validation
            if not re.match(r'^https?://[a-zA-Z0-9\-\.]+\.[a-zA-Z]{2,}/?.*$', url):
                logger.error("Invalid URL format, skipping", url=url)
                continue
            notifications = parse_notifications(url, sent_posts)
            for notification_id, message in notifications:
                if send_telegram(message, url):
                    save_sent_post(notification_id)
                    sent_posts.add(notification_id)
                    logger.info("Sent notification", id=notification_id, message=message[:50])
                else:
                    logger.error("Failed to send notification", id=notification_id)
        except Exception as e:
            logger.error("Error processing URL", url=url, error=str(e))
        time.sleep(2)

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Website Notification Monitor")
    parser.add_argument('--urls', nargs='+', default=URLS, help="URLs to monitor")
    parser.add_argument('--log-level', default='INFO', choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'], help="Logging level")
    args = parser.parse_args()

    logging.getLogger().setLevel(args.log_level)
    main(args.urls)
