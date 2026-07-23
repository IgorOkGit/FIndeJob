from typing import Any, Dict, List

from database.models import (
    get_all_users_with_settings,
    save_job,
    save_or_update_user_job_match,
)
from parsers.filters import check_hard_filters
from parsers.rss_parser import fetch_rss_jobs


async def process_jobs() -> List[Dict[str, Any]]:
    """Fetch RSS jobs, store new ones, and apply hard filters for each active user."""
    jobs = await fetch_rss_jobs()
    users = await get_all_users_with_settings()

    saved_jobs: List[Dict[str, Any]] = []
    for job in jobs:
        await save_job(
            job_id=job["job_id"],
            source=job["source"],
            title=job["title"],
            url=job["url"],
            raw_text=job["raw_text"],
            salary_info=job.get("salary_info"),
        )
        saved_jobs.append(job)

        for user in users:
            user_settings = {
                "keywords": user.get("keywords") or [],
                "stop_words": user.get("stop_words") or [],
                "min_salary": user.get("min_salary"),
            }
            if not check_hard_filters(job, user_settings):
                await save_or_update_user_job_match(
                    user_id=user["user_id"],
                    job_id=job["job_id"],
                    summary=f"Filtered out: {job['title']}",
                    status="filtered_out",
                )

    return saved_jobs
