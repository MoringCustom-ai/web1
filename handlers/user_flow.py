# -*- coding: utf-8 -*-
"""Пользовательский флоу: вход по ссылке с utm → согласие → приветствие с видео →
FAQ → выбор тарифа → оплата Продамус → кабинет → отметка и личная динамика."""
from __future__ import annotations

import datetime as dt
import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)

import chat_access
import config
import database as db
import prodamus
import utils
from utils import fmt_date, today

log = logging.getLogger("user_flow")
router = Router()

MARK_CB_PREFIX = "mark:"          # mark:ok / mark:part / mark:fail
TARIFF_CB_PREFIX = "tariff:"      # tariff:1m ...
FAQ_CB_PREFIX = "faq:"            # faq:<idx>


# ------------------------------------------------------------------ keyboards
def kb_consent() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Согласен ✅", callback_data="consent:yes")],
        [InlineKeyboardButton(text="Не согласен", callback_data="consent:no")],
    ])


def kb_main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Выбрать тариф", callback_data="pay"),
         InlineKeyboardButton(text="✅ Отметиться сегодня", callback_data="today")],
        [InlineKeyboardButton(text="👤 Кабинет", callback_data="cabinet"),
         InlineKeyboardButton(text="📊 Моя динамика", callback_data="trend")],
        [InlineKeyboardButton(text="💬 Чат клуба", callback_data="chat"),
         InlineKeyboardButton(text="📜 Правила", callback_data="rules")],
        [InlineKeyboardButton(text="❓ FAQ", callback_data="faq")],
    ])


def kb_tariffs() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(
        text=f'{t["title"]} — {t["price"]} ₽', callback_data=f"{TARIFF_CB_PREFIX}{key}")]
        for key, t in config.TARIFFS.items()]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_marks() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=config.MARKS["ok"], callback_data=f"{MARK_CB_PREFIX}ok"),
        InlineKeyboardButton(text=config.MARKS["part"], callback_data=f"{MARK_CB_PREFIX}part"),
        InlineKeyboardButton(text=config.MARKS["fail"], callback_data=f"{MARK_CB_PREFIX}fail"),
    ]])


def kb_faq() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=q, callback_data=f"{FAQ_CB_PREFIX}{i}")]
            for i, q in enumerate(config.FAQ)]
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------------------------------------------------------------- /start
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    """Вход по ссылке с меткой источника: /start src=youtube&cmp=shorts."""
    payload = ""
    if message.text and len(message.text.split()) > 1:
        payload = message.text.split(maxsplit=1)[1]
    utm = utils.extract_utm(payload)
    u = message.from_user
    db.upsert_user(u.id, u.username, u.first_name, u.last_name,
                   utm_source=utm.get("source"), utm_campaign=utm.get("campaign"))

    user_row = db.get_user(u.id)
    # Уже в клубе? — сразу меню кабинета
    if user_row and user_row["consent_at"] and db.get_active_subscription(u.id):
        await message.answer("Ты уже в клубе 👇", reply_markup=kb_main_menu())
        return

    # Шаг 1: согласие с документами
    text = config.TEXTS["consent"].format(
        offer=config.DOCS_OFFER_URL, privacy=config.DOCS_PRIVACY_URL)
    await message.answer(text, reply_markup=kb_consent(), disable_web_page_preview=False)


@router.callback_query(F.data == "consent:yes")
async def cb_consent_yes(cb: CallbackQuery):
    db.set_consent(cb.from_user.id)
    ch = db.get_active_challenge()
    video_hint = config.TEXTS["video_hint"] if config.VIDEO_WELCOME_URL else ""
    text = config.TEXTS["welcome"].format(
        challenge_title=ch["title"] if ch else config.DEFAULT_CHALLENGE["title"],
        challenge_desc=(ch["description"] if ch else config.DEFAULT_CHALLENGE["description"]),
        video_hint=video_hint,
    )
    if config.VIDEO_WELCOME_URL:
        try:
            await cb.message.answer_video(config.VIDEO_WELCOME_URL)
        except Exception as e:  # url/file_id может быть невалидным — не роняем флоу
            log.warning("welcome video failed: %s")
    await cb.message.answer(text, reply_markup=kb_main_menu())
    await cb.answer("Отлично, добро пожаловать!")


@router.callback_query(F.data == "consent:no")
async def cb_consent_no(cb: CallbackQuery):
    await cb.message.answer(
        "Без согласия с офертой и политикой мы не можем продолжить. "
        "Если передумаешь — нажми /start.")
    await cb.answer()


