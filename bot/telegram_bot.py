import asyncio
import logging
import os
from typing import Any, Dict, Optional

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart

try:
    from aiogram.client.default import DefaultBotProperties
except ImportError:  # pragma: no cover - compatibility for older aiogram versions
    DefaultBotProperties = None
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ai.gemini_analyzer import JobAnalysis, build_analysis_prompt, analyze_job_with_fallback
from database.models import (
    get_all_users_with_settings,
    get_recent_user_job_context,
    get_user_liked_jobs,
    init_db,
    remove_from_liked,
    save_or_update_user_job_match,
    upsert_user_and_settings,
)
from parsers.filters import check_hard_filters
from parsers.manager import process_jobs

logger = logging.getLogger(__name__)


class SettingsStates(StatesGroup):
    waiting_for_keywords = State()
    waiting_for_stop_words = State()
    waiting_for_min_salary = State()
    waiting_for_bio_prompt = State()


class SmartJobMatcherBot:
    def __init__(self, token: str) -> None:
        if DefaultBotProperties is not None:
            self.bot = Bot(token=token, default=DefaultBotProperties(parse_mode="HTML"))
        else:
            self.bot = Bot(token=token, parse_mode="HTML")
        self.dp = Dispatcher()
        self._register_handlers()

    def _register_handlers(self) -> None:
        self.dp.message(CommandStart())(self.start_command)
        self.dp.message(Command("settings"))(self.settings_command)
        self.dp.message(Command("saved"))(self.saved_command)
        self.dp.callback_query(F.data == "settings:keywords")(self.ask_keywords)
        self.dp.callback_query(F.data == "settings:stop_words")(self.ask_stop_words)
        self.dp.callback_query(F.data == "settings:min_salary")(self.ask_min_salary)
        self.dp.callback_query(F.data == "settings:bio_prompt")(self.ask_bio_prompt)
        self.dp.callback_query(F.data.startswith("page:"))(self.handle_page)
        self.dp.callback_query(F.data.startswith("remove:"))(self.handle_remove)
        self.dp.callback_query(F.data.startswith("open:"))(self.handle_open)
        self.dp.callback_query(F.data.startswith("like:"))(self.handle_like)
        self.dp.callback_query(F.data.startswith("dislike:"))(self.handle_dislike)
        self.dp.message(SettingsStates.waiting_for_keywords)(self.process_keywords)
        self.dp.message(SettingsStates.waiting_for_stop_words)(self.process_stop_words)
        self.dp.message(SettingsStates.waiting_for_min_salary)(self.process_min_salary)
        self.dp.message(SettingsStates.waiting_for_bio_prompt)(self.process_bio_prompt)

    async def start_command(self, message: types.Message) -> None:
        user_id = message.from_user.id
        await upsert_user_and_settings(user_id=user_id, username=message.from_user.username)
        await message.answer(
            "Привіт! Я Smart Job Matcher Bot. Я допоможу знаходити підходящі вакансії та попереджати про ризики скаму."
        )

    async def settings_command(self, message: types.Message) -> None:
        builder = InlineKeyboardBuilder()
        builder.button(text="Keywords", callback_data="settings:keywords")
        builder.button(text="Stop words", callback_data="settings:stop_words")
        builder.button(text="Min salary", callback_data="settings:min_salary")
        builder.button(text="Bio prompt", callback_data="settings:bio_prompt")
        builder.adjust(2)
        await message.answer("Оберіть налаштування:", reply_markup=builder.as_markup())

    async def saved_command(self, message: types.Message) -> None:
        await self._show_saved_jobs(message, message.from_user.id, page=1)

    async def _show_saved_jobs(self, message: types.Message | types.CallbackQuery, user_id: int, page: int = 1, per_page: int = 5) -> None:
        liked_jobs = await get_user_liked_jobs(user_id=user_id, limit=1000, offset=0)
        total_pages = max(1, (len(liked_jobs) + per_page - 1) // per_page)
        page = max(1, min(page, total_pages))
        start = (page - 1) * per_page
        end = start + per_page
        page_jobs = liked_jobs[start:end]

        if not page_jobs:
            text = "У вас поки немає збережених вакансій."
            markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔄 Оновити", callback_data=f"page:{page}")]])
            if isinstance(message, types.CallbackQuery):
                await message.message.edit_text(text, reply_markup=markup)
            else:
                await message.answer(text, reply_markup=markup)
            return

        job = page_jobs[0]
        text = (
            f"<b>Обране</b>\n\n"
            f"• {job['title']}\n"
            f"• Додано: {job['updated_at']}"
        )
        builder = InlineKeyboardBuilder()
        if page > 1:
            builder.button(text="◀️ Назад", callback_data=f"page:{page - 1}")
        else:
            builder.button(text="◀️ Назад", callback_data="page:1")
        builder.button(text=f"{page} / {total_pages}", callback_data="noop")
        builder.button(text="Вперед ▶️", callback_data=f"page:{page + 1 if page < total_pages else total_pages}")
        builder.button(text="🔗 Відкрити вакансію", callback_data=f"open:{job['job_id']}")
        builder.button(text="❌ Видалити з обраного", callback_data=f"remove:{job['job_id']}")
        builder.adjust(3, 2)
        markup = builder.as_markup()

        if isinstance(message, types.CallbackQuery):
            await message.message.edit_text(text, reply_markup=markup)
        else:
            await message.answer(text, reply_markup=markup)

    async def handle_page(self, callback: types.CallbackQuery) -> None:
        page = int(callback.data.split(":", 1)[1])
        await self._show_saved_jobs(callback, callback.from_user.id, page=page)
        await callback.answer()

    async def handle_remove(self, callback: types.CallbackQuery) -> None:
        _, job_id = callback.data.split(":", 1)
        await remove_from_liked(callback.from_user.id, job_id)
        await self._show_saved_jobs(callback, callback.from_user.id, page=1)
        await callback.answer("Видалено з обраного")

    async def handle_open(self, callback: types.CallbackQuery) -> None:
        _, job_id = callback.data.split(":", 1)
        await callback.answer(f"Відкриваємо вакансію {job_id}")

    async def handle_like(self, callback: types.CallbackQuery) -> None:
        _, job_id = callback.data.split(":", 1)
        await save_or_update_user_job_match(callback.from_user.id, job_id, status="liked")
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer("Позначено як цікаве")

    async def handle_dislike(self, callback: types.CallbackQuery) -> None:
        _, job_id = callback.data.split(":", 1)
        await save_or_update_user_job_match(callback.from_user.id, job_id, status="disliked")
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer("Позначено як нецікаве")

    async def ask_keywords(self, callback: types.CallbackQuery, state: FSMContext) -> None:
        await callback.message.answer("Надішліть keywords через кому, наприклад: python, backend")
        await state.set_state(SettingsStates.waiting_for_keywords)
        await callback.answer()

    async def ask_stop_words(self, callback: types.CallbackQuery, state: FSMContext) -> None:
        await callback.message.answer("Надішліть stop_words через кому")
        await state.set_state(SettingsStates.waiting_for_stop_words)
        await callback.answer()

    async def ask_min_salary(self, callback: types.CallbackQuery, state: FSMContext) -> None:
        await callback.message.answer("Надішліть мінімальний бюджет/ЗП у USD")
        await state.set_state(SettingsStates.waiting_for_min_salary)
        await callback.answer()

    async def ask_bio_prompt(self, callback: types.CallbackQuery, state: FSMContext) -> None:
        await callback.message.answer("Надішліть bio prompt або короткий опис профілю")
        await state.set_state(SettingsStates.waiting_for_bio_prompt)
        await callback.answer()

    async def process_keywords(self, message: types.Message, state: FSMContext) -> None:
        values = [item.strip() for item in message.text.split(",") if item.strip()]
        await self._update_settings(message.from_user.id, keywords=values)
        await message.answer("Keywords оновлено")
        await state.clear()

    async def process_stop_words(self, message: types.Message, state: FSMContext) -> None:
        values = [item.strip() for item in message.text.split(",") if item.strip()]
        await self._update_settings(message.from_user.id, stop_words=values)
        await message.answer("Stop words оновлено")
        await state.clear()

    async def process_min_salary(self, message: types.Message, state: FSMContext) -> None:
        try:
            value = int(message.text)
        except ValueError:
            await message.answer("Будь ласка, надішліть число")
            return
        await self._update_settings(message.from_user.id, min_salary=value)
        await message.answer("Min salary оновлено")
        await state.clear()

    async def process_bio_prompt(self, message: types.Message, state: FSMContext) -> None:
        await self._update_settings(message.from_user.id, bio_prompt=message.text)
        await message.answer("Bio prompt оновлено")
        await state.clear()

    async def _update_settings(self, user_id: int, **kwargs: Any) -> None:
        existing = await self._get_user_settings(user_id)
        await upsert_user_and_settings(
            user_id=user_id,
            username=existing.get("username"),
            keywords=kwargs.get("keywords", existing.get("keywords") or []),
            stop_words=kwargs.get("stop_words", existing.get("stop_words") or []),
            min_salary=kwargs.get("min_salary", existing.get("min_salary")),
            bio_prompt=kwargs.get("bio_prompt", existing.get("bio_prompt")),
            risk_sensitivity=existing.get("risk_sensitivity"),
        )

    async def _get_user_settings(self, user_id: int) -> Dict[str, Any]:
        users = await get_all_users_with_settings()
        for user in users:
            if user["user_id"] == user_id:
                return user
        return {}

    async def run(self) -> None:
        await init_db()
        await self.bot.delete_webhook(drop_pending_updates=True)
        await self.bot.set_my_commands(
            [
                types.BotCommand(command="start", description="Реєстрація"),
                types.BotCommand(command="settings", description="Налаштування"),
                types.BotCommand(command="saved", description="Обране"),
            ]
        )
        await self.dp.start_polling(self.bot)

    async def stop(self) -> None:
        try:
            await self.dp.stop_polling()
        except Exception:
            pass
        await self.bot.close()


async def build_job_card(job: Dict[str, Any], analysis: Optional[JobAnalysis]) -> str:
    fit_score = analysis.fit_score if analysis else 0
    risk_score = analysis.risk_score if analysis else 0
    risk_status = "⚠️ ВИСОКИЙ РИЗИК" if risk_score > 40 else "✅ Безпечно"
    bullets = (analysis.summary_bullets if analysis else ["Немає даних", "Немає даних", "Немає даних"])[:2]
    risk_warnings = ", ".join(analysis.risk_warnings if analysis else []) or "Немає"
    return (
        f"📌 <b><a href=\"{job.get('url', '')}\">{job.get('title', '')}</a></b>\n"
        f"🏛 Джерело: {job.get('source', '')} | 💰 ЗП: {job.get('salary_info') or 'Н/Д'}\n\n"
        f"📊 <b>Відповідність:</b> {fit_score}/10\n"
        f"🛡 <b>Ризик скаму:</b> {risk_score}% {risk_status}\n\n"
        f"📝 <b>Короткий опис:</b>\n"
        f"• {bullets[0]}\n"
        f"• {bullets[1]}\n\n"
        f"💡 <b>Чому підходить:</b> {analysis.match_reason if analysis else 'Немає даних'}\n"
        f"⚠️ <b>Зауваження/Ризики:</b> {risk_warnings}"
    )


async def build_job_markup(job: Dict[str, Any]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="👍 Цікаво", callback_data=f"like:{job['job_id']}")
    builder.button(text="👎 Ні", callback_data=f"dislike:{job['job_id']}")
    builder.button(text="🔗 Відкрити", url=job.get("url") or "https://example.com")
    builder.adjust(3)
    return builder.as_markup()


async def send_job_to_user(bot: Bot, user_id: int, job: Dict[str, Any], analysis: Optional[JobAnalysis]) -> None:
    text = await build_job_card(job, analysis)
    markup = await build_job_markup(job)
    await bot.send_message(user_id, text, reply_markup=markup)


NOTIFICATION_QUEUE: list[Dict[str, Any]] = []


async def enqueue_notification(user_id: int, job: Dict[str, Any], analysis: Optional[JobAnalysis]) -> None:
    NOTIFICATION_QUEUE.append({"user_id": user_id, "job": job, "analysis": analysis})


async def process_notification_queue(bot: Bot) -> None:
    while NOTIFICATION_QUEUE:
        item = NOTIFICATION_QUEUE.pop(0)
        await send_job_to_user(bot, item["user_id"], item["job"], item["analysis"])
        await asyncio.sleep(2.5)


async def notify_new_jobs() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        return

    if DefaultBotProperties is not None:
        bot = Bot(token=token, default=DefaultBotProperties(parse_mode="HTML"))
    else:
        bot = Bot(token=token, parse_mode="HTML")
    users = await get_all_users_with_settings()
    jobs = await process_jobs()
    for user in users:
        user_id = user["user_id"]
        for job in jobs:
            if not check_hard_filters(
                job,
                {
                    "keywords": user.get("keywords") or [],
                    "stop_words": user.get("stop_words") or [],
                    "min_salary": user.get("min_salary"),
                },
            ):
                continue
            few_shot_context = await get_recent_user_job_context(user_id)
            prompt = await build_analysis_prompt(job, user, few_shot_context)
            analysis = await analyze_job_with_fallback(prompt, JobAnalysis)
            if analysis and analysis.should_notify:
                await enqueue_notification(user_id, job, analysis)
    await process_notification_queue(bot)
    await bot.close()
