import configparser
import logging
import sqlite3
import os
import re
import time
import random
import asyncio
import aiohttp
from datetime import datetime
from typing import Optional, List, Tuple
from bs4 import BeautifulSoup
from charset_normalizer import detect
from dateutil.parser import parse as date_parse
from dotenv import load_dotenv
import structlog
import scrapy
from scrapy.crawler import CrawlerProcess
from scrapy.http import HtmlResponse
from transformers import pipeline
from sentence_transformers import SentenceTransformer, util
from sklearn.ensemble import RandomForestClassifier
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
TIMEOUT = int(config.get('DEFAULT', 'timeout', fallback=120))  # 120s
RETRIES = int(config.get('DEFAULT', 'retries', fallback=3))
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.114 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.114 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:89.0) Gecko/20100101 Firefox/89.0",
]
KEYWORDS = config.get('DEFAULT', 'keywords', fallback="recruitment,vacancy,exam,admit card,notification,interview,application,results,answer key").split(',')
EXCLUDE_WORDS = ["home", "about us", "contact", "main navigation", "menu"]
SENT_POSTS_DB = config.get('DEFAULT', 'sent_posts_db', fallback="sent_messages.db")
MAX_NOTIFICATIONS_PER_URL = int(config.get('DEFAULT', 'max_notifications_per_url', fallback=10))

# AI model initialization
classifier = pipeline("text-classification", model="distilbert-base-uncased")  # NLP for notification filtering
sentence_model = SentenceTransformer('all-MiniLM-L6-v2')  # Semantic duplicate detection
failure_model = RandomForestClassifier()  # Placeholder: train on failure data
sent_embeddings = {}  # Cache for duplicate detection

# Validate environment variables
if not TOKEN or not CHAT_ID:
    logger.error("Missing Telegram TOKEN or CHAT_ID", token_set=bool(TOKEN), chat_id_set=bool(CHAT_ID))
    exit(1)
else:
    logger.info("Telegram configured", chat_id=CHAT_ID, token_prefix=TOKEN[:5] + "***")

def init_db():
    """Initialize SQLite database for sent notifications."""
    try:
        with sqlite3.connect(SENT_POSTS_DB) as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS notifications (id TEXT PRIMARY KEY, sent_at TIMESTAMP)')
            conn.commit()
        logger.info("Initialized SQLite DB", db_file=SENT_POSTS_DB)
    except sqlite3.Error as e:
        logger.error("Failed to initialize SQLite DB", error=str(e))
        exit(1)

