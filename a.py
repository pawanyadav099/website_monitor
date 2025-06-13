import asyncio
import configparser
import logging
import sqlite3
import os
import re
import time
from datetime import datetime, timedelta
from io import BytesIO
from urllib.parse import urljoin, urlparse
from typing import List, Set, Optional, Tuple

import httpx
from bs4 import BeautifulSoup
from charset_normalizer import detect
from dateutil.parser import parse as date_parse
from dotenv import load_dotenv
from filelock import FileLock
import pdfplumber
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
TIMEOUT = int(config.get('DEFAULT', 'timeout', fallback=15))
PDF_TIMEOUT = int(config.get('DEFAULT', 'pdf_timeout', fallback=20))
HEADERS = {
    "User-Agent": config.get('DEFAULT', 'user_agent', fallback="Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
}
KEYWORDS = config.get('DEFAULT', 'keywords', fallback="job,result,notification,admit card,notice,exam,interview,vacancy,recruitment,call letter,merit list,schedule,announcement,bulletin").split(',')
SENT_POSTS_DB = config.get('DEFAULT', 'sent_posts_db', fallback="sent_posts.db")
LOCK_FILE = config.get('DEFAULT', 'lock_file', fallback="website_monitor.lock")
TELEGRAM_RATE_LIMIT = float(config.get('DEFAULT', 'telegram_rate_limit', fallback=0.1))  # Seconds between messages

# Validate environment variables
if not TOKEN or not CHAT_ID:
    logger.error("Missing Telegram TOKEN or CHAT_ID", token_set=bool(TOKEN), chat_id_set=bool(CHAT_ID))
    exit(1)
else:
    logger.info("Successfully loaded TOKEN and CHAT_ID")

def init_db():
    """Initialize SQLite database for sent posts."""
    with sqlite3.connect(SENT_POSTS_DB) as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS sent_posts (post_id TEXT PRIMARY KEY, saved_at TIMESTAMP)')
        conn.commit()
    logger.info("Initialized SQLite database", db_file=SENT_POSTS_DB)

def load_sent_posts() -> Set[str]:
    """Load previously sent post IDs from SQLite."""
    try:
        with sqlite3.connect(SENT_POSTS_DB) as conn:
            cursor = conn.execute('SELECT post_id FROM sent_posts')
            sent_posts = {row[0] for row in cursor.fetchall()}
        logger.info("Loaded sent posts", count=len(sent_posts))
        return sent_posts
    except Exception as e:
        logger.error("Error loading sent posts", error=str(e))
        return set()

def save_sent_post(post_id: str):
    """Save a post ID to SQLite."""
    try:
        with sqlite3.connect(SENT_POSTS_DB) as conn:
            conn.execute('INSERT OR IGNORE INTO sent_posts (post_id, saved_at) VALUES (?, ?)',
                         (post_id.lower(), datetime.now()))
            conn.commit()
        logger.info("Saved post ID", post_id=post_id)
    except Exception as e:
        logger.error("Error saving post ID", post_id=post_id, error=str(e))

async def send_telegram(message: str, client: httpx.AsyncClient) -> bool:
    """Send a message to Telegram with rate-limiting."""
    if not TOKEN or not CHAT_ID:
        logger.error("Telegram TOKEN or CHAT_ID missing")
        return False
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    data = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": False
    }
    for attempt in range(3):
        try:
            resp = await client.post(url, json=data, timeout=TIMEOUT)
            resp.raise_for_status()
            logger.info("Telegram message sent", message=message[:50])
            await asyncio.sleep(TELEGRAM_RATE_LIMIT)
            return True
        except httpx.RequestException as e:
            logger.warning("Telegram send failed", attempt=attempt + 1, error=str(e))
            await asyncio.sleep(2 ** attempt)
    logger.error("Failed to send Telegram message after retries", message=message[:50])
    return False

async def fetch(url: str, client: httpx.AsyncClient) -> Optional[str]:
    """Fetch HTML content asynchronously with encoding detection."""
    try:
        resp = await client.get(url, headers=HEADERS)
        resp.raise_for_status()
        detected = detect(resp.content)
        encoding = detected['encoding'] or 'utf-8'
        text = resp.content.decode(encoding)
        logger.info("Fetched URL", url=url, status=resp.status_code)
        await asyncio.sleep(2)
        return text
    except Exception as e:
        logger.error("Error fetching URL", url=url, error=str(e))
        return None

