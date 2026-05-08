"""Scrape Hacker News 'Who is Hiring?' threads for remote job postings."""

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from html import unescape

import pandas as pd
import requests

from db import clean_company_name

# Algolia HN search API to find the monthly threads
ALGOLIA_SEARCH = "https://hn.algolia.com/api/v1/search_by_date"
# HN Firebase API to get item details
HN_ITEM_API = "https://hacker-news.firebaseio.com/v0/item/{}.json"

MAX_WORKERS = 20

# How many months back to look
MAX_THREADS = 2


def find_hiring_threads(max_threads: int = MAX_THREADS) -> list[dict]:
    """Find recent 'Who is hiring?' threads by whoishiring."""
    resp = requests.get(
        ALGOLIA_SEARCH,
        params={
            "query": "Ask HN: Who is hiring?",
            "tags": "story,author_whoishiring",
            "hitsPerPage": max_threads,
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("hits", [])


_BLOCK_TAGS = re.compile(r"</?(?:p|br|li|div|tr|h[1-6]|pre)\b[^>]*>", re.IGNORECASE)
_ANY_TAG = re.compile(r"<[^>]+>")


def strip_html(text: str) -> str:
    """Remove HTML tags and decode entities.

    Block-level tags (p, br, li, div, ...) become newlines so paragraph
    structure is preserved. Inline tags (a, i, b, code, em, strong) are
    stripped to nothing so their text content stays on the same line — this
    prevents URL <a>...</a> wrappers from breaking the first header line.
    """
    text = _BLOCK_TAGS.sub("\n", text)
    text = _ANY_TAG.sub("", text)
    text = unescape(text)
    return text.strip()


LOCATION_HINTS = re.compile(
    r"remote|onsite|on-site|hybrid|office|"
    r"(?:san|new|los|austin|seattle|boston|chicago|london|berlin|paris|toronto|"
    r"vancouver|dublin|amsterdam|singapore|sydney|melbourne|tel aviv|"
    r"denver|portland|miami|atlanta|dallas|houston|raleigh|"
    r"pittsburgh|minneapolis|bangalore|hyderabad|mumbai|pune|"
    r"worldwide|anywhere|usa|eu\b|uk\b|us\b|canada|germany|"
    r"india|israel|japan|australia|europe|asia)",
    re.IGNORECASE,
)


def parse_header(first_line: str) -> dict:
    """Parse the pipe-delimited header line of an HN job comment.

    Format varies but company is always first. Location is the segment
    that looks most like a place (contains city/country/remote keywords).
    """
    parts = [p.strip() for p in first_line.split("|")]
    result = {"company": clean_company_name(parts[0]) if parts else None, "location": None}

    # Find the segment that looks most like a location
    for part in parts[1:]:
        if LOCATION_HINTS.search(part):
            result["location"] = part
            break

    return result


_REMOTE_PATTERNS = re.compile(
    r"\bremote(?:ly)?\b|"
    r"\bwfh\b|"
    r"work from (?:anywhere|home)|"
    r"(?:fully|globally|geographically)[- ]distributed|"
    r"distributed (?:team|workforce|company|organi[sz]ation)",
    re.IGNORECASE,
)


def is_remote(text: str) -> bool:
    """Check if a posting mentions remote work.

    "distributed" alone is too noisy — it matches "distributed systems" — so
    require it to be qualified (e.g. "fully distributed", "distributed team").
    """
    return bool(_REMOTE_PATTERNS.search(text))


def _fetch_item(item_id: int) -> dict | None:
    try:
        r = requests.get(HN_ITEM_API.format(item_id), timeout=10)
        r.raise_for_status()
        item = r.json()
        if item and not item.get("deleted") and not item.get("dead"):
            return item
    except Exception:
        pass
    return None


def fetch_comments(thread_id: int) -> list[dict]:
    """Fetch top-level comments (kids) of an HN thread concurrently."""
    resp = requests.get(HN_ITEM_API.format(thread_id), timeout=15)
    resp.raise_for_status()
    thread = resp.json()
    kid_ids = thread.get("kids", [])

    comments = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(_fetch_item, kid_id): kid_id for kid_id in kid_ids}
        for future in as_completed(futures):
            result = future.result()
            if result:
                comments.append(result)

    return comments


def scrape_hn(remote_only: bool = True) -> pd.DataFrame:
    """Scrape recent HN 'Who is hiring?' threads and return a DataFrame of postings."""
    threads = find_hiring_threads()
    if not threads:
        print("No HN 'Who is hiring?' threads found.")
        return pd.DataFrame()

    all_jobs = []

    for thread in threads:
        thread_id = int(thread["objectID"])
        thread_title = thread.get("title", "")
        print(f"[HN] Fetching: {thread_title} (id={thread_id}) ...")

        comments = fetch_comments(thread_id)
        print(f"  {len(comments)} top-level comments")

        for comment in comments:
            text = comment.get("text", "")
            if not text:
                continue

            plain = strip_html(text)
            lines = [l.strip() for l in plain.split("\n") if l.strip()]
            if not lines:
                continue

            if remote_only and not is_remote(plain):
                continue

            header = parse_header(lines[0])
            company = header["company"]
            location = header["location"]

            # Build a URL to the HN comment
            job_url = f"https://news.ycombinator.com/item?id={comment['id']}"

            # Use the comment timestamp as date_posted
            ts = comment.get("time")
            date_posted = (
                datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d") if ts else None
            )

            # Use the first line as the title (company | location | ...)
            title = lines[0][:200]

            all_jobs.append(
                {
                    "title": title,
                    "company": company,
                    "location": location,
                    "job_url": job_url,
                    "date_posted": date_posted,
                    "search_term": "hackernews",
                    "description": plain,
                    "source": "hn",
                }
            )

    if not all_jobs:
        print("[HN] No remote postings found.")
        return pd.DataFrame()

    df = pd.DataFrame(all_jobs)
    df = df.drop_duplicates(subset=["job_url"], keep="first")
    print(f"[HN] {len(df)} remote postings extracted.")
    return df
