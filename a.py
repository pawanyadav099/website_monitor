import configparser
import logging
import sqlite3
import os
import re
import random
import asyncio
import aiohttp
from datetime import datetime, timedelta
from typing import Optional, List, Dict
from bs4 import BeautifulSoup
from dateutil.parser import parse as date_parse
from dotenv import load_dotenv
import structlog
from playwright.async_api import async_playwright
from url import URLS  # Import URLs from url.py file

# Configure structured logging without emojis
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

# Load environment variables (works with GitHub secrets)
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TOKEN")
TELEGRAM_CHAT_ID = os.getenv("CHAT_ID")

# Configuration defaults
DEFAULT_CONFIG = {
    'timeout': 60,
    'retries': 3,
    'keywords': "recruitment,vacancy,exam,admit card,notification,interview,application,results,answer key",
    'exclude_words': "home,about us,contact,main navigation,menu,privacy policy,disclaimer",
    'db_file': "sent_notifications.db",
    'max_notifications': 5,
    'min_notification_length': 20,
    'max_notification_length': 1000,
    'similarity_threshold': 0.9,
    'notification_age_days': 30
}

# Initialize configuration
TIMEOUT = int(config.get('DEFAULT', 'timeout', fallback=DEFAULT_CONFIG['timeout']))
RETRIES = int(config.get('DEFAULT', 'retries', fallback=DEFAULT_CONFIG['retries']))
KEYWORDS = [k.strip() for k in config.get('DEFAULT', 'keywords', fallback=DEFAULT_CONFIG['keywords']).split(',')]
EXCLUDE_WORDS = [w.strip() for w in config.get('DEFAULT', 'exclude_words', fallback=DEFAULT_CONFIG['exclude_words']).split(',')]
SENT_POSTS_DB = config.get('DEFAULT', 'db_file', fallback=DEFAULT_CONFIG['db_file'])
MAX_NOTIFICATIONS_PER_URL = int(config.get('DEFAULT', 'max_notifications', fallback=DEFAULT_CONFIG['max_notifications']))
MIN_NOTIFICATION_LENGTH = int(config.get('DEFAULT', 'min_notification_length', fallback=DEFAULT_CONFIG['min_notification_length']))
MAX_NOTIFICATION_LENGTH = int(config.get('DEFAULT', 'max_notification_length', fallback=DEFAULT_CONFIG['max_notification_length']))
SIMILARITY_THRESHOLD = float(config.get('DEFAULT', 'similarity_threshold', fallback=DEFAULT_CONFIG['similarity_threshold']))
NOTIFICATION_AGE_DAYS = int(config.get('DEFAULT', 'notification_age_days', fallback=DEFAULT_CONFIG['notification_age_days']))

# User Agents for request rotation
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.114 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.114 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:89.0) Gecko/20100101 Firefox/89.0",
]

