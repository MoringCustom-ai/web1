# -*- coding: utf-8 -*-
"""Ежедневные задания по расписанию (APScheduler):

  REMIND_HOUR     — напоминание об отметке всем активным подписчикам-участникам челленджа
  DAILY_POST_HOUR — вечерний пост динамики клуба в чат
  WEEKLY          — недельный итог (WEEKLY_POST_DOW)
  каждые ~10 мин — проверка истёкших подписок и отзыв доступа в чат
"""
from __future__ import annotations

import logging

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

import chat_access
import chat_posts
import config
import database as db
import utils
from handlers.user_flow import kb_marks

log = logging.getLogger("scheduler")


async def daily_reminders(bot: Bot) -> None:
    """Напоминание об отметке + авто-вступление в текущий челлендж для новичков."""
    ch = db.get_active_challenge()
    if not ch:
        return
    subs = db.active_subscribers()
    today_iso = utils.today().isoformat()
    sent = skipped = failed = 0
    for s in subs:
        uid = s["user_id"]
        # кто ещё не в челлендже — включается с текущего дня
        db.join_challenge(uid, ch["id"], today_iso)
        if db.get_mark(uid, ch["id"], today_iso):
            skipped += 1
            continue
        day = utils.day_number(today_iso, ch["duration_days"])
        text = config.TEXTS["reminder"].format(
            chal_title=ch["title"], day=day, total=ch["duration_days"])
        try:
            await bot.send_message(uid, text, reply_markup=kb_marks())
            sent += 1
        except Exception:  # заблокировал бота
            failed += 1
        await _throttle()
    log.info("Reminders: sent=%s skipped(already marked)=%s failed=%s", sent, skipped, failed)


async def _throttle():
    import asyncio
    await asyncio.sleep(0.05)


async def check_expirations(bot: Bot) -> None:
    """Отзыв доступа у тех, у кого подписка истекла и автопродление не сработало."""
    expired_ids = db.expire_due_subscriptions()
    for uid in expired_ids:
        await chat_access.restrict_member(bot, uid)
        sub_until = None
        await chat_access.notify_expired(bot, uid, until=sub_until)
    if expired_ids:
        log.info("Revoked access for %d expired subscriptions", len(expired_ids))


def build_scheduler(bot: Bot) -> AsyncIOScheduler:
    sched = AsyncIOScheduler(timezone=config.TZ)
    sched.add_job(daily_reminders, CronTrigger(hour=config.REMIND_HOUR, minute=0),
                  args=[bot], id="daily_reminders", coalesce=True, max_instances=1)
    sched.add_job(chat_posts.post_daily_summary,
                  CronTrigger(hour=config.DAILY_POST_HOUR, minute=0),
                  args=[bot], id="daily_club_post", coalesce=True, max_instances=1)
    sched.add_job(chat_posts.post_weekly_summary,
                  CronTrigger(day_of_week=config.WEEKLY_POST_DOW, hour=config.DAILY_POST_HOUR,
                              minute=30),
                  args=[bot], id="weekly_club_post", coalesce=True, max_instances=1)
    sched.add_job(check_expirations, "interval", minutes=10, args=[bot],
                  id="expire_check", coalesce=True, max_instances=1)
    return sched
