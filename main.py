import asyncio
import logging
import os

from config import TELEGRAM_BOT_TOKEN

try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
except ImportError:  # pragma: no cover - optional dependency
    AsyncIOScheduler = None

from bot.telegram_bot import SmartJobMatcherBot, notify_new_jobs

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


async def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN)
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")

    bot = SmartJobMatcherBot(token)
    scheduler = None
    if AsyncIOScheduler is not None:
        scheduler = AsyncIOScheduler()
        scheduler.add_job(notify_new_jobs, "interval", minutes=15)
        scheduler.start()

    try:
        await bot.run()
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received")
    finally:
        if scheduler is not None:
            scheduler.shutdown(wait=False)
        await bot.stop()
        logger.info("Bot stopped gracefully")


if __name__ == "__main__":
    asyncio.run(main())