def extract_date_from_text(text: str) -> Optional[datetime]:
    """Extract a date from text using various date patterns."""
    if not text:
        return None
    date_patterns = [
        r'\b(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\b',
        r'\b(\d{4}[/-]\d{1,2}[/-]\d{1,2})\b',
        r'\b(\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{2,4})\b',
        r'\b(\d{1,2}(?:st|nd|rd|th)?\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{2,4})\b',
        r'\b(\d{1,2}-(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)-\d{2,4})\b',
    ]
    for pattern in date_patterns:
        match = re.search(pattern, text, re.I)
        if match:
            try:
                dt = date_parse(match.group(1), dayfirst=True)
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

async def extract_date_from_pdf(url: str, client: httpx.AsyncClient) -> Optional[datetime]:
    """Extract a date from the first few pages of a PDF."""
    try:
        resp = await client.get(url, headers=HEADERS, timeout=PDF_TIMEOUT)
        resp.raise_for_status()
        with pdfplumber.open(BytesIO(resp.content)) as pdf:
            for page in pdf.pages[:3]:  # Limit to first 3 pages
                text = page.extract_text()
                if text:
                    dt = extract_date_from_text(text)
                    if dt:
                        logger.info("Extracted PDF date", date=dt, url=url)
                        return dt
        return None
    except Exception as e:
        logger.error("Error processing PDF", url=url, error=str(e))
        return None

def extract_date(a_tag: BeautifulSoup) -> Optional[datetime]:
    """Extract a date from an <a> tag's attributes or surrounding text."""
    candidates = []
    if a_tag.get('title'):
        candidates.append(a_tag.get('title'))
    next_sibling = a_tag.find_next_sibling(string=True)
    if next_sibling and isinstance(next_sibling, str):
        candidates.append(next_sibling.strip())
    if a_tag.parent:
        candidates.append(a_tag.parent.get_text(strip=True))
        if a_tag.parent.parent:
            candidates.append(a_tag.parent.parent.get_text(strip=True))
    for text in candidates:
        dt = extract_date_from_text(text)
        if dt:
            logger.debug("Extracted date from <a> tag", date=dt, text=text[:50])
            return dt
    return None

def extract_date_from_url(url: str) -> Optional[datetime]:
    """Extract a date from a URL using common date patterns."""
    patterns = [
        r'/(\d{4})[/-](\d{1,2})[/-]?(\d{1,2})?',
        r'date=(\d{4})-(\d{2})-(\d{2})',
    ]
    for pattern in patterns:
        match = re.search(pattern, url, re.I)
        if match:
            try:
                year = int(match.group(1))
                month = int(match.group(2))
                day = 1 if len(match.groups()) < 3 else int(match.group(3))
                if 2000 <= year <= datetime.now().year + 1 and 1 <= month <= 12:
                    dt = datetime(year, month, day)
                    logger.debug("Extracted date from URL", date=dt, url=url)
                    return dt
            except ValueError:
                continue
    return None

def normalize_url(url: str, base_url: str) -> str:
    """Normalize a URL by resolving relative paths and removing fragments."""
    try:
        full_url = urljoin(base_url, url)
        parsed = urlparse(full_url)
        clean_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        if parsed.query:
            clean_url += f"?{parsed.query}"
        return clean_url.lower()
    except Exception as e:
        logger.error("Error normalizing URL", url=url, error=str(e))
        return url.lower()

def is_post_link(href: str, base_url: str) -> bool:
    """Check if a link is a relevant PDF link containing keywords."""
    if not href:
        return False
    full_url = normalize_url(href, base_url)
    if not full_url.lower().endswith('.pdf'):
        return False
    if any(keyword in full_url.lower() for keyword in KEYWORDS) or any(keyword in href.lower() for keyword in KEYWORDS):
        logger.debug("Relevant PDF link found", url=full_url)
        return True
    return False

