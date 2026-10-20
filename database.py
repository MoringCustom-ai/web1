# -*- coding: utf-8 -*-
"""Слой работы с SQLite для бота клуба «Шесть месяцев».

Таблицы:
  users        — все, кто тронул бота (включая метку источника utm)
  subscriptions— подписки Продамус (одна активная на пользователя)
  challenges   — челленджи (на старте один: дофаминовый детокс, 14 дней)
  participations — участие пользователя в челлендже (день старта = день входа)
  marks        — ежедневные отметки ok/part/fail
  payments     — история платежей/ипн от Продамус (для сверки и CSV)
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterable, Optional

DB_PATH = "club.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id       INTEGER PRIMARY KEY,
    username      TEXT,
    first_name    TEXT,
    last_name     TEXT,
    utm_source    TEXT,
    utm_campaign  TEXT,
    consent_at    TEXT,            -- ISO, когда согласился с офертой/политикой
    created_at    TEXT NOT NULL,
    last_active_at TEXT
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL REFERENCES users(user_id),
    tariff_key     TEXT NOT NULL,           -- 1m / 3m / 6m
    price          INTEGER NOT NULL,        -- RUB
    started_at     TEXT NOT NULL,
    expires_at     TEXT NOT NULL,
    rebill_enabled INTEGER NOT NULL DEFAULT 1,
    status         TEXT NOT NULL DEFAULT 'active',  -- active / expired / cancelled
    prodamus_payment_id TEXT,              -- id платежа Продамус
    prodamus_rebill_id  TEXT,              -- id рекуррентного подписочного договора
    UNIQUE(user_id, status)
);
-- примечание: статус 'active' уникален на пользователя; прошлые подписки
-- сохраняются со статусом expired/cancelled.

CREATE TABLE IF NOT EXISTS challenges (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    key           TEXT UNIQUE NOT NULL,
    title         TEXT NOT NULL,
    description   TEXT,
    duration_days INTEGER NOT NULL,
    start_date    TEXT NOT NULL,           -- ISO date, дата старта общего челленджа
    is_active     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS participations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    challenge_id INTEGER NOT NULL REFERENCES challenges(id),
    user_id      INTEGER NOT NULL REFERENCES users(user_id),
    joined_date  TEXT NOT NULL,            -- ISO date: с этого дня считаем его прогресс
    UNIQUE(challenge_id, user_id)
);

CREATE TABLE IF NOT EXISTS marks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES users(user_id),
    challenge_id INTEGER NOT NULL REFERENCES challenges(id),
    mark_date    TEXT NOT NULL,            -- ISO date (день по TZ клуба)
    value        TEXT NOT NULL CHECK(value IN ('ok','part','fail')),
    created_at   TEXT NOT NULL,
    updated_at   TEXT,
    UNIQUE(user_id, challenge_id, mark_date)
);

CREATE TABLE IF NOT EXISTS payments (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER REFERENCES users(user_id),
    prodamus_payment_id TEXT UNIQUE,
    amount           INTEGER,
    currency         TEXT DEFAULT 'RUB',
    tariff_key       TEXT,
    status           TEXT,                 -- success / refunded / failed...
    is_rebill        INTEGER DEFAULT 0,
    raw_json         TEXT,
    paid_at          TEXT,
    created_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_marks_ch_date ON marks(challenge_id, mark_date);
CREATE INDEX IF NOT EXISTS idx_subs_user ON subscriptions(user_id, status);
"""


def init_db(path: str = DB_PATH) -> None:
    with get_conn(path) as conn:
        conn.executescript(SCHEMA)


@contextmanager
def get_conn(path: str = DB_PATH):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- users
def upsert_user(user_id: int, username: str | None, first_name: str | None,
                last_name: str | None, utm_source: str | None = None,
                utm_campaign: str | None = None, path: str = DB_PATH) -> None:
    with get_conn(path) as conn:
        cur = conn.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,))
        if cur.fetchone():
            conn.execute(
                """UPDATE users SET username=?, first_name=?, last_name=?, last_active_at=?
                   WHERE user_id=?""",
                (username, first_name, last_name, _now_iso(), user_id),
            )
        else:
            conn.execute(
                """INSERT INTO users(user_id, username, first_name, last_name,
                        utm_source, utm_campaign, created_at, last_active_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (user_id, username, first_name, last_name,
                 utm_source, utm_campaign, _now_iso(), _now_iso()),
            )


def set_consent(user_id: int, path: str = DB_PATH) -> None:
    with get_conn(path) as conn:
        conn.execute("UPDATE users SET consent_at=? WHERE user_id=?", (_now_iso(), user_id))


def get_user(user_id: int, path: str = DB_PATH) -> Optional[sqlite3.Row]:
    with get_conn(path) as conn:
        return conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()


# ---------------------------------------------------------- subscriptions
def create_or_extend_subscription(user_id: int, tariff_key: str, price: int, days: int,
                                  payment_id: str | None, rebill_id: str | None,
                                  path: str = DB_PATH) -> str:
    """Оплата/продление: продлеваем активную или создаём новую. Возвращает expires_at."""
    now = dt.datetime.now(dt.timezone.utc)
    with get_conn(path) as conn:
        row = conn.execute(
            "SELECT * FROM subscriptions WHERE user_id=? AND status='active'", (user_id,)
        ).fetchone()
        if row:
            base = dt.datetime.fromisoformat(row["expires_at"])
            if base < now:
                base = now
            new_exp = base + dt.timedelta(days=days)
            conn.execute(
                """UPDATE subscriptions SET expires_at=?, tariff_key=?, price=?,
                       prodamus_payment_id=?, prodamus_rebill_id=?, status='active'
                   WHERE id=?""",
                (new_exp.isoformat(timespec="seconds"), tariff_key, price,
                 payment_id, rebill_id, row["id"]),
            )
        else:
            new_exp = now + dt.timedelta(days=days)
            conn.execute(
                """INSERT INTO subscriptions(user_id, tariff_key, price, started_at, expires_at,
                        status, prodamus_payment_id, prodamus_rebill_id)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (user_id, tariff_key, price, now.isoformat(timespec="seconds"),
                 new_exp.isoformat(timespec="seconds"), "active", payment_id, rebill_id),
            )
        return new_exp.isoformat(timespec="seconds")


