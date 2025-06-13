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

import requests
import httpx
from bs4 import BeautifulSoup
from charset_normalizer import detect
from dateutil.parser import parse as date_parse
from dotenv import load_dotenv
from filelock import FileLock
import pdfplumber
import PyPDF2
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
TELEGRAM_RATE_LIMIT = float(config.get('DEFAULT', 'telegram_rate_limit', fallback=0.1))

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
        "text": message[:4096],  # Telegram message length limit
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

async def fetch(url: str, client: httpx.AsyncClient, is_binary: bool = False) -> Optional[Union[str, bytes]]:
    """Fetch content asynchronously, returning text for HTML or bytes for binary (e.g., PDFs)."""
    try:
        resp = await client.get(url, headers=HEADERS, timeout=PDF_TIMEOUT if is_binary else TIMEOUT)
        resp.raise_for_status()
        if is_binary:
            logger.info("Fetched binary content", url=url, status=resp.status_code)
            return resp.content
        detected = detect(resp.content)
        encoding = detected['encoding'] or 'utf-8'
        text = resp.content.decode(encoding, errors='ignore')
        logger.info("Fetched text content", url=url, status=resp.status_code)
        await asyncio.sleep(2)
        return text
    except httpx.HTTPStatusError as e:
        logger.error("HTTP error fetching URL", url=url, status=e.response.status_code, error=str(e))
        return None
    except Exception as e:
        logger.error("Error fetching URL", url=url, error=str(e))
        return None

