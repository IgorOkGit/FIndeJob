import asyncio
import json
import logging
import os
from typing import Any, Optional

from pydantic import BaseModel, Field, ValidationError

from ai.prompts import SYSTEM_PROMPT

try:
    from google import genai as google_genai
except ImportError:  # pragma: no cover - optional dependency
    google_genai = None

try:
    from google.genai import Client as GoogleGenAIClient
except ImportError:  # pragma: no cover - optional dependency
    GoogleGenAIClient = None


logger = logging.getLogger(__name__)

MODELS_CASCADE = [
    "gemini-2.5-flash-lite",
    "gemini-2.5-flash",
    "gemini-1.5-flash",
    "gemini-1.5-flash-8b",
]


class JobAnalysis(BaseModel):
    fit_score: int = Field(ge=1, le=10)
    summary_bullets: list[str] = Field(min_length=3, max_length=4)
    match_reason: str
    risk_score: int = Field(ge=0, le=100)
    risk_warnings: list[str]
    should_notify: bool


def get_client() -> Any:
    if google_genai is None and GoogleGenAIClient is None:
        raise RuntimeError("google-genai is not installed")

    api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY") or os.getenv("GO_API_KEY", "")
    if not api_key:
        raise RuntimeError("GOOGLE_API_KEY or GEMINI_API_KEY is not configured")

    if GoogleGenAIClient is not None:
        return GoogleGenAIClient(api_key=api_key)
    return google_genai.Client(api_key=api_key)


async def _call_model(client: Any, model_name: str, prompt: str, schema: Optional[type[BaseModel]] = None) -> Any:
    if schema is None:
        result = client.models.generate_content(model=model_name, contents=prompt)
        return result

    response_schema = schema.model_json_schema() if schema is not None else None
    response = client.models.generate_content(
        model=model_name,
        contents=prompt,
        config={
            "response_mime_type": "application/json",
            "response_schema": response_schema,
        },
    )
    return response


async def analyze_job_with_fallback(prompt: str, schema: Optional[type[BaseModel]] = None) -> Optional[JobAnalysis]:
    """Try the model cascade and fall back on rate limit errors."""
    if schema is None:
        schema = JobAnalysis

    client = get_client()

    last_error: Optional[Exception] = None
    for index, model_name in enumerate(MODELS_CASCADE):
        try:
            if index > 0:
                await asyncio.sleep(4)
            response = await asyncio.to_thread(_call_model, client, model_name, prompt, schema)
            if not hasattr(response, "text"):
                payload = response
            else:
                payload = response.text

            if isinstance(payload, str):
                data = json.loads(payload)
            else:
                data = payload

            if isinstance(data, dict):
                return JobAnalysis.model_validate(data)
            return JobAnalysis.model_validate(json.loads(json.dumps(data)))
        except (RuntimeError, ValidationError, json.JSONDecodeError) as exc:
            last_error = exc
            if "429" in str(exc) or "Too Many Requests" in str(exc) or "ResourceExhausted" in str(exc) or "503" in str(exc):
                logger.warning("[WARNING] Limit reached for %s. Switching to next model...", model_name)
                continue
            logger.exception("Model %s failed", model_name)
            break

    if last_error is not None:
        logger.error("All models exhausted or failed: %s", last_error)
    return None


async def build_analysis_prompt(job: dict[str, Any], user_settings: dict[str, Any], few_shot_context: Optional[dict[str, list[dict[str, Any]]]] = None) -> str:
    """Construct the prompt for Gemini analysis using the requested system prompt and few-shot examples."""
    bio_prompt = user_settings.get("bio_prompt") or ""
    keywords = ", ".join(user_settings.get("keywords") or [])
    stop_words = ", ".join(user_settings.get("stop_words") or [])
    few_shot_text = ""
    if few_shot_context:
        liked = few_shot_context.get("liked") or []
        disliked = few_shot_context.get("disliked") or []
        few_shot_text = "\n".join(
            [
                "Liked examples:",
                *[f"- {item.get('title', '')}: {item.get('summary', '')}" for item in liked[:5]],
                "Disliked examples:",
                *[f"- {item.get('title', '')}: {item.get('summary', '')}" for item in disliked[:5]],
            ]
        )

    return f"""
{SYSTEM_PROMPT}

User profile:
- Bio: {bio_prompt}
- Keywords: {keywords}
- Stop words: {stop_words}

Few-shot context:
{few_shot_text or 'No prior examples available.'}

Analyze this job posting:
Title: {job.get('title', '')}
Source: {job.get('source', '')}
URL: {job.get('url', '')}
Description:
{job.get('raw_text', '')}
Salary info: {job.get('salary_info') or 'Unknown'}
"""
