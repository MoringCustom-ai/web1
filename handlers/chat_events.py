# -*- coding: utf-8 -*-
"""События закрытого чата клуба: подтверждаем выдачу доступа и ловим «вышел сам»."""
from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import IS_MEMBER, IS_NOT_MEMBER, ChatMemberUpdatedFilter
from aiogram.types import ChatMemberUpdated

import chat_access
import config
import database as db

log = logging.getLogger("chat_events")
router = Router()


@router.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=IS_MEMBER))
async def on_bot_joined_chat(event: ChatMemberUpdated):
    log.info("Bot joined club chat %s", event.chat.id)


@router.chat_member(ChatMemberUpdatedFilter(member_status_changed=IS_MEMBER))
async def on_member_joined(event: ChatMemberUpdated):
    """Кто-то вошёл в чат по invite-ссылке: если подписка активна — выдаём полные права,
    иначе (зашёл «старый» участник без подписки) — ограничиваем."""
    user = event.new_chat_member.from_user
    if not user or user.is_bot:
        return
    await chat_access.provision_member(event.bot, user.id)


@router.chat_member(ChatMemberUpdatedFilter(member_status_changed=IS_NOT_MEMBER))
async def on_member_left(event: ChatMemberUpdated):
    """Участник вышел сам — ничего не делаем, отметки сохраняются в базе."""
    log.debug("member left chat: %s", event.from_user.id if event.from_user else "?")