def get_active_subscription(user_id: int, path: str = DB_PATH) -> Optional[sqlite3.Row]:
    with get_conn(path) as conn:
        return conn.execute(
            "SELECT * FROM subscriptions WHERE user_id=? AND status='active'", (user_id,)
        ).fetchone()


def set_rebill(user_id: int, enabled: bool, path: str = DB_PATH) -> None:
    with get_conn(path) as conn:
        conn.execute(
            "UPDATE subscriptions SET rebill_enabled=? WHERE user_id=? AND status='active'",
            (int(enabled), user_id),
        )


def cancel_subscription(user_id: int, status: str = "cancelled", path: str = DB_PATH) -> None:
    with get_conn(path) as conn:
        conn.execute(
            "UPDATE subscriptions SET status=? WHERE user_id=? AND status='active'",
            (status, user_id),
        )


def expire_due_subscriptions(path: str = DB_PATH) -> list[int]:
    """Возвращает user_id подписок, которые только что истекли (для отзыва доступа)."""
    now = _now_iso()
    with get_conn(path) as conn:
        rows = conn.execute(
            "SELECT user_id FROM subscriptions WHERE status='active' AND expires_at<=?", (now,)
        ).fetchall()
        for r in rows:
            conn.execute(
                "UPDATE subscriptions SET status='expired' WHERE user_id=? AND status='active'",
                (r["user_id"],),
            )
        return [r["user_id"] for r in rows]


def active_subscribers(path: str = DB_PATH) -> list[sqlite3.Row]:
    """Активные подписчики (user_id + тариф + expiry)."""
    with get_conn(path) as conn:
        return conn.execute(
            """SELECT s.user_id, s.tariff_key, s.expires_at, s.rebill_enabled,
                      u.username, u.first_name, u.last_name, u.utm_source, u.utm_campaign
               FROM subscriptions s JOIN users u ON u.user_id = s.user_id
               WHERE s.status='active' AND s.expires_at > ?""",
            (_now_iso(),),
        ).fetchall()


# ------------------------------------------------------------- challenges
def ensure_default_challenge(key: str, title: str, description: str, duration_days: int,
                             start_date: str, activate: bool = True,
                             path: str = DB_PATH) -> int:
    with get_conn(path) as conn:
        row = conn.execute("SELECT id FROM challenges WHERE key=?", (key,)).fetchone()
        if row:
            cid = row["id"]
            if activate:
                conn.execute("UPDATE challenges SET is_active=1 WHERE id=?", (cid,))
            return cid
        if activate:
            conn.execute("UPDATE challenges SET is_active=0")
        cur = conn.execute(
            """INSERT INTO challenges(key, title, description, duration_days, start_date, is_active)
               VALUES(?,?,?,?,?,?)""",
            (key, title, description, duration_days, start_date, int(activate)),
        )
        return cur.lastrowid


def get_active_challenge(path: str = DB_PATH) -> Optional[sqlite3.Row]:
    with get_conn(path) as conn:
        return conn.execute(
            "SELECT * FROM challenges WHERE is_active=1 ORDER BY id DESC LIMIT 1"
        ).fetchone()


def join_challenge(user_id: int, challenge_id: int, joined_date: str,
                   path: str = DB_PATH) -> str:
    """Вступление в челлендж. Кто в середине — включается с текущего дня."""
    with get_conn(path) as conn:
        row = conn.execute(
            "SELECT joined_date FROM participations WHERE challenge_id=? AND user_id=?",
            (challenge_id, user_id),
        ).fetchone()
        if row:
            return row["joined_date"]
        conn.execute(
            "INSERT INTO participations(challenge_id, user_id, joined_date) VALUES(?,?,?)",
            (challenge_id, user_id, joined_date),
        )
        return joined_date


def get_participation(user_id: int, challenge_id: int, path: str = DB_PATH) -> Optional[sqlite3.Row]:
    with get_conn(path) as conn:
        return conn.execute(
            "SELECT * FROM participations WHERE challenge_id=? AND user_id=?",
            (challenge_id, user_id),
        ).fetchone()


