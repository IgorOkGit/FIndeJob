import json
import logging
from typing import Any, Dict, List, Optional

from ai.gemini_analyzer import generate_search_strategy

logger = logging.getLogger(__name__)

DEFAULT_SEARCH_STRATEGY = {
    "sources": [
        {"name": "DOU", "category": "Technical Support", "keywords": ["technical support", "support specialist"]},
        {"name": "Djinni", "category": "project-manager", "keywords": ["project manager", "delivery manager"]},
    ],
    "fallback_keywords": ["technical support", "support specialist", "service desk"],
    "search_terms": ["technical support", "support specialist"],
}


def normalize_search_strategy(payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return DEFAULT_SEARCH_STRATEGY.copy()

    sources = payload.get("sources") or DEFAULT_SEARCH_STRATEGY["sources"]
    normalized_sources: List[Dict[str, Any]] = []
    for source in sources:
        if not isinstance(source, dict):
            continue
        normalized_sources.append(
            {
                "name": str(source.get("name") or "DOU"),
                "category": str(source.get("category") or ""),
                "keywords": [str(keyword) for keyword in (source.get("keywords") or []) if str(keyword).strip()],
            }
        )

    fallback_keywords = [str(keyword) for keyword in (payload.get("fallback_keywords") or DEFAULT_SEARCH_STRATEGY["fallback_keywords"]) if str(keyword).strip()]
    search_terms = [str(term) for term in (payload.get("search_terms") or DEFAULT_SEARCH_STRATEGY["search_terms"]) if str(term).strip()]

    return {
        "sources": normalized_sources or DEFAULT_SEARCH_STRATEGY["sources"],
        "fallback_keywords": fallback_keywords or DEFAULT_SEARCH_STRATEGY["fallback_keywords"],
        "search_terms": search_terms or DEFAULT_SEARCH_STRATEGY["search_terms"],
    }


async def build_search_strategy(user_settings: Dict[str, Any], user_profile: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Ask Gemini for a dynamic search strategy tailored to the user's profile and preferences."""
    existing_strategy = user_settings.get("search_strategy")
    if isinstance(existing_strategy, str) and existing_strategy.strip():
        try:
            parsed = json.loads(existing_strategy)
            if isinstance(parsed, dict):
                return normalize_search_strategy(parsed)
        except json.JSONDecodeError:
            logger.warning("Stored search strategy is not valid JSON, generating a new one")

    profile_text = json.dumps(
        {
            "bio_prompt": user_settings.get("bio_prompt") or "",
            "preferences": user_settings.get("preferences") or "",
            "keywords": user_settings.get("keywords") or [],
            "stop_words": user_settings.get("stop_words") or [],
            "user_profile": user_profile or {},
        },
        ensure_ascii=False,
    )
    prompt = f"""
You are an expert job search strategist.
Given the user's profile and preferences below, return a compact JSON object with:
- sources: array of objects with name, category, keywords
- fallback_keywords: array of keywords to use when a source has no matching results
- search_terms: array of search terms to try across sites

User context:
{profile_text}

Return only valid JSON.
"""
    response = await generate_search_strategy(prompt)
    if response is None:
        logger.warning("Gemini search strategy unavailable, using default fallback")
        return DEFAULT_SEARCH_STRATEGY.copy()

    return normalize_search_strategy(response)


async def build_search_urls(strategy: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Build candidate URLs for supported job boards from a dynamic search strategy."""
    urls: List[Dict[str, Any]] = []
    for source in strategy.get("sources") or []:
        name = str(source.get("name") or "").upper()
        category = str(source.get("category") or "").strip()
        keywords = [str(keyword) for keyword in (source.get("keywords") or []) if str(keyword).strip()]
        if not keywords and not category:
            continue

        if name == "DOU":
            if category:
                encoded = category.replace(" ", "+")
                urls.append({"source": name, "url": f"https://jobs.dou.ua/vacancies/feeds/?category={encoded}"})
            else:
                urls.append({"source": name, "url": "https://jobs.dou.ua/vacancies/feeds/?category=Technical+Support"})
        elif name == "DJINNI":
            if category:
                urls.append({"source": name, "url": f"https://djinni.co/jobs/rss/?category={category.lower().replace(' ', '-')}"})
            else:
                urls.append({"source": name, "url": "https://djinni.co/jobs/rss/?category=sysadmin"})
        elif name == "UPWORK":
            urls.append({"source": name, "url": "https://www.upwork.com/ab/feed/jobs/rss"})
        else:
            urls.append({"source": name, "url": ""})

    if not urls:
        urls = [{"source": "DOU", "url": "https://jobs.dou.ua/vacancies/feeds/?category=Technical+Support"}]
    return urls
