"""Composition root: builds every component and runs the bot."""
from __future__ import annotations

import asyncio
import logging
import os

from aiohttp import web

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions

from .config import Settings
from .database import Database
from .fetcher import Fetcher
from .handlers import router
from .health import create_health_app
from .logging_setup import setup_logging
from .scheduler import PriceMonitor
from .service import TrackerService

log = logging.getLogger(__name__)

COMMANDS = [
    BotCommand(command="list", description="My tracked products"),
    BotCommand(command="target", description="Alert me only at my price"),
    BotCommand(command="remove", description="Stop tracking a product"),
    BotCommand(command="plan", description="My plan and limits"),
    BotCommand(command="premium", description="Track more, checked faster"),
    BotCommand(command="help", description="How to use this bot"),
]


def _make_notifier(bot: Bot):
    async def notify(
        user_id: int, text: str, buy_url: str | None = None, *, track_id: int | None = None
    ) -> None:
        keyboard = None
        if buy_url:
            rows = [[InlineKeyboardButton(text="🛒 Buy Now", url=buy_url)]]
            if track_id is not None:
                rows.append([InlineKeyboardButton(text="📦 View Product", callback_data=f"details:{track_id}:0")])
            keyboard = InlineKeyboardMarkup(inline_keyboard=rows)
        for attempt in range(2):
            try:
                await bot.send_message(
                    user_id, text, reply_markup=keyboard,
                    link_preview_options=LinkPreviewOptions(is_disabled=True)
                )
                return
            except TelegramRetryAfter as exc:
                await asyncio.sleep(exc.retry_after + 1)
            except TelegramForbiddenError:
                log.info("User %s blocked the bot; skipping alert", user_id)
                return

    return notify


async def run() -> None:
    settings = Settings.from_env()
    setup_logging(settings)  # console + rotating files, secrets redacted
    db = Database(settings.db_path)
    fetcher = Fetcher(settings)
    service = TrackerService(db, settings, fetcher.fetch_product)

    bot = Bot(token=settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    monitor = PriceMonitor(db, service, fetcher.fetch_product, _make_notifier(bot), settings)
    dp = Dispatcher(service=service, settings=settings, monitor=monitor)  # injected into handlers by name
    dp.include_router(router)

    # Render Web Services must bind an HTTP port before the deploy can be marked healthy.
    # The Telegram bot still uses long polling; this tiny server is only a health endpoint.
    port = int(os.getenv("PORT", "10000"))
    health_runner = web.AppRunner(create_health_app(), access_log=None)
    await health_runner.setup()
    health_site = web.TCPSite(health_runner, host="0.0.0.0", port=port)
    await health_site.start()
    log.info("Health server listening on 0.0.0.0:%s", port)

    monitor_task = asyncio.create_task(monitor.run())
    try:
        try:
            await bot.set_my_commands(COMMANDS)
        except Exception:
            log.exception("Could not set the command menu (the bot still works)")
        log.info("Bot started")
        await dp.start_polling(bot)
    finally:
        monitor.stop()
        await monitor_task
        await fetcher.close()
        await bot.session.close()
        db.close()
        await health_runner.cleanup()
