import requests
import httpx
from bs4 import BeautifulSoup
import urllib3
import os
from datetime import datetime, timedelta
from dateutil.parser import parse as date_parse
from io import BytesIO
import re
import PyPDF2
import pdfplumber
from urllib.parse import urljoin, urlparse, urlencode, parse_qs
from dotenv import load_dotenv
import logging
import time
import uuid

from url import URLS  # URL list

# Disable SSL warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('website_monitor.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()
TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# Validate environment variables
if not TOKEN or not CHAT_ID:
    logger.error("Missing Telegram TOKEN or CHAT_ID in environment variables")
    logger.error(f"TOKEN is {'set' if TOKEN else 'not set'}")
    logger.error(f"CHAT_ID is {'set' if CHAT_ID else 'not set'}")
    logger.error("Available environment variables: %s", list(os.environ.keys()))
    if os.path.exists('.env'):
        with open('.env', 'r') as f:
            logger.error("Content of .env file: %s", f.read())
    else:
        logger.error(".env file not found")
    exit(1)
else:
    logger.info("Successfully loaded TOKEN and CHAT_ID")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
}

SENT_POSTS_FILE = "sent_posts.txt"

KEYWORDS = [
    "job", "result", "notification", "admit card", "notice", "exam",
    "interview", "vacancy", "recruitment", "call letter", "merit list",
    "schedule", "announcement", "bulletin"
]

def contains_keyword(text):
    """Check if text contains any of the predefined keywords."""
    if not text:
        return False
    text_lower = text.lower()
    return any(keyword in text_lower for keyword in KEYWORDS)

def load_sent_posts():
    """Load previously sent posts from file."""
    sent_posts = set()
    if not os.path.exists(SENT_POSTS_FILE):
        logger.info(f"{SENT_POSTS_FILE} not found, starting fresh")
        return sent_posts
    try:
        with open(SENT_POSTS_FILE, "r", encoding="utf-8") as f:
            sent_posts = set(line.strip() for line in f if line.strip())
        logger.info(f"Loaded {len(sent_posts)} sent posts")
        return sent_posts
    except Exception as e:
        logger.error(f"Error reading {SENT_POSTS_FILE}: {e}")
        return set()

def save_sent_post(post_id):
    """Save a post ID to the sent posts file with immediate flush."""
    try:
        with open(SENT_POSTS_FILE, "a", encoding="utf-8") as f:
            f.write(post_id + "\n")
            f.flush()
        logger.info(f"Saved post ID: {post_id}")
    except Exception as e:
        logger.error(f"Error saving post {post_id} to {SENT_POSTS_FILE}: {e}")

def send_telegram(message):
    """Send a message to Telegram with retry logic."""
    if not TOKEN or not CHAT_ID:
        logger.error("Telegram TOKEN or CHAT_ID missing during send attempt")
        return False
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    data = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": False
    }
    for attempt in range(2):
        try:
            r = requests.post(url, data=data, timeout=10)
            r.raise_for_status()
            logger.info("Telegram message sent successfully")
            time.sleep(2)
            return True
        except requests.RequestException as e:
            logger.warning(f"Telegram send attempt {attempt + 1} failed: {e}")
            time.sleep(2 ** attempt)
    logger.error("Failed to send Telegram message after retries")
    return False

