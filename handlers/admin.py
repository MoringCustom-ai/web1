# -*- coding: utf-8 -*-
"""Админ-команды MVP: рассылка, сводка, выгрузка CSV, запуск челленджа, стоп подписки."""
from __future__ import annotations

import csv
import datetime as dt
import io
import logging

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, Message

import config
import database as db
import utils

log = logging.getLogger("admin")
router = Router()


def _is_admin(message: Message) -> bool:
    return message.from_user.id in config.ADMIN_IDS


async def _guard(message: Message) -> bool:
    if not _is_admin(message):
        await message.answer("Эта команда только для админа.")
        return False
    return True


def _parse_args(text: str | None) -> tuple[str, str]:
    """'/broadcast active Привет!' -> ('active', 'Привет!')"""
    if not text:
        return "", ""
    body = text.strip()
    # отрезаем саму команду (первое слово /cmd[...])
    parts0 = body.split(maxsplit=1)
    body = parts0[1] if len(parts0) == 2 else ""
    parts = body.split(maxsplit=1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return (parts[0] if parts else ""), ""


# ------------------------------------------------------------------ /broadcast
@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message):
    """/broadcast [все|active|no_mark_today] <текст>

    все          — по всем пользователям базы
    active       — только активные подписчики (по умолчанию)
    no_mark_today — активные, кто ещё не отметился сегодня
    """
    if not await _guard(message):
        return
    audience, text = _parse_args(message.text)
    audience = audience or "active"
    if not text:
        await message.answer("Формат: /broadcast active <текст рассылки>")
        return

    if audience == "все":
        ids = [r["user_id"] for r in db.all_users_for_csv()]
    elif audience == "no_mark_today":
        ch = db.get_active_challenge()
        marked = set()
        if ch:
            marked = {m["user_id"] for m in _today_marks(ch["id"])}
        ids = [s["user_id"] for s in db.active_subscribers() if s["user_id"] not in marked]
    else:
        ids = [s["user_id"] for s in db.active_subscribers()]

    sent, failed = 0, 0
    for uid in ids:
        try:
            await message.bot.send_message(uid, f"📣 {text}")
            sent += 1
        except Exception:  # заблокировал бота и т.п.
            failed += 1
        await asyncio_sleep(0.05)  # антифлуд Telegram (~25 msg/s global)
    await message.answer(f"Рассылка готова: доставлено {sent}, ошибок {failed} (аудитория: {audience}).")


async def asyncio_sleep(seconds: float):
    import asyncio
    await asyncio.sleep(seconds)


def _today_marks(challenge_id: int):
    with db.get_conn() as conn:
        return conn.execute(
            "SELECT user_id FROM marks WHERE challenge_id=? AND mark_date=?",
            (challenge_id, utils.today().isoformat()),
        ).fetchall()


# -------------------------------------------------------------------- /summary
@router.message(Command("summary"))
async def cmd_summary(message: Message):
    """Сводка дня: воронка, подписки, отметки клуба."""
    if not await _guard(message):
        return
    ch = db.get_active_challenge()
    subs = db.active_subscribers()
    total_users = len(db.all_users_for_csv())
    paid_users = len(subs)
    consented = _count_consented()
    lines = [
        "📋 СВОДКА КЛУБА",
        f"Всего коснулись бота: {total_users}",
        f"Согласие с документами: {consented}",
        f"Активных подписок: {paid_users}",
    ]
    if ch:
        parts = db.challenge_participants(ch["id"])
        stats = db.day_stats(ch["id"], utils.today().isoformat(), parts)
        marked = stats["ok"] + stats["part"] + stats["fail"]
        pct = round(marked / stats["active"] * 100) if stats["active"] else 0
        day = utils.day_number(ch["start_date"], ch["duration_days"])
        lines += [
            f"Челлендж: «{ch['title']}», день {day} из {ch['duration_days']}",
            f"Участников челленджа: {len(parts)}",
            f"Отметились сегодня: {marked} из {stats['active']} ({pct}%)",
            f"✅ {stats['ok']} · 🟡 {stats['part']} · ❌ {stats['fail']}",
        ]
    revenue = _total_revenue()
    lines.append(f"Выручка (success): {revenue} ₽")
    await message.answer("\n".join(lines))


def _count_consented() -> int:
    with db.get_conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) c FROM users WHERE consent_at IS NOT NULL").fetchone()["c"]


def _total_revenue() -> int:
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(amount),0) s FROM payments WHERE status='success'"
        ).fetchone()
        return row["s"]


