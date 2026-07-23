import asyncio
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

from config import TELEGRAM_BOT_TOKEN

try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
except ImportError:  # pragma: no cover - optional dependency
    AsyncIOScheduler = None

from bot.telegram_bot import SmartJobMatcherBot, notify_new_jobs

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path in {"/", "/healthz", "/health"}:
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"ok")
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A003
        return


def start_health_server() -> tuple[ThreadingHTTPServer, threading.Thread]:
    port = int(os.getenv("PORT", "10000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


async def send_keepalive(url: str) -> None:
    if not url:
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.get(url, headers={"User-Agent": "render-keepalive"})
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Keepalive request failed: %s", exc)


async def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN)
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")

    bot = SmartJobMatcherBot(token)
    health_server, health_thread = start_health_server()
    scheduler = None
    if AsyncIOScheduler is not None:
        scheduler = AsyncIOScheduler()
        scheduler.add_job(notify_new_jobs, "interval", minutes=15)

        keepalive_url = os.getenv("KEEPALIVE_URL") or os.getenv("RENDER_EXTERNAL_URL") or ""
        if keepalive_url:
            interval_minutes = max(5, int(os.getenv("KEEPALIVE_INTERVAL_MINUTES", "10")))
            scheduler.add_job(send_keepalive, "interval", minutes=interval_minutes, args=[keepalive_url])
            await send_keepalive(keepalive_url)

        scheduler.start()

    try:
        await bot.run()
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received")
    finally:
        if scheduler is not None:
            scheduler.shutdown(wait=False)
        try:
            health_server.shutdown()
            health_server.server_close()
            health_thread.join(timeout=3)
        except Exception:
            pass
        await bot.stop()
        logger.info("Bot stopped gracefully")


if __name__ == "__main__":
    asyncio.run(main())