def fetch(url):
    """Fetch HTML content from a URL with fallback to httpx."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10, verify=False)
        resp.raise_for_status()
        logger.info(f"Fetched {url} successfully")
        time.sleep(2)
        return resp.text
    except requests.exceptions.SSLError as ssl_err:
        logger.warning(f"SSL Error for {url}: {ssl_err}. Trying httpx...")
        try:
            with httpx.Client(verify=False, timeout=10) as client:
                resp = client.get(url)
                resp.raise_for_status()
                logger.info(f"Fetched {url} successfully")
                time.sleep(2)
                return resp.text
        except Exception as e:
            logger.error(f"Error fetching {url}: {e}")
            return None
    except Exception as e:
        logger.error(f"Error fetching {url}: {e}")
        return None

def extract_date_from_text(text):
    """Extract a date from a text string using various patterns."""
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
                    return dt
            except ValueError:
                continue
    try:
        dt = date_parse(text, fuzzy=True, dayfirst=True)
        if 2000 <= dt.year <= datetime.now().year + 1:
            return dt
    except ValueError:
        pass
    return None

def extract_date_from_pdf(url):
    """Extract a date from a PDF file by parsing its text content."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=15, verify=False)
        r.raise_for_status()
        pdf_file = BytesIO(r.content)
        try:
            reader = PyPDF2.PdfReader(pdf_file)
            full_text = ""
            for page in reader.pages:
                try:
                    page_text = page.extract_text()
                    if page_text:
                        full_text += page_text + "\n"
                except Exception:
                    continue
            if not full_text:
                with pdfplumber.open(pdf_file) as pdf:
                    for page in pdf.pages:
                        try:
                            page_text = page.extract_text()
                            if page_text:
                                full_text += page_text + "\n"
                        except Exception:
                            continue
            lines = full_text.splitlines()
            date_candidates = []
            for line in lines:
                dt = extract_date_from_text(line)
                if dt:
                    date_candidates.append(dt)
            post_date = min(date_candidates) if date_candidates else None
            logger.info(f"Extracted PDF date: {post_date} from {url}")
            return post_date
        except Exception as e:
            logger.error(f"Error parsing PDF {url}: {e}")
            return None
    except requests.RequestException as e:
        logger.error(f"Error fetching PDF {url}: {e}")
        return None

def extract_date(a_tag):
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
            return dt
    return None

def extract_date_from_url(url):
    """Extract a date from a URL using common date patterns."""
    patterns = [
        r'/(\d{4})[/-](\d{1,2})[/-]?(\d{1,2})?',  # /2025/06 or /2025/06/12
        r'date=(\d{4})-(\d{2})-(\d{2})',           # ?date=2025-06-12
    ]
    for pattern in patterns:
        match = re.search(pattern, url, re.I)
        if match:
            try:
                year = int(match.group(1))
                month = int(match.group(2))
                day = 1 if len(match.groups()) < 3 else int(match.group(3))
                if 2000 <= year <= datetime.now().year + 1 and 1 <= month <= 12:
                    return datetime(year, month, day)
            except ValueError:
                continue
    return None

def normalize_url(url, base_url):
    """Normalize a URL by resolving relative paths and sorting query parameters."""
    try:
        full_url = urljoin(base_url, url)
        parsed = urlparse(full_url)
        query_params = parse_qs(parsed.query)
        sorted_query = urlencode({k: v[0] for k, v in sorted(query_params.items())}, doseq=True)
        clean_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        if sorted_query:
            clean_url += f"?{sorted_query}"
        return clean_url.rstrip('/')
    except Exception as e:
        logger.error(f"Error normalizing URL {url}: {e}")
        return url

def is_valid_url(url):
    """Check if a URL is valid by sending a HEAD request."""
    try:
        result = requests.head(url, headers=HEADERS, timeout=5, allow_redirects=True, verify=False)
        return result.status_code < 400
    except requests.RequestException as e:
        logger.error(f"Error validating URL {url}: {e}")
        return False

def is_post_link(href, base_url):
    """Check if a link is a relevant PDF link containing keywords."""
    if not href:
        return False
    full_url = normalize_url(href, base_url)
    if not full_url.lower().endswith('.pdf'):
        return False
    return contains_keyword(full_url) or contains_keyword(href)

