# -*- coding: utf-8 -*-
"""Точка входа бота клуба «Шесть месяцев».

Запускает одновременно:
  - aiogram long-polling (пользовательские и админ-хендлеры, события чата);
  - aiohttp webhook-сервер для уведомлений Продамус;
  - APScheduler (напоминания, дневной/недельный посты, проверка истечений).
"""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

import config
import database as db
import scheduler as sched_mod
import webhook_server
from handlers import admin, chat_events, user_flow

log = logging.getLogger("main")

ALLOWED_UPDATES = ["message", "callback_query", "chat_member", "my_chat_member"]


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not config.BOT_TOKEN:
        raise SystemExit("BOT_TOKEN не задан — заполни .env (см. .env.example)")

    db.init_db()
    bot = Bot(token=config.BOT_TOKEN,
              default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    # админ раньше юзер-флоу, чтобы /start_challenge и т.п. не перехватывались
    dp.include_routers(admin.router, user_flow.router, chat_events.router)

    scheduler = sched_mod.build_scheduler(bot)
    scheduler.start()

    runner = await webhook_server.start_webhook_server(bot)
    log.info("Bot starting (polling + webhook + scheduler)")
    try:
        await dp.start_polling(bot, allowed_updates=ALLOWED_UPDATES)
    finally:
        scheduler.shutdown(wait=False)
        await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Stopped.")