async def parse_and_notify(url: str, sent_posts: Set[str], client: httpx.AsyncClient) -> List[Tuple[str, str]]:
    """Parse HTML content and notify about new PDF posts."""
    logger.info("Processing URL", url=url)
    html = await fetch(url, client)
    if not html:
        logger.error("No data fetched", url=url)
        return []

    soup = BeautifulSoup(html, 'html.parser', from_encoding="utf-8")
    new_posts = []
    notifications = []

    now = datetime.now()
    start_of_week = now - timedelta(days=now.weekday())
    end_of_week = start_of_week + timedelta(days=6)
    start_of_week = start_of_week.replace(hour=0, minute=0, second=0, microsecond=0)
    end_of_week = end_of_week.replace(hour=23, minute=59, second=59, microsecond=999999)

    logger.info("Filtering posts for week", start=start_of_week, end=end_of_week)

    for a in soup.find_all('a', href=True):
        text = a.get_text(strip=True)
        href = a['href']

        if not text or not href:
            logger.debug("Skipping empty link", href=href)
            continue

        if not is_post_link(href, url):
            logger.debug("Skipping non-relevant link", text=text, href=href)
            continue

        post_id = normalize_url(href, url)
        logger.debug("Processing post", href=href, post_id=post_id)

        if post_id in sent_posts:
            logger.info("Skipping already sent post", post_id=post_id)
            continue

        post_date = extract_date(a)
        is_pdf = post_id.lower().endswith('.pdf')
        if is_pdf and not post_date:
            post_date = await extract_date_from_pdf(post_id, client)
        if not post_date:
            post_date = extract_date_from_url(post_id)

        if not post_date:
            logger.info("No date found, skipping post", post_id=post_id)
            continue

        if post_date < start_of_week:
            logger.info("Stopping at old post", post_id=post_id, date=post_date)
            break

        if not (start_of_week <= post_date <= end_of_week):
            logger.info("Skipping post not in current week", post_id=post_id, date=post_date)
            continue

        msg = f"*New Notification*\n\n"
        msg += f"Website: {url}\n"
        msg += f"Title: {text or 'Untitled Notification'}\n"
        msg += f"URL: {post_id}\n"
        msg += f"Publish Date: {post_date.strftime('%Y-%m-%d')}\n"
        msg += f"PDF URL: {post_id}\n"

        new_posts.append((post_id, msg))
        notifications.append({"title": text, "url": post_id, "date": post_date})

    # Send summary notification for new posts
    if new_posts:
        unique_notifications = set(f"{n['title']}:{n['url']}" for n in notifications)
        if len(unique_notifications) == 1:
            n = notifications[0]
            await send_telegram(
                f"All notifications for {url} this week are identical:\n\nTitle: {n['title']}\nURL: {n['url']}\nDate: {n['date'].strftime('%Y-%m-%d')}\n",
                client
            )
        else:
            latest = max(notifications, key=lambda x: x['date'])
            await send_telegram(
                f"New notification found for {url}:\n\nTitle: {latest['title']}\nURL: {latest['url']}\nDate: {latest['date'].strftime('%Y-%m-%d')}\n",
                client
            )

    return new_posts

async def main(urls: List[str] = URLS):
    """Main function to monitor URLs and send notifications."""
    init_db()
    lock = FileLock(LOCK_FILE)
    try:
        with lock.acquire(timeout=1):
            logger.info("Monitoring started")
            sent_posts = load_sent_posts()
            async with httpx.AsyncClient(verify=False, timeout=TIMEOUT) as client:
                for url in urls:
                    try:
                        if not url:
                            logger.error("Invalid URL, skipping", url=url)
                            continue
                        new_posts = await parse_and_notify(url, sent_posts, client)
                        if not new_posts:
                            logger.info("No new relevant PDFs found", url=url)
                            continue
                        for post_id, message in new_posts:
                            if await send_telegram(message, client):
                                save_sent_post(post_id)
                                sent_posts.add(post_id)
                                logger.info("Sent and saved notification", post_id=post_id)
                            else:
                                logger.error("Failed to send notification", post_id=post_id)
                    except Exception as e:
                        logger.error("Error processing URL", url=url, error=str(e))
                        await send_telegram(f"Error processing {url}: {str(e)}", client)
                    await asyncio.sleep(2)
    except Exception as e:
        logger.error("Script failed", error=str(e))
        async with httpx.AsyncClient(verify=False, timeout=TIMEOUT) as client:
            await send_telegram(f"Script failed: {str(e)}", client)
        raise
    finally:
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)
            logger.info("Cleaned up lock file")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Website Monitor")
    parser.add_argument('--urls', nargs='+', default=URLS, help="URLs to monitor")
    parser.add_argument('--log-level', default='INFO', choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'], help="Logging level")
    args = parser.parse_args()

    logging.getLogger().setLevel(args.log_level)
    asyncio.run(main(args.urls))
