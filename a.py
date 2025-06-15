import configparser
import logging
import sqlite3
import os
import re
import random
import asyncio
import aiohttp
import requests
import json
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Tuple
from bs4 import BeautifulSoup
from dateutil.parser import parse as date_parse
from dotenv import load_dotenv
import structlog
from playwright.async_api import async_playwright
from playwright_stealth import stealth_async
from sentence_transformers import SentenceTransformer, util
from urllib.parse import urljoin, urlparse
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter
from url import URLS  # Import URLs from url.py file

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
TELEGRAM_TOKEN = os.getenv("TOKEN")
TELEGRAM_CHAT_ID = os.getenv("CHAT_ID")
# Proxy pool (use residential proxies)
PROXY_POOL = [
    None,  # No proxy
    # Example: "http://user:pass@residential_proxy1:port",
]

# Cookie file
COOKIE_FILE = "cookies.json"

# Configuration defaults
DEFAULT_CONFIG = {
    'timeout': 30,  # Reduced for faster requests
    'retries': 1,  # Minimal retries to avoid flagging
    'keywords': "recruitment,vacancy,exam,admit card,notification,interview,application,results,answer key,notice,announcement,call letter,merit list",
    'exclude_words': "home,about us,contact,main navigation,menu,privacy policy,disclaimer",
    'db_file': "sent_notifications.db",
    'max_notifications': 5,
    'min_notification_length': 20,
    'max_notification_length': 1000,
    'similarity_threshold': 0.85,
    'notification_age_days': 30,
    'rss_timeout': 30,
    'sitemap_timeout': 30,
    'max_concurrent_requests': 1,  # Single request
    'delay_between_requests': 600,  # 10 minutes base delay
    'respect_robots': True,
    'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'
}

# Initialize configuration
TIMEOUT = int(config.get('DEFAULT', 'timeout', fallback=DEFAULT_CONFIG['timeout']))
RETRIES = int(config.get('DEFAULT', 'retries', fallback=DEFAULT_CONFIG['retries']))
KEYWORDS = [k.strip() for k in config.get('DEFAULT', 'keywords', fallback=DEFAULT_CONFIG['keywords']).split(',')]
EXCLUDE_WORDS = [w.strip() for k in config.get('DEFAULT', 'exclude_words', fallback=DEFAULT_CONFIG['exclude_words']).split(',')]
SENT_POSTS_DB = config.get('DEFAULT', 'db_file', fallback=DEFAULT_CONFIG['db_file'])
MAX_NOTIFICATIONS_PER_URL = int(config.get('DEFAULT', 'max_notifications', fallback=DEFAULT_CONFIG['max_notifications']))
MIN_NOTIFICATION_LENGTH = int(config.get('DEFAULT', 'min_notification_length', fallback=DEFAULT_CONFIG['min_notification_length']))
MAX_NOTIFICATION_LENGTH = int(config.get('DEFAULT', 'max_notification_length', fallback=DEFAULT_CONFIG['max_notification_length']))
SIMILARITY_THRESHOLD = float(config.get('DEFAULT', 'similarity_threshold', fallback=DEFAULT_CONFIG['similarity_threshold']))
NOTIFICATION_AGE_DAYS = int(config.get('DEFAULT', 'notification_age_days', fallback=DEFAULT_CONFIG['notification_age_days']))
RSS_TIMEOUT = int(config.get('DEFAULT', 'rss_timeout', fallback=DEFAULT_CONFIG['rss_timeout']))
SITEMAP_TIMEOUT = int(config.get('DEFAULT', 'sitemap_timeout', fallback=DEFAULT_CONFIG['sitemap_timeout']))
MAX_CONCURRENT_REQUESTS = int(config.get('DEFAULT', 'max_concurrent_requests', fallback=DEFAULT_CONFIG['max_concurrent_requests']))
DELAY_BETWEEN_REQUESTS = int(config.get('DEFAULT', 'delay_between_requests', fallback=DEFAULT_CONFIG['delay_between_requests']))
RESPECT_ROBOTS = config.getboolean('DEFAULT', 'respect_robots', fallback=DEFAULT_CONFIG['respect_robots'])
USER_AGENT = config.get('DEFAULT', 'user_agent', fallback=DEFAULT_CONFIG['user_agent'])

