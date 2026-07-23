import re
from typing import Any, Dict, List


def _normalize_token(token: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", token.lower())


def check_hard_filters(job: Dict[str, Any], user_settings: Dict[str, Any]) -> bool:
    """Apply the first-level hard filters to a job for a single user."""
    combined_text = f"{job.get('title', '')} {job.get('raw_text', '')}"
    normalized_text = _normalize_token(combined_text)
    title_text = _normalize_token(job.get("title") or "")

    stop_words = user_settings.get("stop_words") or []
    normalized_stop_words = {_normalize_token(word) for word in stop_words if _normalize_token(word)}
    if normalized_stop_words and any(word in normalized_text for word in normalized_stop_words):
        return False

    keywords = user_settings.get("keywords") or []
    normalized_keywords = {_normalize_token(word) for word in keywords if _normalize_token(word)}
    if normalized_keywords:
        if not any(keyword in normalized_text for keyword in normalized_keywords):
            return False

    min_salary = user_settings.get("min_salary")
    if min_salary is not None:
        salary_text = f"{job.get('salary_info') or ''} {title_text} {normalized_text}"
        salary_numbers = re.findall(r"\d+", salary_text)
        if salary_numbers:
            salary_value = int(salary_numbers[0])
            if salary_value < int(min_salary):
                return False

    return True
