# -*- coding: utf-8 -*-
"""Посты бота в закрытый чат клуба: ежедневная и недельная динамика.

Логика как в мини-приложении (прототип «Мой путь / Клуб»):
  - берём участников активного челленджа;
  - для каждого считаем его «день N» от даты вступления (кто в середине — со своего дня);
  - агрегируем отметки этого дня по клубу: ok/part/fail/не отметились;
  - постим сводку в чат раз в вечер (DAILY_POST_HOUR), раз в неделю (WEEKLY_POST_DOW) — итоги.
"""
from __future__ import annotations

import datetime as dt
import logging

from aiogram import Bot

import config
import database as db
import utils

log = logging.getLogger("chat_posts")


async def post_daily_summary(bot: Bot) -> None:
    """Вечерний пост: сколько отметились СЕГОДНЯ (по календарной дате)."""
    ch = db.get_active_challenge()
    if not ch:
        log.info("No active challenge — daily post skipped")
        return
    parts = db.challenge_participants(ch["id"])
    today_iso = utils.today().isoformat()
    stats = db.day_stats(ch["id"], today_iso, parts)
    marked = stats["ok"] + stats["part"] + stats["fail"]
    day = utils.day_number(ch["start_date"], ch["duration_days"])
    ratio = stats["ok"] / stats["active"] if stats["active"] else 0
    comment = (config.CHALLENGE_COMMENTS["good"] if ratio >= 0.7 else
               config.CHALLENGE_COMMENTS["mid"] if ratio >= 0.4 else
               config.CHALLENGE_COMMENTS["bad"])
    text = config.TEXTS["chat_daily_post"].format(
        chal_title=ch["title"], day=day, total=ch["duration_days"],
        marked=marked, active=stats["active"],
        ok=stats["ok"], part=stats["part"], fa=stats["fail"], comment=comment)
    try:
        await bot.send_message(config.CHAT_CLUB_ID, text)
        log.info("Daily club post sent (day %s)", day)
    except Exception as e:
        log.error("Daily post failed: %s", e)


async def post_weekly_summary(bot: Bot) -> None:
    """Недельный пост: итоги за последние 7 календарных дней."""
    ch = db.get_active_challenge()
    if not ch:
        return
    parts = db.challenge_participants(ch["id"])
    dates = [(utils.today() - dt.timedelta(days=i)).isoformat() for i in range(1, 8)]
    w = db.week_stats(ch["id"], dates, parts)
    avg_pct = 0
    if parts and w["marked"]:
        per_day = [db.day_stats(ch["id"], d, parts) for d in dates]
        rates = [(s["ok"] + s["part"] + s["fail"]) / s["active"] for s in per_day if s["active"]]
        avg_pct = round(sum(rates) / len(rates) * 100) if rates else 0
    names = db.users_with_streak(ch["id"], min_days=7)
    streaks_line = "\n".join(f"🔥 {n} — {d} дн." for n, d in names[:10]) or "пока нет"
    new_members = db.new_members_last_week()
    text = config.TEXTS["chat_weekly_post"].format(
        chal_title=ch["title"], week_marked=w["marked"],
        w_ok=w["ok"], w_part=w["part"], w_fail=w["fail"],
        avg_pct=avg_pct, long_streaks=len(names),
        streaks_list=streaks_line, new_members=new_members)
    try:
        await bot.send_message(config.CHAT_CLUB_ID, text)
        log.info("Weekly club post sent")
    except Exception as e:
        log.error("Weekly post failed: %s", e)
