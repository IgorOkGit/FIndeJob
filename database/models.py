import json
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from config import DATABASE_URL

Base = declarative_base()


class User(Base):
    __tablename__ = "users"

    user_id = Column(Integer, primary_key=True)
    username = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class UserSetting(Base):
    __tablename__ = "user_settings"

    user_id = Column(Integer, ForeignKey("users.user_id", ondelete="CASCADE"), primary_key=True)
    keywords = Column(Text, nullable=True)
    stop_words = Column(Text, nullable=True)
    min_salary = Column(Integer, nullable=True)
    bio_prompt = Column(Text, nullable=True)
    preferences = Column(Text, nullable=True)
    risk_sensitivity = Column(String(50), nullable=True)


class Job(Base):
    __tablename__ = "jobs"

    job_id = Column(String(255), primary_key=True)
    source = Column(String(255), nullable=True)
    title = Column(Text, nullable=True)
    url = Column(Text, nullable=True)
    raw_text = Column(Text, nullable=True)
    salary_info = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class UserJobMatch(Base):
    __tablename__ = "user_job_matches"

    user_id = Column(Integer, ForeignKey("users.user_id", ondelete="CASCADE"), primary_key=True)
    job_id = Column(String(255), ForeignKey("jobs.job_id", ondelete="CASCADE"), primary_key=True)
    ai_score = Column(Integer, nullable=True)
    risk_score = Column(Integer, nullable=True)
    summary = Column(Text, nullable=True)
    status = Column(String(50), nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)


def _build_engine_config() -> tuple[str, dict[str, Any]]:
    url = (DATABASE_URL or "postgresql+asyncpg://postgres:postgres@localhost:5432/postgres").strip()
    connect_args: dict[str, Any] = {}

    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+asyncpg://", 1)
    if url.startswith("postgresql://") and "+asyncpg" not in url:
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)

    if any(token in url for token in ("sslmode=require", "ssl=require", "ssl=true")):
        connect_args["ssl"] = True
        parsed = urlparse(url)
        query_items = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key not in {"sslmode", "ssl"}]
        url = urlunparse(parsed._replace(query=urlencode(query_items)))

    return url, connect_args


ENGINE_URL, ENGINE_CONNECT_ARGS = _build_engine_config()
engine = create_async_engine(ENGINE_URL, echo=False, connect_args=ENGINE_CONNECT_ARGS)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db() -> None:
    """Create all required tables if they do not exist."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS preferences TEXT"))


async def upsert_user_and_settings(
    user_id: int,
    username: Optional[str] = None,
    keywords: Optional[List[str]] = None,
    stop_words: Optional[List[str]] = None,
    min_salary: Optional[int] = None,
    bio_prompt: Optional[str] = None,
    preferences: Optional[str] = None,
    risk_sensitivity: Optional[str] = None,
) -> None:
    """Insert or update a user and their settings in a single transaction."""
    async with AsyncSessionLocal() as session:
        async with session.begin():
            user = await session.get(User, user_id)
            if user is None:
                session.add(User(user_id=user_id, username=username, created_at=datetime.utcnow()))
            elif username is not None:
                user.username = username

            settings = await session.get(UserSetting, user_id)
            if settings is None:
                session.add(
                    UserSetting(
                        user_id=user_id,
                        keywords=json.dumps(keywords or []),
                        stop_words=json.dumps(stop_words or []),
                        min_salary=min_salary,
                        bio_prompt=bio_prompt,
                        preferences=preferences,
                        risk_sensitivity=risk_sensitivity,
                    )
                )
            else:
                settings.keywords = json.dumps(keywords or []) if keywords is not None else settings.keywords
                settings.stop_words = json.dumps(stop_words or []) if stop_words is not None else settings.stop_words
                settings.min_salary = min_salary if min_salary is not None else settings.min_salary
                settings.bio_prompt = bio_prompt if bio_prompt is not None else settings.bio_prompt
                settings.preferences = preferences if preferences is not None else settings.preferences
                settings.risk_sensitivity = risk_sensitivity if risk_sensitivity is not None else settings.risk_sensitivity


async def get_all_users_with_settings() -> List[Dict[str, Any]]:
    """Return all users together with their settings for multi-user processing."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            """
            SELECT u.user_id, u.username, s.keywords, s.stop_words, s.min_salary,
                   s.bio_prompt, s.preferences, s.risk_sensitivity
            FROM users AS u
            LEFT JOIN user_settings AS s ON s.user_id = u.user_id
            ORDER BY u.user_id
            """
        )
        rows = result.fetchall()

    return [
        {
            "user_id": row[0],
            "username": row[1],
            "keywords": json.loads(row[2]) if row[2] else [],
            "stop_words": json.loads(row[3]) if row[3] else [],
            "min_salary": row[4],
            "bio_prompt": row[5],
            "preferences": row[6],
            "risk_sensitivity": row[7],
        }
        for row in rows
    ]


async def save_job(
    job_id: str,
    source: str,
    title: str,
    url: str,
    raw_text: str,
    salary_info: Optional[str] = None,
) -> None:
    """Insert a job only if it does not already exist."""
    async with AsyncSessionLocal() as session:
        async with session.begin():
            existing = await session.get(Job, job_id)
            if existing is not None:
                return
            session.add(
                Job(
                    job_id=job_id,
                    source=source,
                    title=title,
                    url=url,
                    raw_text=raw_text,
                    salary_info=salary_info,
                    created_at=datetime.utcnow(),
                )
            )


async def save_or_update_user_job_match(
    user_id: int,
    job_id: str,
    ai_score: Optional[int] = None,
    risk_score: Optional[int] = None,
    summary: Optional[str] = None,
    status: Optional[str] = None,
) -> None:
    """Insert or update a user's match evaluation for a specific job."""
    async with AsyncSessionLocal() as session:
        async with session.begin():
            match = await session.get(UserJobMatch, (user_id, job_id))
            if match is None:
                match = UserJobMatch(
                    user_id=user_id,
                    job_id=job_id,
                    ai_score=ai_score,
                    risk_score=risk_score,
                    summary=summary,
                    status=status,
                    updated_at=datetime.utcnow(),
                )
                session.add(match)
            else:
                if ai_score is not None:
                    match.ai_score = ai_score
                if risk_score is not None:
                    match.risk_score = risk_score
                if summary is not None:
                    match.summary = summary
                if status is not None:
                    match.status = status
                match.updated_at = datetime.utcnow()


