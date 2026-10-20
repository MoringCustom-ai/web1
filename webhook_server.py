# -*- coding: utf-8 -*-
"""Webhook Продамус: приём уведомлений об оплате и автопродлении.

Запуск в MVP: aiohttp-сервер на порту WEBHOOK_PORT (или через любой ASGI/WSGI,
если переносите). URL регистрируется в кабинете Продамус:
https://your-domain/prodamus/webhook

Логика:
  payment_status == "success" (и не refund):
    - проверяем подпись ipn_check;
    - находим user_id из custom_fields;
    - продлеваем подписку на срок тарифа;
    - открываем доступ в чат клуба;
    - шлём пользователю сообщение «Оплата прошла»;
  refunded / fail — фиксируем в payments, при refund отключаем доступ.
"""
from __future__ import annotations

import json
import logging

from aiohttp import web

import chat_access
import config
import database as db
import prodamus

log = logging.getLogger("webhook")

router = web.RouteTableDef()


async def _handle_success(app: web.Application, payload: dict) -> web.Response:
    """Вызывается из webhook-обработчика после проверки подписи."""
    data = prodamus.parse_ipn(payload)
    status = (data["status"] or "").lower()
    user_id = data["user_id"]
    tariff_key = data["tariff_key"]

    db.log_payment(user_id, data["payment_id"], data["amount"], tariff_key,
                   status, data["is_rebill"], json.dumps(payload, ensure_ascii=False),
                   data["paid_at"])

    if status in ("refunded", "fail", "canceled", "cancelled"):
        if status == "refunded" and user_id:
            db.cancel_subscription(user_id, status="cancelled")
            bot = app.get("bot")
            if bot:
                await chat_access.restrict_member(bot, user_id)
        return web.json_response({"ok": True})

    if status != "success" or not user_id or tariff_key not in config.TARIFFS:
        log.warning("Ignored IPN: status=%s user=%s tariff=%s", status, user_id, tariff_key)
        return web.json_response({"ignored": True})

    tariff = config.TARIFFS[tariff_key]
    expires_at = db.create_or_extend_subscription(
        user_id=user_id, tariff_key=tariff_key, price=data["amount"] or tariff["price"],
        days=tariff["days"], payment_id=data["payment_id"], rebill_id=data["rebill_id"],
    )

    bot = app.get("bot")
    if bot:
        await chat_access.grant_access(bot, user_id)
        # импортируем здесь, чтобы не было циклической зависимости модулей
        from handlers.user_flow import activate_after_payment
        await activate_after_payment(bot, user_id, tariff_key, expires_at,
                                     rebill_on=data["is_rebill"] or True)
    log.info("Subscription extended: user=%s tariff=%s until=%s", user_id, tariff_key, expires_at)
    return web.json_response({"ok": True})


@router.post("/prodamus/webhook")
async def prodamus_webhook(request: web.Request) -> web.Response:
    try:
        payload = await request.post()  # Продамус слёт form-encoded уведомления
        payload = {k: str(v) for k, v in payload.items()}
    except Exception:
        payload = {}
    if not payload:
        try:
            payload = await request.json()
        except Exception:
            return web.Response(status=400, text="bad body")

    if not prodamus.verify_ipn_signature(payload):
        return web.Response(status=403, text="invalid signature")

    return await _handle_success(request.app, payload)


@router.get("/healthz")
async def healthz(_: web.Request) -> web.Response:
    return web.Response(text="ok")


def make_app(bot=None) -> web.Application:
    app = web.Application()
    app.router.add_routes(router)
    if bot is not None:
        app["bot"] = bot
    return app


async def start_webhook_server(bot, host: str = "0.0.0.0", port: int | None = None):
    """Поднимает aiohttp-сервер рядом с long-polling ботом."""
    port = port or int(config.__dict__.get("WEBHOOK_PORT", 8081))
    app = make_app(bot=bot)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)
    await site.start()
    log.info("Webhook server listening on %s:%d", host, port)
    return runner