def init_db():
    """Initialize SQLite database for tracking sent notifications."""
    try:
        with sqlite3.connect(SENT_POSTS_DB) as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS notifications (
                    id TEXT PRIMARY KEY,
                    url TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    notified BOOLEAN DEFAULT TRUE
                )
            ''')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_content_hash ON notifications(content_hash)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_sent_at ON notifications(sent_at)')
            conn.commit()
        logger.info("Database initialized successfully")
    except sqlite3.Error as e:
        logger.error("Database initialization failed", error=str(e))
        raise

def cleanup_old_entries():
    """Remove old entries from the database."""
    try:
        with sqlite3.connect(SENT_POSTS_DB) as conn:
            cutoff_date = (datetime.now() - timedelta(days=NOTIFICATION_AGE_DAYS)).strftime('%Y-%m-%d')
            conn.execute("DELETE FROM notifications WHERE sent_at < ?", (cutoff_date,))
            conn.commit()
        logger.info("Old database entries cleaned up")
    except sqlite3.Error as e:
        logger.error("Database cleanup failed", error=str(e))

def is_notification_sent(content_hash: str) -> bool:
    """Check if a notification with this content hash has already been sent."""
    try:
        with sqlite3.connect(SENT_POSTS_DB) as conn:
            cursor = conn.execute("SELECT 1 FROM notifications WHERE content_hash = ? LIMIT 1", (content_hash,))
            return cursor.fetchone() is not None
    except sqlite3.Error as e:
        logger.error("Failed to check notification status", error=str(e))
        return False

def mark_notification_sent(url: str, notification_id: str, content_hash: str):
    """Mark a notification as sent in the database."""
    try:
        with sqlite3.connect(SENT_POSTS_DB) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO notifications (id, url, content_hash) VALUES (?, ?, ?)",
                (notification_id, url, content_hash)
            )
            conn.commit()
        logger.debug("Notification marked as sent in database")
    except sqlite3.Error as e:
        logger.error("Failed to mark notification as sent", error=str(e))

async def fetch_with_playwright(url: str) -> Optional[str]:
    """
    Fetch page content using Playwright for JavaScript-heavy sites.
    This helps bypass 403 Forbidden errors by simulating real browser.
    """
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            context = await browser.new_context(
                user_agent=random.choice(USER_AGENTS),
                viewport={'width': 1920, 'height': 1080}
            )
            page = await context.new_page()
            
            try:
                # Prepend https:// if missing
                if not url.startswith(('http://', 'https://')):
                    url = f'https://{url}'
                
                await page.goto(url, timeout=TIMEOUT*1000, wait_until="networkidle")
                content = await page.content()
                await browser.close()
                return content
            except Exception as e:
                logger.warning("Playwright navigation failed", url=url, error=str(e))
                await browser.close()
                return None
    except Exception as e:
        logger.error("Playwright initialization failed", error=str(e))
        return None

async def fetch_page(url: str, session: Optional[aiohttp.ClientSession] = None) -> Optional[str]:
    """
    Fetch page content with retries and fallback methods.
    Handles both direct URL access and https:// prefixed URLs.
    """
    # Prepare proper URL format
    if not url.startswith(('http://', 'https://')):
        url = f'https://{url}'
    
    # Enhanced headers to prevent 403 Forbidden
    headers = {
        'User-Agent': random.choice(USER_AGENTS),
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
        'Accept-Encoding': 'gzip, deflate, br',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
        'Sec-Fetch-Dest': 'document',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'none',
        'Sec-Fetch-User': '?1',
        'Cache-Control': 'max-age=0'
    }
    
    for attempt in range(1, RETRIES + 1):
        try:
            # First try with aiohttp
            if session:
                async with session.get(url, headers=headers, timeout=TIMEOUT) as response:
                    if response.status == 403:
                        # If forbidden, try with Playwright immediately
                        raise Exception("403 Forbidden - Switching to Playwright")
                    response.raise_for_status()
                    content = await response.text(encoding='utf-8', errors='ignore')
                    if len(content) > 1000:
                        return content
            
            # Fallback to Playwright
            playwright_content = await fetch_with_playwright(url)
            if playwright_content:
                return playwright_content
            
            await asyncio.sleep(2 ** attempt)
            
        except Exception as e:
            logger.warning(f"Attempt {attempt} failed for URL: {url}", error=str(e))
            await asyncio.sleep(2 ** attempt)
    
    logger.error(f"All attempts failed for URL: {url}")
    return None

def extract_dates(text: str) -> List[datetime]:
    """Extract all possible dates from notification text."""
    if not text:
        return []
    
    month_names = r'(?:January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)'
    patterns = [
        rf'\b(\d{{1,2}}[-/]\d{{1,2}}[-/]\d{{2,4}})\b',
        rf'\b(\d{{1,2}}\s+{month_names}\s+\d{{4}})\b',
        rf'\b({month_names}\s+\d{{1,2}}(?:st|nd|rd|th)?\s+\d{{4}})\b',
        rf'\b({month_names}\s+\d{{4}})\b',
        rf'\b(\d{{4}}[-/]\d{{1,2}}[-/]\d{{1,2}})\b',
        rf'\b(\d{{1,2}}\s+{month_names}\s+\d{{4}})\b',
    ]
    
    dates = []
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            try:
                dt = date_parse(match.group(0), dayfirst=True, yearfirst=True)
                if 2000 <= dt.year <= datetime.now().year + 1:
                    dates.append(dt)
            except ValueError:
                continue
    
    return dates

def is_relevant_notification(text: str) -> bool:
    """Check if text contains relevant notification keywords."""
    if not text or len(text) < MIN_NOTIFICATION_LENGTH or len(text) > MAX_NOTIFICATION_LENGTH:
        return False
    
    text_lower = text.lower()
    
    # Check for excluded words
    if any(exclude_word.lower() in text_lower for exclude_word in EXCLUDE_WORDS):
        return False
    
    # Check for keywords
    if any(keyword.lower() in text_lower for keyword in KEYWORDS):
        return True
    
    # Check for common notification patterns
    notification_phrases = [
        'last date',
        'apply online',
        'registration',
        'download',
        'published',
        'advertisement',
        'announcement',
        'circular',
        'notice'
    ]
    return any(phrase in text_lower for phrase in notification_phrases)

def generate_content_hash(text: str) -> str:
    """Generate a consistent hash for notification content."""
    normalized = re.sub(r'\s+', ' ', text.strip().lower())
    return str(hash(normalized))

def extract_notifications(url: str, html: str) -> List[Dict[str, str]]:
    """Extract potential notifications from HTML content."""
    if not html:
        return []
    
    soup = BeautifulSoup(html, 'html.parser')
    notifications = []
    
    # Look for common notification containers
    selectors = [
        'li', 'p', 'div.notification', 'div.news-item', 
        'div.announcement', 'a[href*="notification"]',
        'a[href*="circular"]', 'a[href*="advertisement"]'
    ]
    
    for element in soup.select(', '.join(selectors)):
        text = element.get_text(' ', strip=True)
        if not is_relevant_notification(text):
            continue
        
        # Extract dates
        dates = extract_dates(text)
        recent_dates = [dt for dt in dates if dt >= datetime.now() - timedelta(days=NOTIFICATION_AGE_DAYS)]
        
        if not recent_dates and not dates:
            continue  # No dates or only old dates
            
        most_recent_date = max(recent_dates) if recent_dates else max(dates) if dates else None
        
        # Generate unique ID and content hash
        content_hash = generate_content_hash(text)
        notification_id = f"{url}:{content_hash}"
        
        notifications.append({
            'id': notification_id,
            'url': url,
            'text': text,
            'date': most_recent_date.strftime('%Y-%m-%d') if most_recent_date else 'No date',
            'content_hash': content_hash
        })
        
        if len(notifications) >= MAX_NOTIFICATIONS_PER_URL:
            break
    
    return notifications

async def send_telegram_notification(notification: Dict[str, str], url: str) -> bool:
    """
    Send notification to Telegram.
    Works with GitHub secrets when properly configured.
    """
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("Telegram credentials not configured")
        return False
    
    telegram_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    
    # Format message without emojis
    formatted_msg = (
        f"New Notification\n\n"
        f"Source: {url}\n"
        f"Date: {notification['date']}\n\n"
        f"Content:\n{notification['text']}\n\n"
        f"View Original: {url}"
    )
    
    payload = {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': formatted_msg,
        'parse_mode': 'Markdown',
        'disable_web_page_preview': True
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(telegram_url, json=payload, timeout=30) as response:
                if response.status == 200:
                    return True
                logger.error(f"Telegram API error: {await response.text()}")
                return False
    except Exception as e:
        logger.error(f"Failed to send Telegram message: {str(e)}")
        return False

async def monitor_urls(urls: List[str]):
    """Monitor a list of URLs for new notifications."""
    init_db()
    cleanup_old_entries()
    
    async with aiohttp.ClientSession() as session:
        tasks = [fetch_page(url, session) for url in urls]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        for url, html in zip(urls, results):
            if isinstance(html, Exception):
                logger.error(f"Failed to fetch URL: {url}", error=str(html))
                continue
            
            try:
                notifications = extract_notifications(url, html)
                for notification in notifications:
                    if is_notification_sent(notification['content_hash']):
                        continue
                    
                    if await send_telegram_notification(notification, url):
                        mark_notification_sent(url, notification['id'], notification['content_hash'])
                        logger.info(f"Sent notification: {notification['id']}")
                    else:
                        logger.error(f"Failed to send notification: {notification['id']}")
                        
                    await asyncio.sleep(1)  # Rate limiting
                    
            except Exception as e:
                logger.error(f"Error processing URL: {url}", error=str(e))

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Website Notification Monitor")
    parser.add_argument('--urls', nargs='+', help="Optional: Override URLs from url.py")
    parser.add_argument('--config', default='config.ini', help="Configuration file path")
    parser.add_argument('--log-level', default='INFO', choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'], help="Logging level")
    
    args = parser.parse_args()
    
    # Set logging level
    logging.basicConfig(level=args.log_level)
    
    # Verify Telegram credentials
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("Telegram credentials not found. Please set TELEGRAM_TOKEN and TELEGRAM_CHAT_ID environment variables.")
        exit(1)
    
    # Use URLs from command line if provided, otherwise from url.py
    urls_to_monitor = args.urls if args.urls else URLS
    
    if not urls_to_monitor:
        logger.error("No URLs provided to monitor")
        exit(1)
    
    logger.info(f"Starting monitoring for {len(urls_to_monitor)} URLs")
    asyncio.run(monitor_urls(urls_to_monitor))