# ------------------------------------------------------------------------ FAQ
@router.callback_query(F.data == "faq")
async def cb_faq(cb: CallbackQuery):
    await cb.message.answer(config.TEXTS["faq_question"], reply_markup=kb_faq())
    await cb.answer()


@router.callback_query(F.data.startswith(FAQ_CB_PREFIX))
async def cb_faq_item(cb: CallbackQuery):
    idx = int(cb.data.split(":")[1])
    question = list(config.FAQ)[idx]
    await cb.message.answer(f"❓ {question}\n\n{config.FAQ[question]}")
    await cb.answer()


# --------------------------------------------------------------- тариф и оплата
def _tariff_text() -> str:
    t = config.TARIFFS

    def save_pct(long_key, months):
        per_month = t[long_key]["price"] / months
        base = t["1m"]["price"]
        return round((1 - per_month / base) * 100)

    return config.TEXTS["choose_tariff"].format(
        t1_title=t["1m"]["title"], t1_price=t["1m"]["price"],
        t2_title=t["3m"]["title"], t2_price=t["3m"]["price"], t2_save=save_pct("3m", 3),
        t3_title=t["6m"]["title"], t3_price=t["6m"]["price"], t3_save=save_pct("6m", 6),
    )


@router.callback_query(F.data == "pay")
async def cb_pay(cb: CallbackQuery):
    await cb.message.answer(_tariff_text(), reply_markup=kb_tariffs())
    await cb.answer()


@router.callback_query(F.data.startswith(TARIFF_CB_PREFIX))
async def cb_tariff(cb: CallbackQuery):
    tariff_key = cb.data.split(":")[1]
    if tariff_key not in config.TARIFFS:
        await cb.answer("Тариф не найден", show_alert=True)
        return
    await cb.message.answer(config.TEXTS["pay_wait"])
    url = prodamus.build_payment_url(tariff_key, cb.from_user.id)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f'Оплатить {config.TARIFFS[tariff_key]["price"]} ₽ 💳',
                             url=url)]])
    await cb.message.answer(
        f'Тариф «{config.TARIFFS[tariff_key]["title"]}». Нажми кнопку — откроется '
        f"безопасная форма Продамус (карта/СБП). После оплаты всё активируется само.",
        reply_markup=kb)
    await cb.answer()


async def activate_after_payment(bot, user_id: int, tariff_key: str,
                                 expires_at: str, rebill_on: bool = True):
    """Общий код после первой оплаты и после автопродления (вызывается из webhook)."""
    ch = db.get_active_challenge()
    if ch:
        joined = db.join_challenge(user_id, ch["id"], today().isoformat())
        if joined == today().isoformat():  # впервые вступил сегодня
            try:
                await bot.send_message(user_id, config.TEXTS["join_challenge"].format(
                    chal_title=ch["title"], start_date=fmt_date(joined),
                    duration=ch["duration_days"], remind_hour=config.REMIND_HOUR))
            except Exception as e:
                log.info("join_challenge notice failed for %s: %s", user_id, e)
    try:
        await bot.send_message(user_id, config.TEXTS["paid"].format(
            until=fmt_date(expires_at),
            tariff=config.TARIFFS.get(tariff_key, {}).get("title", tariff_key),
            rebill="включено (отключить: /cabinet)" if rebill_on else "выключено",
            chat_url=config.CHAT_CLUB_INVITE_URL or "спроси у админа",
            remind_hour=config.REMIND_HOUR,
        ), reply_markup=kb_main_menu())
    except Exception as e:
        log.warning("paid notice failed for %s: %s", user_id, e)


# ------------------------------------------------------------- личный кабинет
def _personal_stats(user_id: int, ch) -> dict:
    """Считает день челленджа, серию и календарь пользователя (как в мини-приложении)."""
    part = db.get_participation(user_id, ch["id"])
    marks = db.user_marks(user_id, ch["id"])
    if part:
        dates = utils.user_day_dates(part["joined_date"], ch["duration_days"])
    else:
        dates = []
    values = [marks.get(d) for d in dates]
    streaks = db.calc_streaks(values)
    day_idx = utils.day_number(part["joined_date"], ch["duration_days"]) - 1 if part else 0
    ok = sum(v == "ok" for v in values)
    marked = sum(v is not None for v in values)
    return {
        "dates": dates, "values": values, "streaks": streaks, "day_idx": day_idx,
        "ok": ok, "marked": marked, "part_count": sum(v == "part" for v in values),
        "fail_count": sum(v == "fail" for v in values),
        "none_count": sum(v is None for v in values),
        "day": min(day_idx + 1, ch["duration_days"]) if dates else 0,
    }