def challenge_participants(challenge_id: int, path: str = DB_PATH) -> list[sqlite3.Row]:
    with get_conn(path) as conn:
        return conn.execute(
            """SELECT p.*, u.username, u.first_name, u.last_name
               FROM participations p JOIN users u ON u.user_id=p.user_id
               WHERE p.challenge_id=?""",
            (challenge_id,),
        ).fetchall()


# ------------------------------------------------------------------ marks
def set_mark(user_id: int, challenge_id: int, mark_date: str, value: str,
             path: str = DB_PATH) -> None:
    assert value in ("ok", "part", "fail")
    now = _now_iso()
    with get_conn(path) as conn:
        conn.execute(
            """INSERT INTO marks(user_id, challenge_id, mark_date, value, created_at)
               VALUES(?,?,?,?,?)
               ON CONFLICT(user_id, challenge_id, mark_date)
               DO UPDATE SET value=excluded.value, updated_at=?""",
            (user_id, challenge_id, mark_date, value, now, now),
        )


def get_mark(user_id: int, challenge_id: int, mark_date: str,
             path: str = DB_PATH) -> Optional[str]:
    with get_conn(path) as conn:
        row = conn.execute(
            "SELECT value FROM marks WHERE user_id=? AND challenge_id=? AND mark_date=?",
            (user_id, challenge_id, mark_date),
        ).fetchone()
        return row["value"] if row else None


def user_marks(user_id: int, challenge_id: int, path: str = DB_PATH) -> dict[str, str]:
    """{mark_date: value}"""
    with get_conn(path) as conn:
        rows = conn.execute(
            "SELECT mark_date, value FROM marks WHERE user_id=? AND challenge_id=?",
            (user_id, challenge_id),
        ).fetchall()
        return {r["mark_date"]: r["value"] for r in rows}


def day_stats(challenge_id: int, day: str, participants: list[sqlite3.Row],
              path: str = DB_PATH) -> dict:
    """[ok, part, fail, active] как в мини-приложении CLUB[i]."""
    ids = [p["user_id"] for p in participants]
    stats = {"ok": 0, "part": 0, "fail": 0, "active": len(ids)}
    if not ids:
        return stats
    qmarks = ",".join("?" * len(ids))
    with get_conn(path) as conn:
        rows = conn.execute(
            f"SELECT value, COUNT(*) c FROM marks WHERE challenge_id=? AND mark_date=? "
            f"AND user_id IN ({qmarks}) GROUP BY value",
            [challenge_id, day, *ids],
        ).fetchall()
    for r in rows:
        stats[r["value"]] = r["c"]
    return stats


def week_stats(challenge_id: int, dates: Iterable[str],
               participants: list[sqlite3.Row], path: str = DB_PATH) -> dict:
    ids = [p["user_id"] for p in participants]
    out = {"marked": 0, "ok": 0, "part": 0, "fail": 0}
    if not ids:
        return out
    qmarks = ",".join("?" * len(ids))
    dqs = ",".join("?" * len(dates))
    with get_conn(path) as conn:
        rows = conn.execute(
            f"SELECT value, COUNT(*) c FROM marks WHERE challenge_id=? AND mark_date IN ({dqs}) "
            f"AND user_id IN ({qmarks}) GROUP BY value",
            [challenge_id, *dates, *ids],
        ).fetchall()
    for r in rows:
        out[r["value"]] = r["c"]
        out["marked"] += r["c"]
    return out


# --------------------------------------------------------------- payments
def log_payment(user_id: int | None, payment_id: str | None, amount: int | None,
                tariff_key: str | None, status: str, is_rebill: bool,
                raw_json: str | None, paid_at: str | None, path: str = DB_PATH) -> None:
    with get_conn(path) as conn:
        conn.execute(
            """INSERT INTO payments(user_id, prodamus_payment_id, amount, tariff_key, status,
                    is_rebill, raw_json, paid_at, created_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (user_id, payment_id, amount, tariff_key, status, int(is_rebill),
             raw_json, paid_at, _now_iso()),
        )


# ------------------------------------------------------------------ misc
def all_users_for_csv(path: str = DB_PATH) -> list[sqlite3.Row]:
    with get_conn(path) as conn:
        return conn.execute(
            """SELECT u.*, s.tariff_key, s.price, s.started_at, s.expires_at,
                      s.status AS sub_status, s.rebill_enabled
               FROM users u LEFT JOIN subscriptions s
                 ON s.user_id=u.user_id AND s.status='active'
               ORDER BY u.created_at"""
        ).fetchall()


@dataclass
class Streaks:
    current: int = 0
    best: int = 0


def calc_streaks(values_in_order: list[str | None]) -> Streaks:
    """Как streaks() в мини-приложении: ok удлиняет серию, part не сбрасывает, fail сбрасывает."""
    cur = best = 0
    for m in values_in_order:
        if m == "ok":
            cur += 1
        elif m == "part":
            pass
        else:
            cur = 0
        best = max(best, cur)
    return Streaks(current=cur, best=best)
