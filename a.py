import requests
import httpx
from bs4 import BeautifulSoup
import urllib3
import os
from datetime import datetime
from dateutil.parser import parse as date_parse
from io import BytesIO
import re
import PyPDF2
import pdfplumber
from urllib.parse import urljoin, urlparse
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
# Note: load_dotenv() is optional since GitHub Actions sets variables directly
load_dotenv()
TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# Validate environment variables with detailed debugging
if not TOKEN or not CHAT_ID:
    logger.error("Missing Telegram TOKEN or CHAT_ID in environment variables")
    logger.error(f"TOKEN is {'set' if TOKEN else 'not set'}")
    logger.error(f"CHAT_ID is {'set' if CHAT_ID else 'not set'}")
    logger.error("Available environment variables: %s", list(os.environ.keys()))
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
    if not os.path.exists(SENT_POSTS_FILE):
        logger.info(f"{SENT_POSTS_FILE} not found, starting fresh")
        return set()
    try:
        with open(SENT_POSTS_FILE, "r", encoding="utf-8") as f:
            posts = set(line.strip() for line in f if line.strip())
            logger.info(f"Loaded {len(posts)} sent posts")
            return posts
    except Exception as e:
        logger.error(f"Error reading {SENT_POSTS_FILE}: {e}")
        return set()

def save_sent_post(post_id):
    """Save a post ID to the sent posts file."""
    try:
        with open(SENT_POSTS_FILE, "a", encoding="utf-8") as f:
            f.write(post_id + "\n")
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
    for attempt in range(3):
        try:
            r = requests.post(url, data=data, timeout=10)
            r.raise_for_status()
            logger.info("Telegram message sent successfully")
            time.sleep(0.5)  # Rate limit
            return True
        except requests.RequestException as e:
            logger.warning(f"Telegram send attempt {attempt + 1} failed: {e}")
            time.sleep(2 ** attempt)  # Exponential backoff
    logger.error("Failed to send Telegram message after retries")
    return False

def fetch(url):
    """Fetch HTML content from a URL with fallback to httpx."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10, verify=False)
        resp.raise_for_status()
        logger.info(f"Fetched {url} successfully")
        time.sleep(1)  # Rate limit
        return resp.text
    except requests.exceptions.SSLError as ssl_err:
        logger.warning(f"SSL Error for {url}: {ssl_err}. Trying httpx fallback...")
        try:
            with httpx.Client(verify=False, timeout=10) as client:
                resp = client.get(url)
                resp.raise_for_status()
                logger.info(f"Fetched {url} successfully with httpx")
                time.sleep(1)
                return resp.text
        except Exception as e:
            logger.error(f"HTTPX fallback failed for {url}: {e}")
            return None
    except Exception as e:
        logger.error(f"Error fetching {url}: {e}")
        return None

def extract_date_from_text(text):
    """Extract a date from text using multiple patterns."""
    if not text:
        return None
    date_patterns = [
        r'\b(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\b',  # e.g., 01/06/2025, 01-06-25
        r'\b(\d{4}[/-]\d{1,2}[/-]\d{1,2})\b',  # e.g., 2025-06-01
        r'\b(\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)[a-z]*\s+\d{2,4})\b',  # e.g., 01 June 2025
        r'\b(\d{1,2}(?:st|nd|rd|th)?\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{2,4})\b',  # e.g., 1st Jun 2025
        r'\b(\d{1,2}-(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*-\d{2,4})\b',  # e.g., 01-Jun-2025
    ]
    for pattern in date_patterns:
        m = re.search(pattern, text, re.I)
        if m:
            try:
                dt = date_parse(m.group(1), dayfirst=True)
                if 2000 <= dt.year <= datetime.now().year + 1:
                    return dt
            except:
                continue
    try:
        dt = date_parse(text, fuzzy=True, dayfirst=True)
        if 2000 <= dt.year <= datetime.now().year + 1:
            return dt
    except:
        pass
    return None

def extract_date_from_pdf(url):
    """Extract metadata from a PDF, falling back to pdfplumber if PyPDF2 fails."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=15, verify=False)
        r.raise_for_status()
        pdf_file = BytesIO(r.content)

        # Try PyPDF2 first
        try:
            reader = PyPDF2.PdfReader(pdf_file)
            full_text = ""
            for page in reader.pages:
                try:
                    page_text = page.extract_text()
                    if page_text:
                        full_text += page_text + "\n"
                except Exception as e:
                    logger.warning(f"Error extracting text from page in {url}: {e}")
                    continue
        except Exception as e:
            logger.warning(f"PyPDF2 failed for {url}: {e}. Falling back to pdfplumber.")
            full_text = ""

        # Fallback to pdfplumber if PyPDF2 fails or no text extracted
        if not full_text:
            with pdfplumber.open(pdf_file) as pdf:
                for page in pdf.pages:
                    try:
                        page_text = page.extract_text()
                        if page_text:
                            full_text += page_text + "\n"
                    except Exception as e:
                        logger.warning(f"Error extracting text from page in {url} with pdfplumber: {e}")
                        continue

        lines = full_text.splitlines()
        date_candidates = []
        posts_count = None
        application_fee = None
        publish_date = None

        for line in lines:
            dt = extract_date_from_text(line)
            if dt:
                date_candidates.append(dt)

            if posts_count is None:
                m = re.search(r'(total|vacancy|posts?|positions?)[:\s]*([0-9,]+)', line, re.I)
                if m:
                    posts_count = m.group(2).strip()

            if application_fee is None:
                m = re.search(r'(application fee|fee)[:\s]*₹?\s*([\d,]+)', line, re.I)
                if m:
                    application_fee = m.group(2).strip()

            if publish_date is None:
                m = re.search(r'(published|date of issue|notification date|publish date)[:\s]*(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{2,4})', line, re.I)
                if m:
                    try:
                        publish_date = date_parse(m.group(2), dayfirst=True)
                    except:
                        pass

        post_date = min(date_candidates) if date_candidates else None
        logger.info(f"Extracted PDF date: {post_date} from {url}")

        return {
            "post_date": post_date,
            "posts_count": posts_count,
            "application_fee": application_fee,
            "publish_date": publish_date,
            "pdf_url": url,
            "pdf_text_snippet": full_text[:500]
        }
    except Exception as e:
        logger.error(f"Error extracting PDF from {url}: {e}")
        return {"pdf_url": url}

