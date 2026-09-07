"""Инлайн-баттлы: `@бот` — открытый вызов, `@бот @vasya` — адресный.

bothost не даёт наружу порт, поэтому публичного URL для картинки нет и
InlineQueryResultPhoto не подходит. Идём вторым путём: отправляем картинку в
служебный чат, забираем file_id, сообщение удаляем (file_id остаётся рабочим)
и отдаём InlineQueryResultCachedPhoto.

Статистика считается по каждому чату отдельно, а инлайн-запрос чат не сообщает.
Зато опубликованный результат бот видит как обычное сообщение с `via_bot` — если
он в этом чате состоит. По нему вызов и привязывается к чату; если привязки не
случилось, значит бота в группе нет, и вместо результата показываем приглашение.
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import secrets
import time
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    BufferedInputFile, CallbackQuery, InlineQuery, InlineQueryResultCachedPhoto,
    InlineQueryResultsButton, InputMediaPhoto, Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

import card
import db
import scoring

router = Router()

AVATAR_DIR = Path(__file__).with_name("data") / "avatars"
AVATAR_TTL = 6 * 60 * 60
CARD_TTL = 10 * 60

# Служебный чат для получения file_id. Если не задан — шлём в личку тому, кто
# вызвал баттл (работает, только если он уже нажимал /start у бота).
STORAGE_CHAT_ID = os.getenv("STORAGE_CHAT_ID", "").strip()

# Инлайн срабатывает на каждый введённый символ. Без кэша это перерисовка и
# загрузка картинки на каждое нажатие клавиши.
_photo_cache: dict[str, tuple[str, float]] = {}

# Метка вызова в подписи: 12 невидимых символов, по одному на бит. Нужна, чтобы
# из опубликованного сообщения понять, какой именно вызов в нём лежит.
MARK_BITS = 12
ZERO_WIDTH = ("​", "‌")


def encode_mark(mark: int) -> str:
    return "".join(ZERO_WIDTH[(mark >> bit) & 1] for bit in range(MARK_BITS))


def decode_mark(text: str) -> int | None:
    bits = [ZERO_WIDTH.index(char) for char in text if char in ZERO_WIDTH]
    if len(bits) != MARK_BITS:
        return None
    return sum(bit << index for index, bit in enumerate(bits))


def handle(user) -> str:
    return f"@{user.username}" if user.username else "без ника"


def add_button(bot_username: str):
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить бота в группу",
              url=f"https://t.me/{bot_username}?startgroup=true&admin=delete_messages")
    kb.button(text="⚔️ Бросить вызов заново", switch_inline_query_current_chat="")
    kb.adjust(1)
    return kb.as_markup()


async def upload_photo(bot: Bot, data: bytes, hint_chat_id: int) -> str | None:
    """Получить file_id для картинки. Сообщение сразу удаляем — file_id живёт."""
    for chat_id in (chat for chat in (STORAGE_CHAT_ID, hint_chat_id) if chat):
        try:
            message = await bot.send_photo(
                chat_id, BufferedInputFile(data, filename="mog.jpg"), disable_notification=True
            )
        except TelegramAPIError as error:
            logging.info("Не вышло залить картинку в %s: %s", chat_id, error)
            continue
        file_id = message.photo[-1].file_id
        try:
            await bot.delete_message(message.chat.id, message.message_id)
        except TelegramAPIError:
            pass
        return file_id
    return None


async def fetch_avatar(bot: Bot, user_id: int) -> Path | None:
    """Аватарки может не быть или она закрыта приватностью — тогда None,
    карточка нарисует кружок с буквой."""
    AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    path = AVATAR_DIR / f"{user_id}.jpg"
    if path.exists() and time.time() - path.stat().st_mtime < AVATAR_TTL:
        return path
    try:
        photos = await bot.get_user_profile_photos(user_id, limit=1)
        if not photos.photos:
            return None
        await bot.download(photos.photos[0][-1].file_id, destination=path)
    except TelegramAPIError as error:
        logging.info("Нет доступа к аватару %s: %s", user_id, error)
        return path if path.exists() else None
    return path


def parse_target(text: str) -> str | None:
    """Из «@vasya» делаем «vasya». Резолвить ник в user_id не нужно: при нажатии
    кнопки мы просто сверяем ник того, кто её нажал."""
    text = (text or "").strip()
    candidate = text.split()[0].lstrip("@") if text else ""
    if 5 <= len(candidate) <= 32 and all(c.isascii() and (c.isalnum() or c == "_") for c in candidate):
        return candidate
    return None


async def bot_status(bot: Bot, chat_id: int) -> str:
    """Статус бота в чате: administrator / member / left. Ошибку доступа
    трактуем как «нет в чате» — для нас это одно и то же."""
    try:
        member = await bot.get_chat_member(chat_id, bot.id)
    except TelegramAPIError:
        return "left"
    # Статус — строковый enum: сравнивать с обычной строкой можно, а str() от него
    # даёт «ChatMemberStatus.MEMBER», поэтому берём .value.
    return member.status.value if hasattr(member.status, "value") else member.status


async def warn_not_admin(bot: Bot, chat_id: int) -> None:
    """Бот в чате, но без прав администратора: с включённым privacy-режимом он
    видит только команды, и статистика будет дырявой."""
    if not await db.should_warn(chat_id):
        return
    me = await bot.me()
    try:
        await bot.send_message(
            chat_id,
            "<b>⚠️ Я не администратор этого чата</b>\n"
            "<blockquote>Без прав администратора Telegram может не показывать мне обычные "
            "сообщения — статистика будет считаться с пропусками.</blockquote>\n"
            "Выдайте права админа, и всё встанет на место.",
            reply_markup=add_button(me.username),
        )
    except TelegramAPIError as error:
        logging.info("Не вышло предупредить чат %s: %s", chat_id, error)


@router.message(F.via_bot)
async def bind_published(message: Message, bot: Bot):
    """Опубликованный инлайн-результат прилетает боту как обычное сообщение —
    отсюда и узнаём, в каком чате идёт баттл."""
    if message.via_bot.id != bot.id or message.chat.type not in {"group", "supergroup"}:
        return
    await db.bind_challenge(
        message.from_user.id, message.chat.id, decode_mark(message.caption or "")
    )
    if await bot_status(bot, message.chat.id) != "administrator":
        await warn_not_admin(bot, message.chat.id)


@router.inline_query()
async def inline_challenge(query: InlineQuery, bot: Bot):
    user = query.from_user
    target = parse_target(query.query)
    if target and user.username and target.lower() == user.username.lower():
        target = None  # вызвать самого себя нельзя

    key = f"{user.id}:{target or ''}"
    cached = _photo_cache.get(key)
    if cached and time.time() - cached[1] < CARD_TTL:
        file_id = cached[0]
    else:
        avatar = await fetch_avatar(bot, user.id)
        data = await asyncio.to_thread(
            card.challenge_card, user.full_name, handle(user), avatar, target
        )
        file_id = await upload_photo(bot, data, user.id)
        if file_id is None:
            await query.answer(
                [], cache_time=1, is_personal=True,
                button=InlineQueryResultsButton(
                    text="Нажми /start, чтобы включить баттлы", start_parameter="boost"
                ),
            )
            return
        _photo_cache[key] = (file_id, time.time())

    # Токен и метка свои у каждого вызова: по метке потом определяем чат.
    token = secrets.token_urlsafe(9)
    mark = secrets.randbits(MARK_BITS)
    await db.save_challenge(token, user, target, mark)

    kb = InlineKeyboardBuilder()
    kb.button(text="⚔️ Принять вызов", callback_data=f"mog:{token}")
    await query.answer(
        [
            InlineQueryResultCachedPhoto(
                id=token,
                photo_file_id=file_id,
                title=f"Вызвать @{target}" if target else "Открытый вызов",
                description=(
                    f"Принять сможет только @{target}" if target
                    else "Баттл примет тот, кто первым нажмёт кнопку"
                ),
                caption=(
                    f"<b>⚔️ {html.escape(user.full_name)} вызывает "
                    f"{('@' + html.escape(target)) if target else 'любого желающего'} на баттл!</b>\n"
                    f"Жми кнопку, чтобы принять.{encode_mark(mark)}"
                ),
                reply_markup=kb.as_markup(),
            )
        ],
        cache_time=0,
        is_personal=True,
    )


async def show_missing(callback: CallbackQuery, bot: Bot) -> None:
    """Бота в этой группе нет — статистику по ней взять неоткуда."""
    me = await bot.me()
    try:
        await bot.edit_message_caption(
            inline_message_id=callback.inline_message_id,
            caption=(
                "<b>❌ Меня нет в этой группе</b>\n"
                "<blockquote>Статистика считается отдельно по каждому чату, "
                "а этот я не вижу — считать нечего.</blockquote>\n"
                "Добавьте меня сюда админом, дайте участникам пообщаться — и возвращайтесь "
                "за баттлом."
            ),
            reply_markup=add_button(me.username),
        )
    except TelegramAPIError as error:
        logging.info("Не вышло показать приглашение: %s", error)
    await callback.answer("Бота нет в этой группе — сначала добавьте его.", show_alert=True)


@router.callback_query(F.data.startswith("mog:"))
async def accept_challenge(callback: CallbackQuery, bot: Bot):
    challenge = await db.get_challenge(callback.data.split(":", 1)[1])
    if challenge is None:
        await callback.answer("Вызов устарел — брось новый.", show_alert=True)
        return

    rival = callback.from_user
    if rival.id == challenge["user_id"]:
        await callback.answer("Нельзя принять собственный вызов.", show_alert=True)
        return
    target = challenge["target"]
    if target and (rival.username or "").lower() != target.lower():
        await callback.answer(f"Этот вызов для @{target}.", show_alert=True)
        return

    # Чат не привязался — значит, опубликованного сообщения бот не увидел:
    # его в этой группе нет (или это личка/канал).
    chat_id = challenge["chat_id"]
    if chat_id is None or await bot_status(bot, chat_id) == "left":
        await show_missing(callback, bot)
        return

    left_stats = await db.battle_stats(chat_id, challenge["user_id"])
    right_stats = await db.battle_stats(chat_id, rival.id)
    if not (left_stats["known"] and right_stats["known"]):
        silent = challenge["name"] if not left_stats["known"] else rival.full_name
        await callback.answer(
            f"{silent} ещё ничего не писал в этом чате — статистика считается по каждому "
            "чату отдельно. Напишите пару сообщений и повторите.",
            show_alert=True,
        )
        return

    rows, left_total, right_total = scoring.score_battle(left_stats, right_stats)
    data = await asyncio.to_thread(
        card.battle_card,
        challenge["name"], challenge["username"], await fetch_avatar(bot, challenge["user_id"]),
        rival.full_name, handle(rival), await fetch_avatar(bot, rival.id),
        rows, left_total, right_total,
    )
    file_id = await upload_photo(bot, data, rival.id)
    if file_id is None:
        await callback.answer("Нажми /start у бота, чтобы он смог показать результат.",
                              show_alert=True)
        return

    left_label = challenge["username"] if challenge["username"].startswith("@") else challenge["name"]
    right_label = handle(rival) if rival.username else rival.full_name
    caption = scoring.battle_caption(
        html.escape(left_label), html.escape(right_label), rows, left_total, right_total
    )
    caption += f"\n<i>Статистика чата «{html.escape(await db.chat_title(chat_id))}»</i>"

    me = await bot.me()
    kb = InlineKeyboardBuilder()
    kb.button(text="⚔️ Новый вызов", switch_inline_query_current_chat="")
    kb.button(text="📈 Поднять статы", url=f"https://t.me/{me.username}?start=boost")
    kb.adjust(1)
    try:
        await bot.edit_message_media(
            inline_message_id=callback.inline_message_id,
            media=InputMediaPhoto(media=file_id, caption=caption),
            reply_markup=kb.as_markup(),
        )
    except TelegramAPIError as error:
        logging.warning("Не удалось показать результат баттла: %s", error)
        await callback.answer("Не получилось показать результат, попробуй ещё раз.", show_alert=True)
        return
    await callback.answer("Баттл посчитан!")