def fetch_sync(url: str) -> Optional[str]:
    """Fetch HTML content synchronously using requests."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        detected = detect(resp.content)
        encoding = detected['encoding'] or 'utf-8'
        text = resp.content.decode(encoding, errors='ignore')
        logger.info("Fetched URL synchronously", url=url, status=resp.status_code)
        time.sleep(2)
        return text
    except requests.RequestException as e:
        logger.error("Error fetching URL synchronously", url=url, error=str(e))
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
    """Extract a date from the first few pages of a PDF using pdfplumber and PyPDF2."""
    try:
        content = await fetch(url, client, is_binary=True)
        if not content:
            return None
        pdf_content = BytesIO(content)

        # Try pdfplumber first
        try:
            with pdfplumber.open(pdf_content) as pdf:
                for page in pdf.pages[:3]:
                    text = page.extract_text() or ""
                    dt = extract_date_from_text(text)
                    if dt:
                        logger.info("Extracted PDF date with pdfplumber", date=dt, url=url)
                        return dt
        except Exception as e:
            logger.warning("pdfplumber failed, trying PyPDF2", url=url, error=str(e))

        # Fallback to PyPDF2
        pdf_content.seek(0)
        pdf_reader = PyPDF2.PdfReader(pdf_content)
        for page_num in range(min(3, len(pdf_reader.pages))):
            text = pdf_reader.pages[page_num].extract_text() or ""
            dt = extract_date_from_text(text)
            if dt:
                logger.info("Extracted PDF date with PyPDF2", date=dt, url=url)
                return dt
        return None
    except Exception as e:
        logger.error("Error processing PDF", url=url, error=str(e))
        return None

async def extract_date(a_tag: BeautifulSoup, notification_url: str, client: httpx.AsyncClient) -> Optional[datetime]:
    """Extract a date from an <a> tag, surrounding text, or linked page."""
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

    # Fetch linked page for additional date context
    try:
        linked_html = await fetch(notification_url, client)
        if linked_html:
            linked_soup = BeautifulSoup(linked_html, 'html.parser')
            meta_date = linked_soup.find('meta', attrs={'name': re.compile('date|publish', re.I)})
            if meta_date and meta_date.get('content'):
                candidates.append(meta_date.get('content'))
            date_elements = linked_soup.find_all(['time', 'span', 'div'], class_=re.compile('date|publish', re.I))
            for elem in date_elements:
                candidates.append(elem.get_text(strip=True))
    except Exception as e:
        logger.warning("Failed to fetch linked page for date", url=notification_url, error=str(e))

    for text in candidates:
        dt = extract_date_from_text(text)
        if dt:
            logger.debug("Extracted date", date=dt, text=text[:50])
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

def is_notification_link(href: str, text: str) -> bool:
    """Check if a link or text contains notification keywords."""
    if not href and not text:
        return False
    text_lower = (text or "").lower()
    href_lower = (href or "").lower()
    return any(keyword in text_lower or keyword in href_lower for keyword in KEYWORDS)

async def find_pdf_in_notification_page(notification_url: str, client: httpx.AsyncClient) -> Optional[str]:
    """Check the notification page for any PDF links."""
    try:
        html = await fetch(notification_url, client)
        if not html:
            return None
        soup = BeautifulSoup(html, 'html.parser')
        for a in soup.find_all('a', href=True):
            href = a['href']
            if href.lower().endswith('.pdf'):
                pdf_url = normalize_url(href, notification_url)
                logger.info("Found PDF in notification page", pdf_url=pdf_url)
                return pdf_url
        return None
    except Exception as e:
        logger.error("Error checking notification page for PDFs", url=notification_url, error=str(e))
        return None

async def parse_and_notify(url: str, sent_posts: Set[str], client: httpx.AsyncClient) -> List[Tuple[str, str]]:
    """Parse HTML content and notify about new notifications."""
    logger.info("Processing URL", url=url)
    html = fetch_sync(url)
    if not html:
        logger.error("No data fetched", url=url)
        async with httpx.AsyncClient(verify=False, timeout=TIMEOUT) as temp_client:
            await send_telegram(f"Error: Failed to fetch {url}", temp_client)
        return []

    soup = BeautifulSoup(html, 'html.parser')
    new_posts = []
    notifications = []
    latest_no_date = None
    links_checked = 0

    now = datetime.now()
    start_of_week = now - timedelta(days=now.weekday())  # Monday, June 9, 2025
    end_of_week = start_of_week + timedelta(days=6)      # Sunday, June 15, 2025
    start_of_week = start_of_week.replace(hour=0, minute=0, second=0, microsecond=0)
    end_of_week = end_of_week.replace(hour=23, minute=59, second=59, microsecond=999999)

    logger.info("Filtering notifications for week", start=start_of_week, end=end_of_week)

    for a in soup.find_all('a', href=True):
        links_checked += 1
        text = a.get_text(strip=True)
        href = a['href']

        if not is_notification_link(href, text):
            logger.debug("Skipping non-notification link", text=text, href=href)
            continue

        post_id = normalize_url(href, url)
        logger.debug("Processing notification", href=href, post_id=post_id)

        if post_id in sent_posts:
            logger.info("Skipping already sent notification", post_id=post_id)
            continue

        # Extract full text
        full_text = text or "No title available"
        if a.parent:
            parent_text = a.parent.get_text(strip=True)
            full_text = parent_text if len(parent_text) > len(full_text) else full_text
        try:
            linked_html = await fetch(post_id, client)
            if linked_html:
                linked_soup = BeautifulSoup(linked_html, 'html.parser')
                main_content = linked_soup.find('div', class_=['content', 'main', 'article', 'post']) or linked_soup
                full_text = main_content.get_text(strip=True)[:2000] if main_content else full_text
        except Exception as e:
            logger.warning("Failed to fetch full text from notification page", post_id=post_id, error=str(e))

        # Extract date
        post_date = await extract_date(a, post_id, client)
        is_pdf = post_id.lower().endswith('.pdf')
        if is_pdf and not post_date:
            post_date = await extract_date_from_pdf(post_id, client)
        if not post_date:
            post_date = extract_date_from_url(post_id)

        # Check for PDF
        pdf_url = None
        if is_pdf:
            pdf_url = post_id
        else:
            pdf_url = await find_pdf_in_notification_page(post_id, client)

        if post_date:
            if post_date < start_of_week:
                logger.info("Stopping at old notification", post_id=post_id, date=post_date)
                break
            if not (start_of_week <= post_date <= end_of_week):
                logger.info("Skipping notification not in current week", post_id=post_id, date=post_date)
                continue

            msg = f"*New Notification*\n\n"
            msg += f"Website: {url}\n"
            msg += f"Title: {text or 'Untitled Notification'}\n"
            msg += f"URL: {post_id}\n"
            msg += f"Publish Date: {post_date.strftime('%Y-%m-%d')}\n"
            if pdf_url:
                msg += f"PDF URL: {pdf_url}\n"
            msg += f"\n*Full Text*:\n{full_text[:1500]}\n"

            notifications.append({"title": text, "url": post_id, "date": post_date, "pdf_url": pdf_url, "msg": msg})
        else:
            # Store first notification without date as potential latest
            if not latest_no_date:
                latest_no_date = {
                    "title": text,
                    "url": post_id,
                    "pdf_url": pdf_url,
                    "full_text": full_text
                }
            logger.info("No date found for notification", post_id=post_id)

    logger.info("Parsing complete", url=url, links_checked=links_checked, notifications_found=len(notifications))

    # Process notifications with dates
    for n in notifications:
        new_posts.append((n["url"], n["msg"]))

    # Handle case where no notifications have dates
    if not notifications and latest_no_date:
        post_id = latest_no_date["url"]
        if post_id not in sent_posts:
            msg = f"*New Notification (Latest, Date Not Found)*\n\n"
            msg += f"Website: {url}\n"
            msg += f"Title: {latest_no_date['title'] or 'Untitled Notification'}\n"
            msg += f"URL: {post_id}\n"
            msg += f"Publish Date: Not found, assumed to be the latest notification\n"
            if latest_no_date["pdf_url"]:
                msg += f"PDF URL: {latest_no_date['pdf_url']}\n"
            msg += f"\n*Full Text*:\n{latest_no_date['full_text'][:1500]}\n"
            new_posts.append((post_id, msg))
            notifications.append({
                "title": latest_no_date["title"],
                "url": post_id,
                "date": datetime.now(),
                "pdf_url": latest_no_date["pdf_url"],
                "msg": msg
            })

    if not notifications:
        logger.warning("No notifications found", url=url)
        async with httpx.AsyncClient(verify=False, timeout=TIMEOUT) as temp_client:
            await send_telegram(f"Warning: No notifications found on {url} for the current week", temp_client)

    # Send summary notification
    if notifications:
        unique_notifications = set(f"{n['title']}:{n['url']}" for n in notifications)
        if len(unique_notifications) == 1:
            n = notifications[0]
            await send_telegram(
                f"All notifications for {url} this week are identical:\n\nTitle: {n['title']}\nURL: {n['url']}\nDate: {'Not found, latest' if 'Date Not Found' in n['msg'] else n['date'].strftime('%Y-%m-%d')}\n{'PDF URL: ' + n['pdf_url'] if n['pdf_url'] else ''}",
                client
            )
        else:
            latest = max(notifications, key=lambda x: x['date'])
            await send_telegram(
                f"New notification found for {url}:\n\nTitle: {latest['title']}\nURL: {latest['url']}\nDate: {'Not found, latest' if 'Date Not Found' in latest['msg'] else latest['date'].strftime('%Y-%m-%d')}\n{'PDF URL: ' + latest['pdf_url'] if latest['pdf_url'] else ''}",
                client
            )

    return new_posts

async def main(urls: List[str] = URLS):
    """Main function to monitor URLs and send notifications."""
    init_db()
    lock = FileLock(LOCK_FILE)
    any_notifications_found = False
    try:
        with lock.acquire(timeout=1):
            logger.info("Monitoring started")
            sent_posts = load_sent_posts()
            async with httpx.AsyncClient(verify=False, timeout=TIMEOUT) as client:
                for url in urls:
                    if not url:
                        logger.error("Invalid URL, skipping", url=url)
                        continue
                    try:
                        new_posts = await parse_and_notify(url, sent_posts, client)
                        if new_posts:
                            any_notifications_found = True
                        for post_id, message in new_posts:
                            if await send_telegram(message, client):
                                save_sent_post(post_id)
                                sent_posts.add(post_id)
                                logger.info("Sent and saved notification", post_id=post_id)
                            else:
                                logger.error("Failed to send notification", post_id=post_id)
                    except Exception as e:
                        logger.error("Error processing URL", url=url, error=str(e))
                        async with httpx.AsyncClient(verify=False, timeout=TIMEOUT) as temp_client:
                            await send_telegram(f"Error processing {url}: {str(e)}", temp_client)
                    await asyncio.sleep(2)
                if not any_notifications_found:
                    async with httpx.AsyncClient(verify=False, timeout=TIMEOUT) as temp_client:
                        await send_telegram("No notifications found across all URLs for the current week", temp_client)
                    logger.info("No notifications found for any URL")
    except Exception as e:
        logger.error("Script failed", error=str(e))
        async with httpx.AsyncClient(verify=False, timeout=TIMEOUT) as temp_client:
            await send_telegram(f"Script failed: {str(e)}", temp_client)
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
