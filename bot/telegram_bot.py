import asyncio
import logging
import os
from typing import Any, Dict, Optional

from aiogram import Bot, Dispatcher, F, types
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramServerError
from aiogram.filters import Command, CommandStart
from aiogram.types import BotCommand

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
    waiting_for_preferences = State()
    waiting_for_freeform_prompt = State()


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
        self.dp.message(Command("search"))(self.search_command)
        self.dp.callback_query(F.data == "settings:keywords")(self.ask_keywords)
        self.dp.callback_query(F.data == "settings:stop_words")(self.ask_stop_words)
        self.dp.callback_query(F.data == "settings:min_salary")(self.ask_min_salary)
        self.dp.callback_query(F.data == "settings:bio_prompt")(self.ask_bio_prompt)
        self.dp.callback_query(F.data == "settings:preferences")(self.ask_preferences)
        self.dp.callback_query(F.data == "settings:preferences_remove")(self.clear_preferences)
        self.dp.callback_query(F.data == "search:refresh")(self.refresh_search)
        self.dp.message(SettingsStates.waiting_for_freeform_prompt)(self.process_freeform_prompt)
        self.dp.callback_query(F.data.startswith("page:"))(self.handle_page)
        self.dp.callback_query(F.data.startswith("remove:"))(self.handle_remove)
        self.dp.callback_query(F.data.startswith("open:"))(self.handle_open)
        self.dp.callback_query(F.data.startswith("like:"))(self.handle_like)
        self.dp.callback_query(F.data.startswith("dislike:"))(self.handle_dislike)
        self.dp.message(SettingsStates.waiting_for_keywords)(self.process_keywords)
        self.dp.message(SettingsStates.waiting_for_stop_words)(self.process_stop_words)
        self.dp.message(SettingsStates.waiting_for_min_salary)(self.process_min_salary)
        self.dp.message(SettingsStates.waiting_for_bio_prompt)(self.process_bio_prompt)
        self.dp.message(SettingsStates.waiting_for_preferences)(self.process_preferences)

    async def start_command(self, message: types.Message, state: FSMContext) -> None:
        user_id = message.from_user.id
        await upsert_user_and_settings(user_id=user_id, username=message.from_user.username)
        settings = await self._get_user_settings(user_id)
        await message.answer(
            "Привіт! Я Smart Job Matcher Bot. Я допоможу знаходити підходящі вакансії та попереджати про ризики скаму."
        )
        if not (settings.get("preferences") or "").strip():
            await message.answer(
                "Щоб Gemini підбирало вакансії точніше, напишіть свої побажання. Наприклад: «Шукаю backend вакансії в Україні, з salary від 3000 USD, без розсилок і без повної зайнятості»."
            )
            await state.set_state(SettingsStates.waiting_for_preferences)
        else:
            await message.answer("Мої побажання вже збережено. Можеш писати /search або змінити їх у /settings.")

    async def settings_command(self, message: types.Message) -> None:
        builder = InlineKeyboardBuilder()
        builder.button(text="Keywords", callback_data="settings:keywords")
        builder.button(text="Stop words", callback_data="settings:stop_words")
        builder.button(text="Min salary", callback_data="settings:min_salary")
        builder.button(text="Bio prompt", callback_data="settings:bio_prompt")
        builder.button(text="Preferences", callback_data="settings:preferences")
        builder.button(text="Delete preferences", callback_data="settings:preferences_remove")
        builder.adjust(2)
        await message.answer("Оберіть налаштування:", reply_markup=builder.as_markup())

    async def process_freeform_prompt(self, message: types.Message, state: FSMContext) -> None:
        if not message.text or not message.text.strip():
            await message.answer("Будь ласка, напишіть текст побажань.")
            return
        await self._update_settings(message.from_user.id, preferences=message.text.strip())
        await message.answer(
            "✅ Побажання збережено. Тепер я зможу використовувати їх для підбору вакансій."
        )
        await state.clear()

    async def saved_command(self, message: types.Message) -> None:
        await self._show_saved_jobs(message, message.from_user.id, page=1)

    async def search_command(self, message: types.Message) -> None:
        await self._send_search_results(message, user_id=message.from_user.id)

    async def refresh_search(self, callback: types.CallbackQuery) -> None:
        await self._send_search_results(callback, user_id=callback.from_user.id)
        await callback.answer("Пошук запущено заново")

    async def _send_search_results(self, message: types.Message | types.CallbackQuery, user_id: int) -> None:
        logger.info("Starting dynamic search for user %s", user_id)
        jobs = await process_jobs(user_id=user_id)
        logger.info("Search for user %s returned %d raw jobs", user_id, len(jobs))
        user_settings = await self._get_user_settings(user_id)
        few_shot_context = await get_recent_user_job_context(user_id)

        matched: list[tuple[Dict[str, Any], Optional[JobAnalysis]]] = []
        for job in jobs:
            prompt = await build_analysis_prompt(job, user_settings, few_shot_context)
            analysis = await analyze_job_with_fallback(prompt, JobAnalysis)
            logger.info(
                "Analyzed job for user %s: %s | fit=%s | risk=%s",
                user_id,
                job.get("title"),
                getattr(analysis, "fit_score", None),
                getattr(analysis, "risk_score", None),
            )
            matched.append((job, analysis))

        matched.sort(
            key=lambda item: (
                item[1].fit_score if item[1] else 0,
                -(item[1].risk_score if item[1] else 100),
            ),
            reverse=True,
        )

        top_jobs = matched[:10]
        if not top_jobs:
            text = "🔎 Пошук завершено. Поки що немає підходящих вакансій."
            markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔄 Шукати ще", callback_data="search:refresh")]])
            if isinstance(message, types.CallbackQuery):
                try:
                    await message.message.edit_text(text, reply_markup=markup)
                except TelegramBadRequest as exc:
                    if "message is not modified" in str(exc).lower():
                        await message.answer("Нових вакансій поки немає")
                        return
                    logger.warning("Unable to edit search message for user %s: %s", user_id, exc)
                    await message.message.answer(text, reply_markup=markup)
            else:
                await message.answer(text, reply_markup=markup)
            return

        lines = ["🔎 <b>Результати пошуку</b>", ""]
        for index, (job, analysis) in enumerate(top_jobs, start=1):
            fit_score = analysis.fit_score if analysis else 0
            risk_score = analysis.risk_score if analysis else 0
            lines.append(
                f"{index}. <b>{job.get('title', 'Без назви')}</b>\n"
                f"   • Джерело: {job.get('source', 'N/A')}\n"
                f"   • ЗП: {job.get('salary_info') or 'Н/Д'}\n"
                f"   • Fit: {fit_score}/10 | Risk: {risk_score}%\n"
                f"   • <a href=\"{job.get('url', '')}\">Відкрити</a>"
            )

        text = "\n\n".join(lines)
        builder = InlineKeyboardBuilder()
        builder.button(text="🔄 Шукати ще", callback_data="search:refresh")
        markup = builder.as_markup()

        if isinstance(message, types.CallbackQuery):
            try:
                await message.message.edit_text(text, reply_markup=markup)
            except TelegramBadRequest as exc:
                if "message is not modified" in str(exc).lower():
                    await message.answer("Нових вакансій поки немає")
                    return
                logger.warning("Unable to edit search message for user %s: %s", user_id, exc)
                await message.message.answer(text, reply_markup=markup)
        else:
            await message.answer(text, reply_markup=markup)

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

    async def ask_preferences(self, callback: types.CallbackQuery, state: FSMContext) -> None:
        await callback.message.answer("Надішліть побажання для Gemini щодо вакансій")
        await state.set_state(SettingsStates.waiting_for_preferences)
        await callback.answer()

    async def clear_preferences(self, callback: types.CallbackQuery) -> None:
        await self._update_settings(callback.from_user.id, preferences="")
        await callback.message.answer("Побажання видалено")
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

    async def process_preferences(self, message: types.Message, state: FSMContext) -> None:
        await self._update_settings(message.from_user.id, preferences=message.text or "")
        await message.answer("Побажання для Gemini збережено")
        await state.clear()

    async def _update_settings(self, user_id: int, **kwargs: Any) -> None:
        existing = await self._get_user_settings(user_id)
        preferences = kwargs.get("preferences")
        if preferences is None:
            preferences = existing.get("preferences")

        await upsert_user_and_settings(
            user_id=user_id,
            username=existing.get("username"),
            keywords=kwargs.get("keywords", existing.get("keywords") or []),
            stop_words=kwargs.get("stop_words", existing.get("stop_words") or []),
            min_salary=kwargs.get("min_salary", existing.get("min_salary")),
            bio_prompt=kwargs.get("bio_prompt", existing.get("bio_prompt")),
            preferences=preferences,
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
        for attempt in range(3):
            try:
                await self.bot.delete_webhook(drop_pending_updates=True)
                break
            except (TelegramServerError, TelegramNetworkError) as exc:
                if attempt == 2:
                    logger.warning("Webhook cleanup failed after retries: %s", exc)
                    break
                logger.warning("Webhook cleanup failed, retrying (%s/3): %s", attempt + 1, exc)
                await asyncio.sleep(2)

        await self.bot.set_my_commands(
            [
                BotCommand(command="start", description="Реєстрація"),
                BotCommand(command="settings", description="Налаштування"),
                BotCommand(command="saved", description="Обране"),
                BotCommand(command="search", description="Пошук вакансій"),
            ]
        )
        await self.dp.start_polling(self.bot)

    async def stop(self) -> None:
        try:
            await self.dp.stop_polling()
        except Exception:
            pass
        try:
            if hasattr(self.bot, "session") and self.bot.session is not None:
                await self.bot.session.close()
        except Exception:
            pass
        try:
            await self.bot.close()
        except Exception:
            pass


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