@router.callback_query(F.data == "cabinet")
@router.message(Command("cabinet"))
async def show_cabinet(event: Message | CallbackQuery):
    message = event.message if isinstance(event, CallbackQuery) else event
    user_id = event.from_user.id
    sub = db.get_active_subscription(user_id)
    if not sub:
        await message.answer(config.TEXTS["no_subscription"], reply_markup=kb_tariffs())
        if isinstance(event, CallbackQuery):
            await event.answer()
        return
    ch = db.get_active_challenge()
    st = _personal_stats(user_id, ch) if ch else {}
    now = dt.datetime.now(dt.timezone.utc)
    expired = utils.parse_iso(sub["expires_at"]) <= now
    next_action = ("Ждём твою отметку сегодня 😉" if not expired and st.get("day_idx", 0) >= 0
                   else "Продли подписку: /pay")
    text = config.TEXTS["cabinet"].format(
        tariff=config.TARIFFS.get(sub["tariff_key"], {}).get("title", sub["tariff_key"]),
        until=fmt_date(sub["expires_at"]),
        rebill=("включено" if sub["rebill_enabled"] else "выключено"),
        chal_name=ch["title"] if ch else "—",
        day=st.get("day", 0), total=ch["duration_days"] if ch else 0,
        streak=st.get("streaks").current if st else 0,
        best=st.get("streaks").best if st else 0,
        ok=st.get("ok", 0), marked=st.get("marked", 0),
        pct=round(st.get("ok", 0) / st["marked"] * 100) if st.get("marked") else 0,
        next_action=next_action,
    )
    rows = [[InlineKeyboardButton(
        text="🔁 Автопродление: " + ("выключить" if sub["rebill_enabled"] else "включить"),
        callback_data="rebill:toggle")]]
    if config.CHAT_CLUB_INVITE_URL:
        rows.append([InlineKeyboardButton(text="💬 Чат клуба",
                                          url=config.CHAT_CLUB_INVITE_URL)])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await message.answer(text, reply_markup=kb)
    if isinstance(event, CallbackQuery):
        await event.answer()


@router.callback_query(F.data == "rebill:toggle")
async def cb_rebill_toggle(cb: CallbackQuery):
    sub = db.get_active_subscription(cb.from_user.id)
    if not sub:
        await cb.answer("Нет активной подписки", show_alert=True)
        return
    new_val = not bool(sub["rebill_enabled"])
    db.set_rebill(cb.from_user.id, new_val)
    if not new_val and sub["prodamus_rebill_id"]:
        ok = await prodamus.disable_recurring(sub["prodamus_rebill_id"])
        note = "Заявка на отключение автоплатежа отправлена в Продамус." if ok else \
               "На стороне Продамус что-то не получилось — напиши админу, разберёмся."
    else:
        note = "Автопродление включено 🔁" if new_val else "Автопродление выключено"
    await cb.message.answer(note)
    await cb.answer()


# ----------------------------------------------------------- отметка дня (/today)
@router.callback_query(F.data == "today")
@router.message(Command("today"))
async def cmd_today(event: Message | CallbackQuery):
    message = event.message if isinstance(event, CallbackQuery) else event
    user_id = event.from_user.id
    ch = db.get_active_challenge()
    if not ch:
        await message.answer("Сейчас нет активного челленджа — админ скоро запустит новый 🏁")
        if isinstance(event, CallbackQuery):
            await event.answer()
        return
    sub = db.get_active_subscription(user_id)
    if not sub:
        await message.answer(config.TEXTS["no_subscription"], reply_markup=kb_tariffs())
        if isinstance(event, CallbackQuery):
            await event.answer()
        return
    part = db.get_participation(user_id, ch["id"])
    joined = part["joined_date"] if part else today().isoformat()
    day = utils.day_number(joined, ch["duration_days"])
    existing = db.get_mark(user_id, ch["id"], today().isoformat())
    if existing:
        text = config.TEXTS["mark_already"].format(mark=config.MARKS[existing])
    else:
        text = config.TEXTS["reminder"].format(
            chal_title=ch["title"], day=day, total=ch["duration_days"])
    await message.answer(text, reply_markup=kb_marks())
    if isinstance(event, CallbackQuery):
        await event.answer()