# Load sentence transformer model
model = SentenceTransformer('paraphrase-MiniLM-L6-v2')

# User Agents for rotation
USER_AGENTS = [
    USER_AGENT,
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
]

# Track consecutive aiohttp failures per URL
AIOHTTP_FAILURES = {url: 0 for url in URLS}
AIOHTTP_FAILURE_THRESHOLD = 3  # Switch to requests after 3 failures

def save_cookies(cookies: Dict):
    """Save cookies to file."""
    try:
        with open(COOKIE_FILE, 'w') as f:
            json.dump(cookies, f)
        logger.debug("Cookies saved")
    except Exception as e:
        logger.error(f"Failed to save cookies: {str(e)}")

def load_cookies() -> Dict:
    """Load cookies from file."""
    try:
        if os.path.exists(COOKIE_FILE):
            with open(COOKIE_FILE, 'r') as f:
                cookies = json.load(f)
            logger.debug("Cookies loaded")
            return cookies
        return {}
    except Exception as e:
        logger.error(f"Failed to load cookies: {str(e)}")
        return {}

def init_db():
    try:
        with sqlite3.connect(SENT_POSTS_DB) as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS notifications (
                    id TEXT PRIMARY KEY,
                    url TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    semantic_hash TEXT NOT NULL,
                    notification_text TEXT NOT NULL,
                    extracted_date TEXT,
                    sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    notified BOOLEAN DEFAULT TRUE
                )
            ''')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_content_hash ON notifications(content_hash)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_semantic_hash ON notifications(semantic_hash)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_sent_at ON notifications(sent_at)')
            conn.commit()
        logger.info("Database initialized successfully")
    except sqlite3.Error as e:
        logger.error("Database initialization failed", error=str(e))
        raise

def cleanup_old_entries():
    try:
        with sqlite3.connect(SENT_POSTS_DB) as conn:
            cutoff_date = (datetime.now() - timedelta(days=NOTIFICATION_AGE_DAYS)).strftime('%Y-%m-%d')
            conn.execute("DELETE FROM notifications WHERE sent_at < ?", (cutoff_date,))
            conn.commit()
        logger.info("Old database entries cleaned up")
    except sqlite3.Error as e:
        logger.error("Database cleanup failed", error=str(e))

def is_notification_sent(content_hash: str, semantic_hash: str) -> bool:
    try:
        with sqlite3.connect(SENT_POSTS_DB) as conn:
            cursor = conn.execute(
                "SELECT 1 FROM notifications WHERE content_hash = ? LIMIT 1", 
                (content_hash,)
            )
            if cursor.fetchone() is not None:
                return True
            cursor = conn.execute(
                "SELECT notification_text FROM notifications WHERE semantic_hash = ? LIMIT 5",
                (semantic_hash,)
            )
            existing_notifications = cursor.fetchall()
            if existing_notifications:
                for (existing_text,) in existing_notifications:
                    if is_similar_notification(existing_text, content_hash):
                        return True
            return False
    except sqlite3.Error as e:
        logger.error("Failed to check notification status", error=str(e))
        return False

def is_similar_notification(text1: str, text2: str) -> bool:
    embeddings = model.encode([text1, text2], convert_to_tensor=True)
    similarity = util.pytorch_cos_sim(embeddings[0], embeddings[1]).item()
    return similarity > SIMILARITY_THRESHOLD

def mark_notification_sent(url: str, notification_id: str, content_hash: str, semantic_hash: str, text: str, date: str):
    try:
        with sqlite3.connect(SENT_POSTS_DB) as conn:
            conn.execute(
                """INSERT OR IGNORE INTO notifications 
                (id, url, content_hash, semantic_hash, notification_text, extracted_date) 
                VALUES (?, ?, ?, ?, ?, ?)""",
                (notification_id, url, content_hash, semantic_hash, text, date)
            )
            conn.commit()
        logger.debug("Notification marked as sent in database")
    except sqlite3.Error as e:
        logger.error("Failed to mark notification as sent", error=str(e))

async def fetch_with_playwright(url: str) -> Optional[str]:
    """Fetch page using Playwright (fallback only)."""
    logger.warning(f"Using Playwright fallback for {url}. Ensure cookies are preloaded.")
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=False,
                args=['--disable-blink-features=AutomationControlled', '--no-sandbox']
            )
            context = await browser.new_context(
                user_agent=random.choice(USER_AGENTS),
                viewport={'width': random.randint(1280, 1920), 'height': random.randint(720, 1080)},
                locale='en-US',
                timezone_id='Asia/Kolkata',
                java_script_enabled=True,
                proxy={'server': random.choice(PROXY_POOL) if PROXY_POOL else None}
            )
            page = await context.new_page()
            await stealth_async(page)

            try:
                if not url.startswith(('http://', 'https://')):
                    url = f'https://{url}'
                
                parsed = urlparse(url)
                homepage = f"{parsed.scheme}://{parsed.netloc}"
                await page.goto(homepage, timeout=TIMEOUT*1000, wait_until="domcontentloaded")
                await asyncio.sleep(random.uniform(3, 7))
                
                await page.mouse.move(random.randint(100, 500), random.randint(100, 500))
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight / 4)")
                await asyncio.sleep(random.uniform(2, 5))
                
                await page.goto(url, timeout=TIMEOUT*1000, wait_until="networkidle")
                await asyncio.sleep(random.uniform(3, 7))
                
                content = await page.content()
                if any(term in content.lower() for term in ["captcha", "verify you are not a robot", "cloudflare"]):
                    logger.warning(f"Challenge detected on {url}. Solve manually in Chrome.")
                    return None
                
                await browser.close()
                return content
            except Exception as e:
                logger.warning("Playwright navigation failed", url=url, error=str(e))
                await browser.close()
                return None
    except Exception as e:
        logger.error("Playwright initialization failed", error=str(e))
        return None

def fetch_with_requests(url: str) -> Optional[str]:
    """Fetch page using requests (final fallback)."""
    logger.info(f"Using requests fallback for {url}")
    session = requests.Session()
    retries = Retry(total=1, backoff_factor=0.1, status_forcelist=[429])
    session.mount('http://', HTTPAdapter(max_retries=retries))
    session.mount('https://', HTTPAdapter(max_retries=retries))
    
    headers = {
        'User-Agent': random.choice(USER_AGENTS),
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9,hi;q=0.8',
        'Referer': 'https://www.google.com/',
        'Connection': 'keep-alive',
    }
    cookies = load_cookies()
    proxy = random.choice(PROXY_POOL) if PROXY_POOL else None
    
    try:
        response = session.get(
            url,
            headers=headers,
            cookies=cookies,
            timeout=10,
            verify=False,  # Disable SSL verification
            proxies={'http': proxy, 'https': proxy}
        )
        if response.status_code == 403:
            logger.warning(f"403 Forbidden on {url} with requests. Update cookies.")
            return None
        elif response.status_code == 429:
            logger.warning(f"429 Rate Limit on {url}. Waiting 30s...")
            time.sleep(30)
            return None
        response.raise_for_status()
        content = response.text
        if any(term in content.lower() for term in ["captcha", "verify you are not a robot", "cloudflare"]):
            logger.warning(f"Challenge detected on {url}. Solve manually in Chrome.")
            return None
        # Save cookies
        save_cookies({c.name: c.value for c in session.cookies})
        return content
    except requests.exceptions.RequestException as e:
        logger.error(f"Requests fallback failed for {url}: {str(e)}")
        return None

async def fetch_page(url: str, session: aiohttp.ClientSession) -> Optional[str]:
    """Fetch page with aiohttp, falling back to Playwright or requests."""
    if not url.startswith(('http://', 'https://')):
        url = f'https://{url}'
    headers = {
        'User-Agent': random.choice(USER_AGENTS),
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9,hi;q=0.8',
        'Referer': 'https://www.google.com/',
        'Connection': 'keep-alive',
    }
    for attempt in range(1, RETRIES + 1):
        try:
            proxy = random.choice(PROXY_POOL) if PROXY_POOL else None
            async with session.get(
                url, 
                headers=headers, 
                timeout=TIMEOUT,
                proxy=proxy,
                ssl=False  # Disable SSL verification
            ) as response:
                if response.status == 403:
                    logger.warning(f"403 Forbidden on {url} with aiohttp")
                    AIOHTTP_FAILURES[url] += 1
                    break
                elif response.status == 429:
                    retry_after = int(response.headers.get('Retry-After', 30))
                    logger.warning(f"429 Rate Limit on {url}, retrying after {retry_after}s")
                    AIOHTTP_FAILURES[url] += 1
                    await asyncio.sleep(retry_after)
                    continue
                response.raise_for_status()
                content = await response.text(encoding='utf-8', errors='ignore')
                if any(term in content.lower() for term in ["captcha", "verify you are not a robot", "cloudflare"]):
                    logger.warning(f"Challenge detected on {url}. Solve manually in Chrome.")
                    AIOHTTP_FAILURES[url] += 1
                    break
                AIOHTTP_FAILURES[url] = 0  # Reset failure count on success
                return content
        except Exception as e:
            logger.warning(f"Attempt {attempt} failed for URL: {url}", error=str(e))
            AIOHTTP_FAILURES[url] += 1
            await asyncio.sleep(2 ** attempt)
    
    # Check if aiohttp failures exceed threshold
    if AIOHTTP_FAILURES[url] >= AIOHTTP_FAILURE_THRESHOLD:
        logger.info(f"aiohttp failed {AIOHTTP_FAILURE_THRESHOLD} times for {url}. Switching to requests.")
        return fetch_with_requests(url)
    
    # Fallback to Playwright if cookies are available
    if os.path.exists(COOKIE_FILE):
        playwright_content = await fetch_with_playwright(url)
        if playwright_content:
            AIOHTTP_FAILURES[url] = 0  # Reset failure count
            return playwright_content
    
    logger.error(f"All attempts failed for URL: {url}. Try manual cookie extraction.")
    return None

def extract_dates(text: str) -> List[datetime]:
    if not text:
        return []
    month_names = r'(?:January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)'
    patterns = [
        r'\b(\d{1,2})[-/](\d{1,2})[-/](\d{2,4})\b',
        r'\b(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(\d{4})\b',
        r'\b(\d{4})[-/](\d{1,2})[-/](\d{1,2})\b',
        r'\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2}(?:st|nd|rd|th)?\s+\d{4}\b',
        r'\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2}(?:st|nd|rd|th)?\s+\d{4}\b'
    ]
    dates = []
    current_year = datetime.now().year
    current_month = datetime.now().month
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            try:
                dt = date_parse(match.group(0), dayfirst=True, yearfirst=True)
                if dt.year == current_year and dt.month == current_month:
                    dates.append(dt)
            except ValueError:
                continue
    return dates

def is_relevant_notification(text: str) -> bool:
    if not text or len(text) < MIN_NOTIFICATION_LENGTH or len(text) > MAX_NOTIFICATION_LENGTH:
        return False
    text_lower = text.lower()
    if any(exclude_word.lower() in text_lower for exclude_word in EXCLUDE_WORDS):
        return False
    if any(keyword.lower() in text_lower for keyword in KEYWORDS):
        return True
    notification_phrases = [
        'last date', 'apply online', 'registration', 'download', 'published',
        'advertisement', 'announcement', 'circular', 'notice', 'apply before',
        'last date to apply', 'application form', 'admit card', 'result',
        'extends', 'extended', 'vacancy', 'vacancies', 'candidates', 'exam',
        'examination', 'declared'
    ]
    return any(phrase in text_lower for phrase in notification_phrases)

def generate_content_hash(text: str) -> str:
    normalized = re.sub(r'\s+', ' ', text.strip().lower())
    normalized = re.sub(r'\d+', '', normalized)
    normalized = re.sub(r'\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\b', '', normalized)
    return str(hash(normalized))

def generate_semantic_hash(text: str) -> str:
    embedding = model.encode(text, convert_to_tensor=True)
    return str(embedding.mean().item())

def extract_notifications(url: str, html: str) -> List[Dict[str, str]]:
    if not html:
        return []
    soup = BeautifulSoup(html, 'html.parser')
    notifications = []
    current_month = datetime.now().month
    current_year = datetime.now().year
    for a in soup.find_all('a', href=True):
        text = a.get_text(strip=True)
        href = a['href']
        if not text or not href:
            continue
        if not any(keyword.lower() in text.lower() for keyword in KEYWORDS):
            continue
        if href.startswith("/"):
            href = urljoin(url, href)
        dates = extract_dates(text)
        if not dates:
            # Try extracting date from nearby elements
            candidates = [
                a.get('title', ''),
                a.find_next_sibling(text=True).strip() if a.find_next_sibling(text=True) else '',
                a.parent.get_text(" ", strip=True) if a.parent else ''
            ]
            for candidate in candidates:
                dates = extract_dates(candidate)
                if dates:
                    break
        if not dates:
            continue
        most_recent_date = max(dates)
        content_hash = generate_content_hash(text)
        semantic_hash = generate_semantic_hash(text)
        notification_id = f"{url}:{content_hash}"
        is_pdf = href.lower().endswith(".pdf")
        notification_text = (
            f"{'PDF' if is_pdf else 'Post'}: {text}\n"
            f"{'PDF Link' if is_pdf else 'Link'}: {href}"
        )
        notifications.append({
            'id': notification_id,
            'url': url,
            'text': notification_text,
            'date': most_recent_date.strftime('%Y-%m-%d'),
            'content_hash': content_hash,
            'semantic_hash': semantic_hash
        })
        if len(notifications) >= MAX_NOTIFICATIONS_PER_URL:
            break
    return notifications

async def send_telegram_notification(notification: Dict[str, str], url: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("Telegram credentials not configured")
        return False
    telegram_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    retry_delay = 1
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
    while retry_delay < 60:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(telegram_url, json=payload, timeout=30) as response:
                    if response.status == 200:
                        return True
                    elif response.status == 429:
                        retry_after = int((await response.json()).get('parameters', {}).get('retry_after', 5))
                        await asyncio.sleep(retry_after)
                        continue
                    logger.error(f"Telegram API error: {await response.text()}")
                    return False
        except Exception as e:
            logger.error(f"Failed to send Telegram message: {str(e)}")
            await asyncio.sleep(retry_delay)
            retry_delay *= 2
    return False

async def check_robots_txt(url: str, session: aiohttp.ClientSession) -> Tuple[bool, float]:
    if not RESPECT_ROBOTS:
        return True, DELAY_BETWEEN_REQUESTS
    parsed = urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    try:
        async with session.get(robots_url, timeout=10, ssl=False) as response:
            if response.status == 200:
                content = await response.text()
                if f"User-agent: {USER_AGENT.split('/')[0]}" in content:
                    if "Disallow: /" in content:
                        return False, 0
                    delay_match = re.search(r"Crawl-delay:\s*(\d+)", content)
                    if delay_match:
                        return True, float(delay_match.group(1))
                return True, DELAY_BETWEEN_REQUESTS
            return True, DELAY_BETWEEN_REQUESTS
    except Exception:
        return True, DELAY_BETWEEN_REQUESTS

async def fetch_rss_feed(url: str, session: aiohttp.ClientSession) -> Optional[List[Dict]]:
    parsed = urlparse(url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    feed_paths = [
        "/feed", "/rss", "/atom.xml", "/feed.xml", "/rss.xml",
        "/notifications/feed", "/news/feed"
    ]
    for path in feed_paths:
        feed_url = urljoin(base_url, path)
        try:
            async with session.get(feed_url, timeout=RSS_TIMEOUT, ssl=False) as response:
                if response.status == 200:
                    content = await response.text()
                    return parse_feed(content, base_url)
        except Exception as e:
            logger.debug(f"Failed to fetch RSS feed at {feed_url}", error=str(e))
    return None

def parse_feed(content: str, base_url: str) -> List[Dict]:
    items = []
    try:
        root = ET.fromstring(content)
        for item in root.findall('.//item') or root.findall('.//entry'):
            title = item.findtext('title', '').strip()
            link = item.findtext('link', '').strip()
            description = item.findtext('description', '') or item.findtext('content:encoded', '') or ''
            pub_date = item.findtext('pubDate', '') or item.findtext('published', '')
            if not link.startswith('http'):
                link = urljoin(base_url, link)
            if title:
                items.append({
                    'title': title,
                    'url': link,
                    'text': f"{title}\n\n{description}",
                    'date': pub_date
                })
    except Exception as e:
        logger.error("Failed to parse feed", error=str(e))
    return items

async def fetch_sitemap(url: str, session: aiohttp.ClientSession) -> Optional[List[Dict]]:
    parsed = urlparse(url)
    sitemap_url = f"{parsed.scheme}://{parsed.netloc}/sitemap.xml"
    try:
        async with session.get(sitemap_url, timeout=SITEMAP_TIMEOUT, ssl=False) as response:
            if response.status == 200:
                content = await response.text()
                return parse_sitemap(content)
    except Exception as e:
        logger.debug(f"Failed to fetch sitemap at {sitemap_url}", error=str(e))
    return None

def parse_sitemap(content: str) -> List[Dict]:
    items = []
    try:
        root = ET.fromstring(content)
        namespace = {'ns': 'http://www.sitemaps.org/schemas/sitemap/0.9'}
        for url in root.findall('.//ns:url', namespace):
            loc = url.findtext('ns:loc', '').strip()
            lastmod = url.findtext('ns:lastmod', '').strip()
            if loc and any(keyword in loc.lower() for keyword in KEYWORDS):
                items.append({
                    'url': loc,
                    'date': lastmod
                })
    except Exception as e:
        logger.error("Failed to parse sitemap", error=str(e))
    return items

async def process_url(url: str, session: aiohttp.ClientSession) -> Optional[List[Dict]]:
    allowed, delay = await check_robots_txt(url, session)
    if not allowed:
        logger.info(f"Skipping {url} due to robots.txt restrictions")
        return None
    await asyncio.sleep(delay + random.uniform(0, 600))  # Randomize delay up to 20 minutes
    rss_items = await fetch_rss_feed(url, session)
    if rss_items:
        logger.debug(f"Found {len(rss_items)} items in RSS feed for {url}")
        return rss_items
    sitemap_items = await fetch_sitemap(url, session)
    if sitemap_items:
        logger.debug(f"Found {len(sitemap_items)} relevant items in sitemap for {url}")
        return sitemap_items
    logger.debug(f"Falling back to HTML scraping for {url}")
    html = await fetch_page(url, session)
    if html:
        return extract_notifications(url, html)
    return None

async def monitor_urls(urls: List[str]):
    init_db()
    cleanup_old_entries()
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
    async def process_with_semaphore(url):
        async with semaphore:
            async with aiohttp.ClientSession(cookies=load_cookies()) as session:
                return await process_url(url, session)
    while True:
        tasks = [process_with_semaphore(url) for url in urls]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for url, result in zip(urls, results):
            if isinstance(result, Exception):
                logger.error(f"Failed to process URL: {url}", error=str(result))
                continue
            elif not result:
                continue
            for notification in result:
                if 'content_hash' not in notification:
                    text = notification.get('text', notification.get('title', ''))
                    notification['content_hash'] = generate_content_hash(text)
                    notification['semantic_hash'] = generate_semantic_hash(text)
                    notification['id'] = f"{url}:{notification['content_hash']}"
                if is_notification_sent(notification['content_hash'], notification['semantic_hash']):
                    continue
                if await send_telegram_notification(notification, url):
                    mark_notification_sent(
                        url=url,
                        notification_id=notification['id'],
                        content_hash=notification['content_hash'],
                        semantic_hash=notification['semantic_hash'],
                        text=notification.get('text', ''),
                        date=notification.get('date', 'No date')
                    )
                    logger.info(f"Sent notification: {notification['id']}")
                else:
                    logger.error(f"Failed to send notification: {notification['id']}")
        await asyncio.sleep(random.uniform(DELAY_BETWEEN_REQUESTS, DELAY_BETWEEN_REQUESTS + 600))  # 10–20 minutes

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Website Notification Monitor")
    parser.add_argument('--urls', nargs='+', help="Optional: Override URLs from url.py")
    parser.add_argument('--log-level', default='INFO', choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'], help="Logging level")
    args = parser.parse_args()
    logging.basicConfig(level=args.log_level)
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("Telegram credentials not configured")
        exit(1)
    urls_to_monitor = args.urls if args.urls else URLS
    if not urls_to_monitor:
        logger.error("No URLs provided")
        exit(1)
    logger.info(f"Starting monitoring for {len(urls_to_monitor)} URLs")
    asyncio.run(monitor_urls(urls_to_monitor))
