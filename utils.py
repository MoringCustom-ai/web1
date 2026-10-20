# -*- coding: utf-8 -*-
"""Мелкие утилиты: часовой пояс клуба, разбор deep-link с utm-меткой."""
from __future__ import annotations

import datetime as dt
import re
from zoneinfo import ZoneInfo

import config

TZINFO = ZoneInfo(config.TZ)


def today() -> dt.date:
    """«Сегодня» по часовому поясу клуба — все отметки привязаны к этой дате."""
    return dt.datetime.now(TZINFO).date()


def now_local() -> dt.datetime:
    return dt.datetime.now(TZINFO)


def fmt_date(d: dt.date | str) -> str:
    if isinstance(d, str):
        d = dt.date.fromisoformat(d[:10])
    return d.strftime("%d.%m.%Y")


def parse_iso(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s)


def day_number(start_iso: str, duration_days: int, ref: dt.date | None = None) -> int:
    """Номер дня челленджа для пользователя. Кто вступил в середине — считается от joined_date,
    день 1 = день вступления. Ограничено длительностью челленджа."""
    ref = ref or today()
    start = dt.date.fromisoformat(start_iso[:10])
    n = (ref - start).days + 1
    return max(1, min(n, duration_days))


def user_day_dates(joined_iso: str, duration_days: int) -> list[str]:
    """Даты дней пользователя челленджа (от дня вступления, не длиннее duration)."""
    start = dt.date.fromisoformat(joined_iso[:10])
    return [(start + dt.timedelta(days=i)).isoformat() for i in range(duration_days)]


def calendar_line(values: list[str | None], today_idx: int) -> str:
    """Календарь из 14 клеток текстом: 🟩 ok / 🟨 part / 🟥 fail / ⬜ не отмечен / 🔵 сегодня."""
    icons = {"ok": "🟩", "part": "🟨", "fail": "🟥", None: "⬜"}
    cells = []
    for i, v in enumerate(values):
        if i == today_idx and v is None:
            cells.append("🔵")
        else:
            cells.append(icons.get(v, "⬜"))
    return "".join(cells)


_UTM_RE = re.compile(r"(utm_source|utm_campaign|utm_medium|utm_content|utm_term)=([\w\-.]+)")


def extract_utm(payload: str) -> dict[str, str]:
    """Разбирает метку источника из deep-link.

    Примеры:
      /start src=tg_channel&cmp=winter   -> {'src': 'tg_channel', ...}
      /start utm_source=youtube&utm_campaign=shorts
    """
    payload = (payload or "").strip()
    out: dict[str, str] = {}
    pairs = _UTM_RE.findall(payload)
    if pairs:
        mapping = {"utm_source": "source", "utm_campaign": "campaign",
                   "utm_medium": "medium", "utm_content": "content", "utm_term": "term"}
        for k, v in pairs:
            out[mapping[k]] = v
        return out
    # короткий формат: key=value&key2=value2
    for chunk in payload.split("&"):
        if "=" in chunk:
            k, v = chunk.split("=", 1)
            k = k.strip().lstrip("/").lower()
            if k in ("src", "utm", "s"):
                out["source"] = v
            elif k in ("cmp", "c"):
                out["campaign"] = v
            elif k in ("mdm", "m"):
                out["medium"] = v
    return out
