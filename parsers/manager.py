import json
from typing import Any, Dict, List, Optional

from database.models import get_all_users_with_settings, save_job, save_or_update_user_job_match, upsert_user_and_settings
from parsers.filters import check_hard_filters
from parsers.rss_parser import fetch_rss_jobs
from parsers.search_service import build_search_strategy, build_search_urls


async def _get_user_settings(user_id: int) -> Dict[str, Any]:
    users = await get_all_users_with_settings()
    for user in users:
        if user.get("user_id") == user_id:
            return {
                "user_id": user.get("user_id"),
                "keywords": user.get("keywords") or [],
                "stop_words": user.get("stop_words") or [],
                "min_salary": user.get("min_salary"),
                "bio_prompt": user.get("bio_prompt") or "",
                "preferences": user.get("preferences") or "",
                "search_strategy": user.get("search_strategy"),
            }
    return {}


async def process_jobs(user_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """Fetch RSS jobs using a dynamic search strategy tailored to the active user."""
    if user_id is None:
        jobs = await fetch_rss_jobs()
        return jobs

    user_settings = await _get_user_settings(user_id)
    strategy = await build_search_strategy(user_settings, user_profile={"user_id": user_id})
    await upsert_user_and_settings(
        user_id=user_id,
        keywords=user_settings.get("keywords") or [],
        stop_words=user_settings.get("stop_words") or [],
        min_salary=user_settings.get("min_salary"),
        bio_prompt=user_settings.get("bio_prompt") or "",
        preferences=user_settings.get("preferences") or "",
        search_strategy=json.dumps(strategy, ensure_ascii=False),
    )
    urls = await build_search_urls(strategy)
    feed_specs = [(item["source"], item["url"]) for item in urls if item.get("url")]

    jobs = await fetch_rss_jobs(feed_specs)
    filtered_jobs: List[Dict[str, Any]] = []
    for job in jobs:
        await save_job(
            job_id=job["job_id"],
            source=job["source"],
            title=job["title"],
            url=job["url"],
            raw_text=job["raw_text"],
            salary_info=job.get("salary_info"),
        )

        if not check_hard_filters(job, user_settings):
            await save_or_update_user_job_match(
                user_id=user_id,
                job_id=job["job_id"],
                summary=f"Filtered out: {job['title']}",
                status="filtered_out",
            )
            continue

        filtered_jobs.append(job)

    return filtered_jobs
