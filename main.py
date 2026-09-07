"""MOG Battle — бот статистики чата и инлайн-баттлов.

Запуск: положить папку на bothost, переменная окружения BOT_TOKEN.
Необязательно: STORAGE_CHAT_ID — id канала/чата, куда бот кладёт картинки,
чтобы получить file_id (иначе используется личка вызвавшего).
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import subprocess
import sys
from pathlib import Path

# Зависимости ставим сами: на хостинге может не быть заранее собранного окружения.
try:
    import aiogram  # noqa: F401
    import aiosqlite  # noqa: F401
    import PIL  # noqa: F401
except ImportError:
    subprocess.check_call([
        sys.executable, "-m", "pip", "install", "-q", "-r",
        str(Path(__file__).with_name("requirements.txt")),
    ])
    os.execv(sys.executable, [sys.executable, *sys.argv])

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message, TelegramObject
from aiogram.utils.keyboard import InlineKeyboardBuilder

import analysis
import battle
import db
import scoring

router = Router()

GROUPS = {"group", "supergroup"}


# ── Учёт сообщений ───────────────────────────────────────────────────────
# Middleware, а не хэндлер: команды и обычные сообщения должны попадать в
# статистику одинаково, независимо от того, кто их потом обработает.
async def counter(handler, event: TelegramObject, data: dict):
    message: Message = event
    user = message.from_user
    if message.chat.type in GROUPS and user is not None and not user.is_bot:
        text = message.text or message.caption or ""
        db.track(user, message.chat, analysis.analyse(text))
    return await handler(event, data)


# ── Меню ─────────────────────────────────────────────────────────────────
def main_menu(bot_username: str):
    kb = InlineKeyboardBuilder()
    kb.button(text="⚔️ Начать баттл", switch_inline_query="")
    kb.button(text="📊 Моя статистика", callback_data="menu:stats")
    kb.button(text="📈 Как поднять статы", callback_data="menu:boost")
    kb.button(text="➕ Добавить в группу", url=f"https://t.me/{bot_username}?startgroup=true")
    kb.adjust(1)
    return kb.as_markup()


def back_menu():
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data="menu:home")
    return kb.as_markup()


MENU_TEXT = (
    "<b>⚔️ MOG BATTLE</b>\n"
    "<blockquote>Считаю статистику участников чата и стравливаю их в баттлах.</blockquote>\n"
    "Напиши <code>@{username}</code> в группе, где я состою — уйдёт вызов, соперник жмёт "
    "кнопку и получает карточку сравнения.\n\n"
    "<i>Сравниваю: активность за день, неделю, месяц и всё время, культурность, "
    "грамотность и время в чате. Статистика у каждого чата своя.</i>"
)

BOOST_TEXT = (
    "<b>📈 Как поднять статы</b>\n"
    "<blockquote>Балл по каждой строке считается сам по себе, а не в сравнении "
    "с соперником — расти можно бесконечно.</blockquote>\n"
    "• <b>Активность</b> — пиши каждый день, а не раз в месяц пачкой.\n"
    "• <b>Культурность</b> — меньше мата, а особенно оскорблений: они весят вдвое.\n"
    "• <b>Грамотность</b> — заглавные буквы, знаки в конце, без КАПСА и «крч».\n"
    "• <b>Время в чате</b> — растёт само с первого твоего сообщения.\n\n"
    "<i>Считается отдельно в каждом чате: в новой группе всё начинается с нуля. "
    "Короткие реплики вроде «ок» на грамотность не влияют.</i>"
)

NO_STATS = (
    "<b>📊 Статистики пока нет</b>\n"
    "<blockquote>Статистика считается отдельно по каждому чату. Добавь меня в группу "
    "админом и напиши там пару сообщений — считать я умею только то, что вижу сам.</blockquote>"
)


async def stats_text(chat_id: int, user_id: int, name: str, title: str | None = None) -> str:
    stats = await db.battle_stats(chat_id, user_id)
    if not stats["known"]:
        return NO_STATS
    lines, total = [], 0.0
    for key, label, ref, kind in scoring.METRICS:
        score = scoring.metric_score(key, ref, kind, stats)
        total += score
        lines.append(f"{label}: <b>{scoring.value_text(key, kind, stats)}</b> · +{score:.2f}")
    total /= len(scoring.METRICS)
    body = "\n".join(lines)
    where = f" · {html.escape(title)}" if title else ""
    return (
        f"<b>📊 {html.escape(name)}</b>{where}\n"
        f"<blockquote>Общий балл: <b>{total:.2f}</b> из 10</blockquote>\n"
        f"{body}"
    )


@router.message(CommandStart(), F.chat.type == "private")
async def start(message: Message, bot: Bot):
    me = await bot.me()
    await message.answer(MENU_TEXT.format(username=me.username), reply_markup=main_menu(me.username))


@router.callback_query(F.data == "menu:home")
async def menu_home(callback: CallbackQuery, bot: Bot):
    me = await bot.me()
    await callback.message.edit_text(
        MENU_TEXT.format(username=me.username), reply_markup=main_menu(me.username)
    )
    await callback.answer()


@router.callback_query(F.data == "menu:boost")
async def menu_boost(callback: CallbackQuery):
    await callback.message.edit_text(BOOST_TEXT, reply_markup=back_menu())
    await callback.answer()


@router.callback_query(F.data == "menu:stats")
async def menu_stats(callback: CallbackQuery):
    """В личке чата нет, а статистика у каждого своя — сначала выбор чата."""
    chats = await db.user_chats(callback.from_user.id)
    if not chats:
        await callback.message.edit_text(NO_STATS, reply_markup=back_menu())
        await callback.answer()
        return
    kb = InlineKeyboardBuilder()
    for row in chats:
        title = row["title"] or f"Чат {row['chat_id']}"
        kb.button(text=f"{title} · {scoring.format_compact(row['msgs'])}",
                  callback_data=f"stats:{row['chat_id']}")
    kb.button(text="⬅️ Назад", callback_data="menu:home")
    kb.adjust(1)
    await callback.message.edit_text(
        "<b>📊 Твоя статистика</b>\n"
        "<blockquote>Считается отдельно в каждом чате — выбери, какой показать.</blockquote>",
        reply_markup=kb.as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("stats:"))
async def show_chat_stats(callback: CallbackQuery):
    chat_id = int(callback.data.split(":", 1)[1])
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ К списку чатов", callback_data="menu:stats")
    text = await stats_text(chat_id, callback.from_user.id, callback.from_user.full_name,
                            await db.chat_title(chat_id))
    await callback.message.edit_text(text, reply_markup=kb.as_markup())
    await callback.answer()


@router.message(Command("stats"), F.chat.type.in_(GROUPS))
async def cmd_stats(message: Message):
    target = message.reply_to_message.from_user if message.reply_to_message else message.from_user
    await message.reply(
        await stats_text(message.chat.id, target.id, target.full_name, message.chat.title)
    )


@router.message(Command("top"), F.chat.type.in_(GROUPS))
async def cmd_top(message: Message):
    rows = await db.top_chat(message.chat.id)
    if not rows:
        await message.reply("Пока пусто — напишите что-нибудь, и я начну считать.")
        return
    medals = ("🥇", "🥈", "🥉")
    lines = [
        f"{medals[i] if i < 3 else f'{i + 1}.'} {html.escape(row['name'])} — "
        f"<b>{scoring.format_compact(row['msgs'])}</b>"
        for i, row in enumerate(rows)
    ]
    await message.reply("<b>🏆 Топ по сообщениям</b>\n" + "\n".join(lines))


@router.message(Command("battle"), F.chat.type.in_(GROUPS))
async def cmd_battle(message: Message):
    kb = InlineKeyboardBuilder()
    kb.button(text="⚔️ Бросить вызов", switch_inline_query_current_chat="")
    await message.reply(
        "<b>⚔️ Готов к баттлу?</b>\n"
        "<blockquote>Жми кнопку или напиши @ник_бота — вызов уйдёт прямо сюда.</blockquote>",
        reply_markup=kb.as_markup(),
    )


@router.my_chat_member(F.chat.type.in_(GROUPS))
async def added_to_group(event):
    status = event.new_chat_member.status
    if status not in {"member", "administrator"}:
        return
    await event.bot.send_message(
        event.chat.id,
        "<b>⚔️ MOG BATTLE на месте</b>\n"
        "<blockquote>Считаю активность, культурность и грамотность участников — "
        "статистика этого чата своя, с чистого листа.</blockquote>\n"
        "Проверить себя — /stats, рейтинг чата — /top, вызвать кого-то — /battle.",
    )
    if status != "administrator":
        await battle.warn_not_admin(event.bot, event.chat.id)


# Любое непонятое сообщение в личке — обратно в меню. Ловится последним.
@router.message(F.chat.type == "private")
async def fallback(message: Message, bot: Bot):
    me = await bot.me()
    await message.answer(MENU_TEXT.format(username=me.username), reply_markup=main_menu(me.username))


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("Не задана переменная окружения BOT_TOKEN")

    await db.init()
    bot = Bot(token, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    dp.message.outer_middleware(counter)
    dp.include_router(battle.router)
    dp.include_router(router)

    flusher = asyncio.create_task(db.flush_loop())
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        flusher.cancel()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
