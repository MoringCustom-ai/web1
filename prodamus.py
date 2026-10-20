# -*- coding: utf-8 -*-
"""Интеграция с Продамус (prodamus.pro): форма оплаты + webhook-уведомления.

Документация: https://docs.prodams.ru/
Схема MVP:
  1. Пользователь выбирает тариф -> получаем ссылку на форму оплаты
     https://my.prodams.ru/{product_id}?email=&custom_fields[user_id]=...
     (для автопродления в форме включён чекбокс рекуррентных платежей,
      тариф привязан к нужному плану через product_id/plan).
  2. После успешной оплаты Продамус шлёт POST-уведомление на наш webhook
     с полем ipn_check = md5(секрет + payment_id + payer_id + sum + currency ...).
     Мы проверяем подпись, продлеваем подписку и открываем доступ в чат.
  3. Отключение автопродления — по API (POST /api/set_recurring_payment_limit или
     отмена подписочного договора rebill_id), либо вручную администратором.

Здесь вся работа с сетью вынесена в тонкие функции, чтобы их было легко
заменить/протестировать моками.
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Optional
from urllib.parse import urlencode

import aiohttp

import config

log = logging.getLogger("prodamus")

API_BASE = "https://api.prodams.ru/api"


def build_payment_url(tariff_key: str, user_id: int,
                      email: Optional[str] = None) -> str:
    """Ссылка на форму оплаты конкретного тарифа.

    В личном кабинете Продамус для каждого тарифа создаётся свой товар/план;
    его id кладём в config.TARIFFS[key]["prodamus_plan_id"] (или shop_id).
    custom_fields позволяют вернуть user_id в webhook без дополнительных хранилищ.
    """
    tariff = config.TARIFFS[tariff_key]
    plan_id = tariff.get("prodamus_plan_id") or config.PRODAMUS_SHOP_ID
    params: dict[str, Any] = {
        "custom_fields": {"user_id": user_id, "tariff": tariff_key},
        "description": f"club6m:{tariff_key}:{user_id}",
    }
    if email:
        params["email"] = email
    # Продамус принимает вложенные поля в формате custom_fields[user_id]=...
    flat = {f"{k}[{kk}]": vv for k, v in params.items() if isinstance(v, dict)
            for kk, vv in v.items()}
    flat.update({k: v for k, v in params.items() if not isinstance(v, dict)})
    query = urlencode(flat)
    return f"https://my.prodams.ru/{plan_id}?{query}"


def verify_ipn_signature(data: dict[str, str]) -> bool:
    """Проверка ipn_check по алгоритму Продамус.

    Алгоритм (docs.prodams.ru): ipn_check = md5(secret + payment_id + payer_id +
    sum + currency + payment_status + is_test). Порядок конкатенации может
    отличаться в актуальной версии API — сверьтесь с кабинетом при запуске.
    """
    received = data.get("ipn_check", "")
    parts = "".join(str(data.get(k, "")) for k in (
        "payment_id", "payer_id", "sum", "currency", "payment_status", "is_test",
    ))
    calc = hashlib.md5((config.PRODAMUS_SECRET_KEY + parts).encode()).hexdigest()
    ok = received.lower() == calc.lower()
    if not ok:
        log.warning("IPN signature mismatch for payment_id=%s", data.get("payment_id"))
    return ok


def parse_ipn(data: dict[str, str]) -> dict[str, Any]:
    """Нормализует полезную нагрузку webhook в удобный dict."""
    cf = {}
    try:
        cf = json.loads(data.get("custom_fields") or "{}")
    except json.JSONDecodeError:
        pass
    out = {
        "payment_id": str(data.get("payment_id", "")),
        "user_id": int(cf.get("user_id") or data.get("custom_field_1") or 0) or None,
        "tariff_key": cf.get("tariff") or None,
        "amount": int(float(data.get("sum", 0))),
        "currency": data.get("currency", "RUB"),
        "status": data.get("payment_status", ""),
        "is_rebill": str(data.get("recurring_payment_id", "")) != "",
        "rebill_id": data.get("recurring_payment_id") or None,
        "paid_at": data.get("payment_date") or None,
        "email": data.get("email") or None,
        "raw": data,
    }
    return out


async def disable_recurring(rebill_id: str) -> bool:
    """Отключает автопродление на стороне Продамус.

    POST https://api.prodams.ru/api/recurring_payments/{id}/deactivate
    c авторизацией по secret_key. Точный путь см. в актуальном API-доке:
    при расхождении поправьте только эту функцию.
    """
    url = f"{API_BASE}/recurring_payments/{rebill_id}/deactivate"
    payload = {"secret_key": config.PRODAMUS_SECRET_KEY}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(15)) as resp:
                body = await resp.text()
                log.info("disable_recurring %s -> %s %s", rebill_id, resp.status, body[:200])
                return resp.status == 200
    except aiohttp.ClientError as e:  # сеть/таймаут — не роняем бота
        log.error("disable_recurring failed: %s", e)
        return False