def parse_and_notify(url, sent_posts):
    """Parse HTML content and notify about new PDF posts."""
    logger.info(f"Processing URL: {url}")
    html = fetch(url)
    if not html:
        logger.error(f"No data fetched for {url}")
        return []

    soup = BeautifulSoup(html, 'html.parser', from_encoding="utf-8")
    new_posts = []
    notifications = []

    now = datetime.now()
    start_of_week = now - timedelta(days=now.weekday())
    end_of_week = start_of_week + timedelta(days=6)
    start_of_week = start_of_week.replace(hour=0, minute=0, second=0, microsecond=0)
    end_of_week = end_of_week.replace(hour=23, minute=59, second=59, microsecond=999999)

    logger.info(f"Filtering posts for week: {start_of_week.strftime('%Y-%m-%d')} to {end_of_week.strftime('%Y-%m-%d')}")

    for a in soup.find_all('a', href=True):
        text = a.get_text(strip=True)
        href = a['href']

        if not text or not href:
            logger.debug(f"Skipping empty link: {href}")
            continue

        if not is_post_link(href, url):
            logger.debug(f"Skipping non-relevant link: {text} ({href})")
            continue

        post_id = normalize_url(href, url)
        logger.debug(f"Processing post: {href} -> Resolved to: {post_id}")

        if post_id in sent_posts:
            logger.info(f"Skipping already sent post: {post_id}")
            continue

        if not is_valid_url(post_id):
            logger.warning(f"Skipping invalid URL: {post_id}")
            continue

        post_date = extract_date(a)
        is_pdf = post_id.lower().endswith('.pdf')
        if is_pdf and not post_date:
            post_date = extract_date_from_pdf(post_id)
        if not post_date:
            post_date = extract_date_from_url(post_id)

        if not post_date:
            logger.info(f"No date found for {post_id}, skipping post")
            continue

        if post_date < start_of_week:
            logger.info(f"Stopping at old post {post_id}: Date {post_date.strftime('%Y-%m-%d')} before current week")
            break

        if not (start_of_week <= post_date <= end_of_week):
            logger.info(f"Skipping post {post_id}: Date {post_date.strftime('%Y-%m-%d')} not in current week")
            continue

        msg = f"*New Notification*\n\n"
        msg += f"Website: {url}\n"
        msg += f"Title: {text or 'Untitled Notification'}\n"
        msg += f"URL: {post_id}\n"
        msg += f"Publish Date: {post_date.strftime('%Y-%m-%d')}\n"
        msg += f"PDF URL: {post_id}\n"

        new_posts.append((post_id, msg))
        notifications.append({"title": text, "url": post_id, "date": post_date})

    if notifications:
        unique_notifications = set(f"{n['title']}: {n['url']}" for n in notifications)
        if len(unique_notifications) == 1:
            logger.info(f"All {len(notifications)} notifications are identical: {notifications[0]['title']}")
            send_telegram(f"All notifications for {url} this week are identical:\n\nTitle: {notifications[0]['title']}\nURL: {notifications[0]['url']}\nDate: {notifications[0]['date'].strftime('%Y-%m-%d')}}\n")
        else:
            latest = max(notifications, key=lambda x: x['date'])
            logger.info(f"Found {len(notifications)} notifications, latest: {latest['title']} on {latest['date'].strftime('%Y-%m-%d')}}\n")
            send_telegram(f"New notification found for {url}:\n\nTitle: {latest['title']}\nURL: {latest['url']}\nDate: {latest['date'].strftime('%Y-%m-%d')}}\n")

    return new_posts

def main():
    """Main function to monitor URLs and send notifications."""
    logger.info("Monitoring started...")
    sent_posts = load_sent_posts()
    for url in URLS:
        try:
            new_posts = parse_and_notify(url, sent_posts)
            if not new_posts:
                logger.info(f"No new relevant PDFs found for {url}")
                continue
            for post_id, message in new_posts:
                if send_telegram(message):
                    save_sent_post(post_id)
                    sent_posts.add(post_id)
                    logger.info(f"Sent and saved notification for: {post_id}")
                else:
                    logger.error(f"Failed to send notification for: {post_id}")
        except Exception as e:
            logger.error(f"Error processing {url}: {e}")
            continue
        time.sleep(2)

if __name__ == "__main__":
    main()