async def get_recent_user_job_context(user_id: int, limit: int = 5) -> Dict[str, List[Dict[str, Any]]]:
    """Return the most recent liked and disliked jobs for few-shot context."""
    async with AsyncSessionLocal() as session:
        liked_result = await session.execute(
            """
            SELECT j.job_id, j.source, j.title, j.url, uj.summary, uj.status, uj.updated_at
            FROM user_job_matches AS uj
            JOIN jobs AS j ON j.job_id = uj.job_id
            WHERE uj.user_id = :user_id AND uj.status = 'liked'
            ORDER BY uj.updated_at DESC
            LIMIT :limit
            """,
            {"user_id": user_id, "limit": limit},
        )
        liked_rows = liked_result.fetchall()

        disliked_result = await session.execute(
            """
            SELECT j.job_id, j.source, j.title, j.url, uj.summary, uj.status, uj.updated_at
            FROM user_job_matches AS uj
            JOIN jobs AS j ON j.job_id = uj.job_id
            WHERE uj.user_id = :user_id AND uj.status = 'disliked'
            ORDER BY uj.updated_at DESC
            LIMIT :limit
            """,
            {"user_id": user_id, "limit": limit},
        )
        disliked_rows = disliked_result.fetchall()

    liked = [
        {
            "job_id": row[0],
            "source": row[1],
            "title": row[2],
            "url": row[3],
            "summary": row[4],
            "status": row[5],
            "updated_at": row[6],
        }
        for row in liked_rows
    ]
    disliked = [
        {
            "job_id": row[0],
            "source": row[1],
            "title": row[2],
            "url": row[3],
            "summary": row[4],
            "status": row[5],
            "updated_at": row[6],
        }
        for row in disliked_rows
    ]
    return {"liked": liked, "disliked": disliked}


async def get_user_liked_jobs(user_id: int, limit: int = 10, offset: int = 0) -> List[Dict[str, Any]]:
    """Return liked jobs for a user with pagination."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            """
            SELECT j.job_id, j.source, j.title, j.url, uj.ai_score, uj.risk_score, uj.summary, uj.status, uj.updated_at
            FROM user_job_matches AS uj
            JOIN jobs AS j ON j.job_id = uj.job_id
            WHERE uj.user_id = :user_id AND uj.status = 'liked'
            ORDER BY uj.updated_at DESC
            LIMIT :limit OFFSET :offset
            """,
            {"user_id": user_id, "limit": limit, "offset": offset},
        )
        rows = result.fetchall()

    return [
        {
            "job_id": row[0],
            "source": row[1],
            "title": row[2],
            "url": row[3],
            "ai_score": row[4],
            "risk_score": row[5],
            "summary": row[6],
            "status": row[7],
            "updated_at": row[8],
        }
        for row in rows
    ]


async def remove_from_liked(user_id: int, job_id: str) -> None:
    """Remove a job from liked state by updating its status to 'disliked' or 'pending'."""
    async with AsyncSessionLocal() as session:
        async with session.begin():
            match = await session.get(UserJobMatch, (user_id, job_id))
            if match is not None and match.status == "liked":
                match.status = "pending"
                match.updated_at = datetime.utcnow()
