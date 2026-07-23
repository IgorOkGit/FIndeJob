import json
import logging
from typing import Any, Dict, List, Optional

from ai.gemini_analyzer import generate_search_strategy

logger = logging.getLogger(__name__)

DEFAULT_SEARCH_STRATEGY = {
    "sources": [
        {"name": "DOU", "category": "Support", "keywords": ["technical support", "support specialist"]},
        {"name": "Djinni", "category": "sysadmin", "keywords": ["sysadmin", "devops", "support"]},
    ],
    "fallback_keywords": ["technical support", "support specialist", "service desk"],
    "search_terms": ["technical support", "support specialist"],
}

ALLOWED_DOU_CATEGORIES = {"Project Manager", "Support", "SysAdmin", "DevOps", "QA", ""}
ALLOWED_DJINNI_CATEGORIES = {"project-manager", "sysadmin", "devops", "qa", "support", ""}


def _normalize_dou_category(category: str) -> str:
    cleaned = category.strip()
    if cleaned in ALLOWED_DOU_CATEGORIES:
        return cleaned
    mapping = {
        "project manager": "Project Manager",
        "project-manager": "Project Manager",
        "technical support": "Support",
        "support": "Support",
        "sysadmin": "SysAdmin",
        "devops": "DevOps",
        "qa": "QA",
        "quality assurance": "QA",
    }
    return mapping.get(cleaned.lower(), "")


def _normalize_djinni_category(category: str) -> str:
    cleaned = category.strip()
    if cleaned in ALLOWED_DJINNI_CATEGORIES:
        return cleaned
    mapping = {
        "project manager": "project-manager",
        "project-manager": "project-manager",
        "technical support": "support",
        "support": "support",
        "sysadmin": "sysadmin",
        "devops": "devops",
        "qa": "qa",
        "quality assurance": "qa",
    }
    return mapping.get(cleaned.lower(), "")


def normalize_search_strategy(payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return DEFAULT_SEARCH_STRATEGY.copy()

    sources = payload.get("sources") or DEFAULT_SEARCH_STRATEGY["sources"]
    normalized_sources: List[Dict[str, Any]] = []
    for source in sources:
        if not isinstance(source, dict):
            continue
        source_name = str(source.get("name") or "DOU")
        category = str(source.get("category") or "")
        if source_name.upper() == "DOU":
            category = _normalize_dou_category(category)
        elif source_name.upper() == "DJINNI":
            category = _normalize_djinni_category(category)

        normalized_sources.append(
            {
                "name": source_name,
                "category": category,
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

IMPORTANT RULES:
- For DOU, category MUST be one of: Project Manager, Support, SysAdmin, DevOps, QA, or empty string.
- For Djinni, category MUST be one of: project-manager, sysadmin, devops, qa, support, or empty string.
- Do NOT invent arbitrary English names like Job Board, Career, IT Management, or similar.
- If the user intent is not clear, use empty string for the category and rely on keywords.
- For Upwork, use the source name "Upwork" and leave category empty unless a known category is explicitly requested.

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
            normalized_category = _normalize_dou_category(category)
            if normalized_category:
                encoded = normalized_category.replace(" ", "+")
                urls.append({"source": name, "url": f"https://jobs.dou.ua/vacancies/feeds/?category={encoded}"})
            else:
                urls.append({"source": name, "url": "https://jobs.dou.ua/vacancies/feeds/?category=Support"})
        elif name == "DJINNI":
            normalized_category = _normalize_djinni_category(category)
            if normalized_category:
                urls.append({"source": name, "url": f"https://djinni.co/jobs/rss/?category={normalized_category}"})
            else:
                urls.append({"source": name, "url": "https://djinni.co/jobs/rss/?category=sysadmin"})
        elif name == "UPWORK":
            urls.append({"source": name, "url": "https://www.upwork.com/ab/feed/jobs/rss"})
        else:
            urls.append({"source": name, "url": ""})

    if not urls:
        urls = [{"source": "DOU", "url": "https://jobs.dou.ua/vacancies/feeds/?category=Support"}]
    return urls