def check_db_integrity():
    """Check and repair SQLite database integrity."""
    try:
        with sqlite3.connect(SENT_POSTS_DB) as conn:
            conn.execute("PRAGMA integrity_check")
            conn.execute("DELETE FROM notifications WHERE id IN (SELECT id FROM notifications GROUP BY id HAVING COUNT(*) > 1)")
            conn.commit()
        logger.info("Checked SQLite DB integrity", db_file=SENT_POSTS_DB)
    except sqlite3.Error as e:
        logger.error("SQLite DB integrity check failed", error=str(e))

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
            conn.execute('INSERT OR IGNORE INTO notifications (id, sent_at) VALUES (?, ?)',
                         (notification_id, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
            conn.commit()
        logger.info("Saved notification ID", id=notification_id)
    except sqlite3.Error as e:
        logger.error("Failed to save notification ID", id=notification_id, error=str(e))

async def send_telegram(message: str, url: str) -> bool:
    """Send a message to Telegram without escaping text or date."""
    telegram_url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    lines = message.split('\n')
    formatted_lines = [lines[0]]  # *New Notification*
    formatted_lines.append("")  # Blank line
    for line in lines[2:]:
        if line.startswith("Website:"):
            formatted_lines.append(f"Website: {url}")
        else:
            formatted_lines.append(line)
    formatted_message = "\n".join(formatted_lines)[:4096]
    data = {
        "chat_id": CHAT_ID,
        "text": formatted_message,
        "parse_mode": "Markdown"
    }
    async with aiohttp.ClientSession() as session:
        for attempt in range(1, 4):
            try:
                async with session.post(telegram_url, json=data, timeout=30) as resp:
                    resp.raise_for_status()
                    logger.info("Telegram message sent", message=formatted_message[:50], url=url)
                    await asyncio.sleep(1.0)
                    return True
            except aiohttp.ClientError as e:
                logger.warning("Telegram send failed", attempt=attempt, error=str(e))
                await asyncio.sleep(2 ** attempt)
    logger.error("Failed to send Telegram message after retries", message=formatted_message[:50], url=url)
    return False

async def send_failure_alert(failed_urls: List[str]):
    """Send a Telegram alert for failed URLs if >50% fail."""
    if not failed_urls or len(failed_urls) < len(URLS) * 0.5:
        return
    message = f"*Critical Alert*\n\nMore than 50% of URLs failed to fetch:\n" + "\n".join(failed_urls[:10]) + \
              f"\n\nTotal failed: {len(failed_urls)}/{len(URLS)}. Check logs for details."
    await send_telegram(message, "N/A")

async def fetch_page_async(url: str, session: aiohttp.ClientSession) -> Optional[str]:
    """Fetch HTML content asynchronously with retries and User-Agent rotation."""
    for attempt in range(1, RETRIES + 1):
        headers = {"User-Agent": random.choice(USER_AGENTS)}
        try:
            async with session.get(url, headers=headers, timeout=TIMEOUT, ssl=False) as resp:
                resp.raise_for_status()
                text = await resp.text(encoding='utf-8', errors='ignore')
                logger.info("Fetched page", url=url, attempt=attempt)
                await asyncio.sleep(2)
                return text
        except aiohttp.ClientError as e:
            logger.warning("Failed to fetch URL", url=url, attempt=attempt, error=str(e))
            # Placeholder: Predict failure type
            failure_type = 'retry'  # Train failure_model on real data
            if failure_type == 'unrecoverable' or attempt == RETRIES:
                logger.error("Exhausted retries for URL", url=url, error=str(e))
                return None
            await asyncio.sleep(2 ** attempt)
    return None

def fetch_page_dynamic(url: str) -> Optional[str]:
    """Fetch HTML content for dynamic pages using Scrapy."""
    class NotificationSpider(scrapy.Spider):
        name = 'notification'
        start_urls = [url]
        def parse(self, response):
            return {'html': response.text}
    process = CrawlerProcess(settings={
        'USER_AGENT': random.choice(USER_AGENTS),
        'DOWNLOAD_TIMEOUT': TIMEOUT,
        'PLAYWRIGHT_ENABLED': True,
    })
    result = []
    def collect_result(spider, item):
        result.append(item)
    process.crawl(NotificationSpider, callback=collect_result)
    process.start()
    return result[0]['html'] if result else None

def extract_date_from_text(text: str) -> Optional[datetime]:
    """Extract a date from text with relaxed patterns."""
    if not text:
        return None
    month_names = r'(?:January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)'
    date_patterns = [
        r'\b(\d{1,2}[-/\s]\d{1,2}[-/\s]\d{4})\b',
        r'\b(\d{4}[-/\s]\d{1,2}[-/\s]\d{1,2})\b',
        rf'\b(\d{{1,2}}\s+{month_names}\s+\d{{4}})\b',
        rf'\b(\d{{1,2}}(?:st|nd|rd|th)?\s+{month_names}\s+\d{{4}})\b',
        rf'\b(\d{{1,2}}-{month_names}-\d{{4}})\b',
        rf'\b({month_names}\s+\d{{1,2}},?\s+\d{{4}})\b',
        r'\b(\d{4}/\d{2}/\d{2})\b',
        r'\b(\d{2}/\d{2}/\d{4})\b',
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
    text_lower = text.lower()
    now = datetime.now()
    if f"{now.strftime('%B').lower()} {now.year}" in text_lower or f"{now.strftime('%m')}/{now.year}" in text_lower:
        dt = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        logger.debug("Assigned fallback date", date=dt, text=text[:50])
        return dt
    logger.debug("No valid date found", text=text[:50])
    return None

def is_notification(text: str) -> bool:
    """Check if text is a valid notification using NLP."""
    if not text or len(text) < 20 or len(text) > 500:
        return False
    text_lower = text.lower()
    if any(word in text_lower for word in EXCLUDE_WORDS):
        return False
    result = classifier(text[:512])[0]
    logger.debug("NLP classification", text=text[:50], label=result['label'], score=result['score'])
    return result['label'] == 'POSITIVE' and result['score'] > 0.7

def normalize_text(text: str) -> str:
    """Normalize text for duplicate checking using semantic similarity."""
    text = ''.join(c for c in text if c.isprintable())
    text = re.sub(r'\s+', ' ', text.strip()).lower()
    embedding = sentence_model.encode(text)
    for cached_id, cached_emb in sent_embeddings.items():
        if util.cos_sim(embedding, cached_emb) > 0.95:
            logger.debug("Found duplicate", text=text[:50], cached_id=cached_id)
            return cached_id
    sent_embeddings[text] = embedding
    return text

def parse_notifications(url: str, html: str, sent_posts: set) -> List[Tuple[str, str]]:
    """Parse HTML for new notifications in the current month."""
    logger.info("Processing URL", url=url)
    if not html:
        logger.error("No HTML content fetched, skipping notifications", url=url)
        return []

    soup = BeautifulSoup(html, 'html.parser')
    notifications = []
    now = datetime.now()
    current_year, current_month = now.year, now.month
    logger.info("Filtering notifications for current month", year=current_year, month=current_month)

    notification_count = 0
    for element in soup.find_all(['li', 'p', 'a']):
        text = element.get_text(strip=True)
        if not is_notification(text):
            logger.debug("Skipping non-notification text", text=text[:50])
            continue

        normalized_text = normalize_text(text)
        if not normalized_text:
            continue

        notification_id = f"{url}:{hash(normalized_text)}".lower()
        if notification_id in sent_posts:
            logger.info("Skipping duplicate notification", id=notification_id, text=text[:50])
            continue

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

        if date and date.year == current_year and date.month == current_month:
            text_lower = text.lower()
            month_str = date.strftime('%B').lower()[:3]
            if month_str in text_lower or date.strftime('%m') in text_lower:
                msg = f"*New Notification*\n\n"
                msg += f"Website: {url}\n"
                msg += f"Text: {text}\n"
                msg += f"Date: {date.strftime('%Y-%m-%d')}\n"
                notifications.append((notification_id, msg))
                notification_count += 1
                if notification_count >= MAX_NOTIFICATIONS_PER_URL:
                    logger.info("Reached max notifications for URL", url=url, max=MAX_NOTIFICATIONS_PER_URL)
                    break
            else:
                logger.debug("Skipping notification with mismatched month in text", text=text[:50], date=date)
        else:
            logger.debug("Skipping notification not in current month", text=text[:50], date=date)

    logger.info("Found notifications", url=url, count=len(notifications))
    return notifications

async def main(urls: List[str] = URLS):
    """Monitor URLs for new notifications and send to Telegram."""
    init_db()
    check_db_integrity()
    logger.info("Monitoring started", total_urls=len(urls))
    sent_posts = load_sent_posts()
    failed_urls = []

    async with aiohttp.ClientSession() as session:
        tasks = []
        for url in urls:
            if not url:
                logger.error("Empty URL, skipping")
                continue
            if not url.startswith(('http://', 'https://')):
                url = f"https://{url}"
                logger.info("Prepended https:// to URL", url=url)
            tasks.append(fetch_page_async(url, session))
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for url, html in zip(urls, results):
            if html is None:
                # Try dynamic fetch for failed URLs
                logger.info("Attempting dynamic fetch", url=url)
                html = fetch_page_dynamic(url)
                if html is None:
                    failed_urls.append(url)
                    continue
            try:
                notifications = parse_notifications(url, html, sent_posts)
                for notification_id, message in notifications:
                    sent_posts.add(notification_id)
                    if await send_telegram(message, url):
                        save_sent_post(notification_id)
                        logger.info("Sent notification", id=notification_id, message=message[:50], url=url)
                    else:
                        logger.error("Failed to send notification", id=notification_id, url=url)
                        sent_posts.remove(notification_id)
            except Exception as e:
                logger.error("Error processing URL", url=url, error=str(e))
                failed_urls.append(url)
            await asyncio.sleep(2)

    if failed_urls:
        logger.warning("Summary of failed URLs", failed_count=len(failed_urls), total_urls=len(urls), failed_urls=failed_urls)
        await send_failure_alert(failed_urls)
    else:
        logger.info("All URLs processed successfully", total_urls=len(urls))

if __name__ == "__module__":
    import argparse
    parser = argparse.ArgumentParser(description="Website Notification Monitor")
    parser.add_argument('--urls', nargs='+', default=URLS, help="URLs to monitor")
    parser.add_argument('--log-level', default='INFO', choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'], help="Logging level")
    args = parser.parse_args()

    logging.getLogger().setLevel(args.log_level)
    asyncio.run(main(args.urls))