# --------------------------------------------------------------------- /export
@router.message(Command("export"))
async def cmd_export(message: Message):
    """CSV для дашбордов: users + подписка + отметки по дням текущего челленджа."""
    if not await _guard(message):
        return
    ch = db.get_active_challenge()
    rows = db.all_users_for_csv()
    marks_by_user: dict[int, dict[str, str]] = {}
    if ch:
        with db.get_conn() as conn:
            for m in conn.execute(
                "SELECT user_id, mark_date, value FROM marks WHERE challenge_id=?",
                (ch["id"],),
            ):
                marks_by_user.setdefault(m["user_id"], {})[m["mark_date"]] = m["value"]

    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")
    header = ["user_id", "username", "first_name", "utm_source", "utm_campaign",
              "consent_at", "created_at", "tariff", "price", "expires_at",
              "sub_status", "rebill_enabled"]
    if ch:
        dates = [(dt.date.fromisoformat(ch["start_date"]) + dt.timedelta(days=i)).isoformat()
                 for i in range(ch["duration_days"])]
        header += [f"day_{i+1}_{d}" for i, d in enumerate(dates)]
    writer.writerow(header)
    now_iso = utils.today().isoformat()
    for r in rows:
        line = [r["user_id"], r["username"], r["first_name"], r["utm_source"],
                r["utm_campaign"], r["consent_at"], r["created_at"], r["tariff_key"],
                r["price"], r["expires_at"], r["sub_status"], r["rebill_enabled"]]
        if ch:
            um = marks_by_user.get(r["user_id"], {})
            part = db.get_participation(r["user_id"], ch["id"])
            joined = dt.date.fromisoformat(part["joined_date"]) if part else None
            for i in range(ch["duration_days"]):
                if joined is None:
                    line.append("")
                    continue
                d = joined + dt.timedelta(days=i)
                line.append(um.get(d.isoformat(), "") if d <= dt.date.fromisoformat(now_iso) else "")
        writer.writerow(line)

    doc = BufferedInputFile(buf.getvalue().encode("utf-8-sig"),
                            filename=f"club_export_{now_iso}.csv")
    await message.answer_document(doc)


# ------------------------------------------------------------ /start_challenge
@router.message(Command("start_challenge"))
async def cmd_start_challenge(message: Message):
    """/start_challenge [ключ|название] — запускает общий челлендж.

    Без аргумента — дефолтный «Дофаминовый детокс» на 14 дней со вчерашней даты
    (чтобы первый пост динамики был уже сегодня).
    Формат с параметрами: /start_challenge key=title|days=14
    """
    if not await _guard(message):
        return
    arg, rest = _parse_args(message.text)
    # стартуем «со вчерашнего дня», чтобы первый вечерний пост динамики был уже сегодня
    start_yesterday = (utils.today() - dt.timedelta(days=1)).isoformat()
    if not arg:
        d = config.DEFAULT_CHALLENGE
        ch_row = db.ensure_default_challenge(
            d["key"], d["title"], d["description"], d["duration_days"], start_yesterday)
        cid, title, days = ch_row["id"], d["title"], d["duration_days"]
    else:
        # /start_challenge Дофаминовый детокс days=14   или   key=dopamine_detox ...
        kv = {}
        words = []
        for w in (arg + " " + rest).split():
            if "=" in w:
                k, v = w.split("=", 1)
                kv[k] = v
            else:
                words.append(w)
        title = " ".join(words) or config.DEFAULT_CHALLENGE["title"]
        days = int(kv.get("days", config.DEFAULT_CHALLENGE["duration_days"]))
        key = kv.get("key", title.lower().replace(" ", "_")[:32])
        ch_row = db.ensure_default_challenge(key, title, title, days, start_yesterday)
        cid = ch_row["id"]
    # авто-вступление активных подписчиков, кто ещё не в челлендже
    subs = db.active_subscribers()
    for s in subs:
        db.join_challenge(s["user_id"], cid, utils.today().isoformat())
    await message.answer(
        f"🏁 Челлендж «{title}» запущен на {days} дней с {utils.fmt_date(start_yesterday)}. "
        f"Автоподписано участников: {len(subs)}.")


# ------------------------------------------------------------------- /stop_sub
@router.message(Command("stop_sub"))
async def cmd_stop_sub(message: Message):
    """/stop_sub <user_id> — ручная отмена подписки и отзыв доступа в чат."""
    if not await _guard(message):
        return
    arg, _ = _parse_args(message.text)
    try:
        uid = int(arg)
    except (TypeError, ValueError):
        await message.answer("Формат: /stop_sub 123456789")
        return
    db.cancel_subscription(uid, status="cancelled")
    import chat_access
    await chat_access.restrict_member(message.bot, uid)
    await message.answer(f"Подписка пользователя {uid} остановлена, доступ в чат отозван.")
