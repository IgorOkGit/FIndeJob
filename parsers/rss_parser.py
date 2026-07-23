import asyncio
import hashlib
import html
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

import feedparser
import httpx

logger = logging.getLogger(__name__)

RSS_FEEDS: List[Tuple[str, str]] = [
    ("DOU", "https://jobs.dou.ua/vacancies/feeds/?category=Technical+Support"),
    ("Djinni", "https://djinni.co/jobs/rss/?category=sysadmin"),
    ("Upwork", "https://www.upwork.com/ab/feed/jobs/rss"),
]


def _clean_html(text: Optional[str]) -> str:
    if not text:
        return ""
    text = html.unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _extract_salary_info(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    patterns = [
        r"\$\s?\d[\d,]*(?:\s*-\s*\$?\d[\d,]*)?",
        r"\b\d{2,6}\s*(?:k|K|usd|USD)\b",
        r"\b\d{3,6}\s*(?:usd|USD)\s*/\s*(?:mo|month|year|yr)\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(0)
    return None


def _build_job_id(source: str, title: str, url: str, raw_text: str) -> str:
    payload = f"{source}|{title}|{url}|{raw_text}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


async def _fetch_feed(feed_url: str, source: str, client: httpx.AsyncClient) -> List[Dict[str, Any]]:
    try:
        response = await client.get(
            feed_url,
            follow_redirects=True,
            timeout=20.0,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        response.raise_for_status()
    except Exception as exc:
        logger.warning("RSS fetch failed for %s (%s): %s", source, feed_url, exc)
        return []

    try:
        parsed = feedparser.parse(response.text)
    except Exception as exc:
        logger.warning("RSS parse failed for %s: %s", source, exc)
        return []

    jobs: List[Dict[str, Any]] = []
    for entry in parsed.entries:
        title = _clean_html(entry.get("title") or "")
        link = entry.get("link") or entry.get("id") or ""
        summary = entry.get("summary") or entry.get("description") or ""
        content_text = ""
        if entry.get("content"):
            content_text = " ".join(
                _clean_html(item.get("value") or "")
                for item in entry.get("content", [])
                if isinstance(item, dict)
            )
        raw_text = _clean_html(" ".join(filter(None, [summary, content_text])))
        salary_info = _extract_salary_info(f"{title} {raw_text}")

        jobs.append(
            {
                "job_id": entry.get("id") or _build_job_id(source, title, link, raw_text),
                "source": source,
                "title": title or "Untitled vacancy",
                "url": link,
                "raw_text": raw_text,
                "salary_info": salary_info,
            }
        )
    return jobs


async def fetch_rss_jobs(feed_specs: Optional[List[Tuple[str, str]]] = None) -> List[Dict[str, Any]]:
    """Fetch job postings from configured RSS feeds and return normalized job dictionaries."""
    specs = feed_specs or RSS_FEEDS
    async with httpx.AsyncClient(headers={"User-Agent": "Mozilla/5.0"}) as client:
        results = await asyncio.gather(
            *[_fetch_feed(url, source, client) for source, url in specs],
            return_exceptions=True,
        )

    jobs: List[Dict[str, Any]] = []
    for result in results:
        if isinstance(result, list):
            jobs.extend(result)
    return jobs
