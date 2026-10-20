# -*- coding: utf-8 -*-
"""Выдача и отзыв доступа в закрытый чат клуба.

MVP-подход: бот — админ чата. При успешной оплате добавляем пользователя
через постоянную ссылку-приглашение (бот не может «добавить» сам, если у чата
нет invite-ссылки), а при истечении подписки ограничиваем права через
set_chat_permissions + удаляем из чата (kickChatMember без бана, чтобы человек
смог вернуться после продления).

Если нужен полностью автоматический add/remove — используйте_approach с
bot-админом и приватной группой, где участники добавляются по user_id через
invite link, сохранённый в config.CHAT_CLUB_INVITE_URL.
"""
from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError, TelegramBadRequest

import config
import database as db

log = logging.getLogger("chat_access")


async def grant_access(bot: Bot, user_id: int) -> bool:
    """Открывает доступ после оплаты.

    Bot API не позволяет «добавить» человека в группу без его действия, поэтому
    схема MVP: пользователю отправляется постоянная invite-ссылка, а когда он
    входит в чат (событие my_chat_member/new_chat_member — см.
    handlers/chat_events.py), бот выдаёт ему полные права через
    provision_member(). Здесь же пытаемся выдать права заранее на случай,
    если участник уже в чате (например, продлился).
    """
    if not config.CHAT_CLUB_INVITE_URL:
        log.warning("CHAT_CLUB_INVITE_URL пуст — доступ выдан только «на словах»")
    await provision_member(bot, user_id)
    return True


def _FULL_PERMS():
    from aiogram.types import ChatPermissions
    return ChatPermissions(
        can_send_messages=True,
        can_send_media_messages=True,
        can_send_polls=False,   # опросов в чате больше нет — отметка только в боте
        can_send_other_messages=True,
        can_add_web_page_previews=True,
    )


def _RESTRICTED_PERMS():
    from aiogram.types import ChatPermissions
    return ChatPermissions(
        can_send_messages=False,
        can_send_media_messages=False,
        can_send_other_messages=False,
        can_add_web_page_previews=False,
    )


async def provision_member(bot: Bot, user_id: int) -> None:
    """Пользователь пришёл в чат по ссылке — убеждаемся, что подписка активна,
    и выдаём полные права."""
    sub = db.get_active_subscription(user_id)
    if not sub:
        await restrict_member(bot, user_id)
        return
    try:
        await bot.restrict_chat_member(
            chat_id=config.CHAT_CLUB_ID,
            user_id=user_id,
            permissions=_FULL_PERMS(),
        )
        log.info("Full rights granted in club chat for %s", user_id)
    except TelegramBadRequest as e:
        # пользователя нет в чате — он ещё не перешёл по ссылке
        log.info("restrict skipped for %s: %s", user_id, e)


async def restrict_member(bot: Bot, user_id: int) -> None:
    """Отзыв доступа: ограничиваем права и убираем из чата (без блэклиста)."""
    try:
        await bot.restrict_chat_member(
            chat_id=config.CHAT_CLUB_ID,
            user_id=user_id,
            permissions=_RESTRICTED_PERMS(),
        )
    except TelegramAPIError as e:
        log.warning("restrict %s failed: %s", user_id, e)
    try:
        await bot.ban_chat_member(config.CHAT_CLUB_ID, user_id)
        # сразу разбаниваем, чтобы можно было вернуться после продления
        await bot.unban_chat_member(config.CHAT_CLUB_ID, user_id, revoke_messages=False)
        # ban+unban удаляет пользователя из чата — это штатный способ «kick» в Bot API
    except TelegramAPIError as e:
        log.warning("kick %s failed: %s", user_id, e)
    log.info("Access revoked for %s", user_id)


async def notify_expired(bot: Bot, user_id: int, until: str | None = None) -> None:
    """Сообщение в личку: подписка кончилась, доступ отозван."""
    text = config.TEXTS["expired"].format(until=until or "—")
    try:
        await bot.send_message(user_id, text)
    except (TelegramForbiddenError, TelegramAPIError) as e:
        log.info("Cannot notify %s about expiry: %s", user_id, e)