def extract_post_date(a_tag):
    """Extract a date from an <a> tag or its surrounding context."""
    candidates = []
    title_attr = a_tag.get('title')
    if title_attr:
        candidates.append(title_attr)

    next_sibling = a_tag.find_next_sibling(string=True)
    if next_sibling and isinstance(next_sibling, str):
        candidates.append(next_sibling.strip())

    parent = a_tag.parent
    if parent:
        candidates.append(parent.get_text(" ", strip=True))
        if parent.parent:
            candidates.append(parent.parent.get_text(" ", strip=True))

    for text in candidates:
        dt = extract_date_from_text(text)
        if dt:
            return dt
    return None

def extract_date_from_url(url):
    """Extract a date from a URL path."""
    m = re.search(r'/(\d{4})[/-](\d{1,2})/', url)
    if m:
        try:
            year = int(m.group(1))
            month = int(m.group(2))
            if 2000 <= year <= datetime.now().year + 1 and 1 <= month <= 12:
                return datetime(year, month, 1)
        except:
            pass
    return None

def normalize_url(url, base_url):
    """Normalize a URL by resolving relative paths and removing trailing slashes."""
    try:
        full_url = urljoin(base_url, url)
        parsed = urlparse(full_url)
        clean_url = parsed.scheme + "://" + parsed.netloc + parsed.path
        return clean_url.rstrip('/')
    except Exception as e:
        logger.error(f"Error normalizing URL {url}: {e}")
        return url

def is_valid_url(url):
    """Check if a URL is reachable."""
    try:
        result = requests.head(url, headers=HEADERS, timeout=5, allow_redirects=True, verify=False)
        return result.status_code < 400
    except Exception as e:
        logger.warning(f"URL {url} is invalid or unreachable: {e}")
        return False

def is_post_link(href, base_url):
    """Check if a link is relevant based on keywords or PDF extension."""
    if not href:
        return False
    full_url = normalize_url(href, base_url)
    return full_url.lower().endswith('.pdf') or contains_keyword(full_url) or contains_keyword(href)

def parse_and_notify(url, sent_posts):
    """Parse a webpage and send notifications for new relevant posts."""
    logger.info(f"Processing URL: {url}")
    html = fetch(url)
    if not html:
        logger.error(f"No data fetched from {url}")
        return

    soup = BeautifulSoup(html, "html.parser", from_encoding="utf-8")
    new_posts = []

    for a in soup.find_all('a', href=True):
        text = a.get_text(strip=True)
        href = a['href']

        if not text or not href:
            logger.info(f"Skipping empty link: {href}")
            continue

        if not is_post_link(href, url):
            logger.info(f"Skipping non-relevant link: {text}")
            continue

        post_id = normalize_url(href, url)
        logger.info(f"Processing post: {href} -> Resolved to: {post_id}")

        if post_id in sent_posts:
            logger.info(f"Skipping already sent post: {post_id}")
            continue

        if not is_valid_url(post_id):
            logger.info(f"Skipping invalid URL: {post_id}")
            continue

        post_date = extract_post_date(a)
        pdf_data = None
        if post_id.lower().endswith('.pdf'):
            pdf_data = extract_date_from_pdf(post_id)
            if pdf_data and pdf_data.get("post_date"):
                post_date = pdf_data["post_date"]

        if not post_date:
            post_date = extract_date_from_url(post_id)

        now = datetime.now()
        if not post_date:
            post_date = datetime(now.year, now.month, 1)
            logger.info(f"No date found for {post_id}, assuming current month: {post_date}")

        # Only process posts in current month
        if post_date.year != now.year or post_date.month != now.month:
            logger.info(f"Skipping post {post_id}: Date {post_date} not in current month")
            continue

        msg = f"*New Notification*\n\n"
        msg += f"Website URL: {url}\n"
        msg += f"Notification Title: {text or 'Untitled Notification'}\n"
        msg += f"Notification URL: {post_id}\n"
        publish_date = pdf_data.get('publish_date') if pdf_data else post_date
        msg += f"Publish Date: {publish_date.strftime('%Y-%m-%d') if publish_date else '_Not found_'}\n"
        msg += f"PDF URL: {pdf_data.get('pdf_url') if pdf_data else '_Not applicable_'}\n"

        new_posts.append((post_id, msg))

    if not new_posts:
        logger.info(f"No new relevant posts found on {url} for current month")
        return

    for post_id, message in new_posts:
        if send_telegram(message):
            save_sent_post(post_id)
            sent_posts.add(post_id)
            logger.info(f"Sent notification for: {post_id}")
        else:
            logger.error(f"Failed to send notification for: {post_id}")

def main():
    """Main function to start website monitoring."""
    logger.info("Monitoring started...")
    sent_posts = load_sent_posts()
    for url in URLS:
        try:
            parse_and_notify(url, sent_posts)
        except Exception as e:
            logger.error(f"Error processing URL {url}: {e}")
        time.sleep(2)  # Rate limit between URLs

if __name__ == "__main__":
    main()