@router.callback_query(F.data.startswith(MARK_CB_PREFIX))
async def cb_mark(cb: CallbackQuery):
    value = cb.data.split(":")[1]
    if value not in config.MARKS:
        await cb.answer("Неизвестная отметка", show_alert=True)
        return
    user_id = cb.from_user.id
    ch = db.get_active_challenge()
    if not ch:
        await cb.answer("Нет активного челленджа", show_alert=True)
        return
    sub = db.get_active_subscription(user_id)
    if not sub:
        await cb.answer("Нужна активная подписка", show_alert=True)
        return
    # кто в середине — включается с текущего дня
    db.join_challenge(user_id, ch["id"], today().isoformat())
    db.set_mark(user_id, ch["id"], today().isoformat(), value)
    part = db.get_participation(user_id, ch["id"])
    st = _personal_stats(user_id, ch)
    await cb.message.answer(config.TEXTS["mark_saved"].format(
        mark=config.MARKS[value], streak=st["streaks"].current))
    await cb.answer()


# ------------------------------------------------------------ личная динамика
@router.callback_query(F.data == "trend")
@router.message(Command("trend"))
async def cmd_trend(event: Message | CallbackQuery):
    message = event.message if isinstance(event, CallbackQuery) else event
    user_id = event.from_user.id
    ch = db.get_active_challenge()
    if not ch:
        await message.answer("Челлендж ещё не запущен.")
        if isinstance(event, CallbackQuery):
            await event.answer()
        return
    part = db.get_participation(user_id, ch["id"])
    if not part:
        await message.answer("Ты пока не в челлендже — отметься сегодня: /today")
        if isinstance(event, CallbackQuery):
            await event.answer()
        return
    st = _personal_stats(user_id, ch)
    cal = utils.calendar_line(st["values"], st["day_idx"])
    # «Ты vs Клуб» — доля дней «выдержал», как в мини-приложении
    my_total = sum(1 for v in st["values"] if v) or 0
    my_pct = round(st["ok"] / my_total * 100) if my_total else 0
    parts = db.challenge_participants(ch["id"])
    club_ok = club_all = 0
    for p in parts:
        j = dt.date.fromisoformat(p["joined_date"])
        span = (utils.today() - j).days + 1
        span = min(span, ch["duration_days"])
        pm = db.user_marks(p["user_id"], ch["id"])
        dates = utils.user_day_dates(p["joined_date"], ch["duration_days"])[:span]
        vals = [pm.get(d) for d in dates]
        club_ok += sum(1 for v in vals if v == "ok")
        club_all += sum(1 for v in vals if v)
    club_pct = round(club_ok / club_all * 100) if club_all else 0
    vs_comment = ("Ты идёшь ровнее, чем клуб в среднем 💪" if my_pct >= club_pct else
                  "Клуб идёт чуть ровнее, но твой темп — твой. Главное не выпадать.")
    sub = db.get_active_subscription(user_id)
    month = 1
    if sub:
        started = utils.parse_iso(sub["started_at"]).date()
        month = max(1, min(6, (utils.today() - started).days // 30 + 1))
    text = config.TEXTS["personal_trend"].format(
        chal_title=ch["title"], calendar=cal,
        ok=st["ok"], part=st["part_count"], fail=st["fail_count"], none=st["none_count"],
        streak=st["streaks"].current, best=st["streaks"].best,
        month=month, my_pct=my_pct, club_pct=club_pct, vs_comment=vs_comment,
    )
    await message.answer(text)
    if isinstance(event, CallbackQuery):
        await event.answer()


# --------------------------------------------------------------- прочие команды
@router.callback_query(F.data == "rules")
@router.message(Command("rules"))
async def cmd_rules(event: Message | CallbackQuery):
    message = event.message if isinstance(event, CallbackQuery) else event
    await message.answer(config.TEXTS["rules"].format(
        offer=config.DOCS_OFFER_URL, privacy=config.DOCS_PRIVACY_URL))
    if isinstance(event, CallbackQuery):
        await event.answer()


@router.callback_query(F.data == "chat")
@router.message(Command("chat"))
async def cmd_chat(event: Message | CallbackQuery):
    message = event.message if isinstance(event, CallbackQuery) else event
    user_id = event.from_user.id
    if not db.get_active_subscription(user_id):
        await message.answer(config.TEXTS["no_subscription"], reply_markup=kb_tariffs())
    elif config.CHAT_CLUB_INVITE_URL:
        await message.answer(f"💬 Закрытый чат клуба: {config.CHAT_CLUB_INVITE_URL}")
    else:
        await message.answer("Ссылка на чат ещё не настроена — напиши админу.")
    if isinstance(event, CallbackQuery):
        await event.answer()


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(config.TEXTS["help"])


@router.message(Command("pay"))
async def cmd_pay(message: Message):
    await message.answer(_tariff_text(), reply_markup=kb_tariffs())
