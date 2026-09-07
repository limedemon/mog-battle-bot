"""Хранилище статистики: SQLite в data/ рядом с кодом (bothost — обычная папка).

Статистика считается отдельно по каждому чату: активность в одной группе не
переносится в другую. Ключ везде — пара (chat_id, user_id).

Писать в базу на каждое сообщение чата нельзя: в живой группе это сотни
транзакций в минуту. Всё копится в памяти и сбрасывается пачкой раз в
FLUSH_EVERY секунд, а также при остановке бота.
"""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite

DB_PATH = Path(__file__).with_name("data") / "stats.db"
FLUSH_EVERY = 5.0
MSK = timezone(timedelta(hours=3))
CHALLENGE_TTL = 24 * 60 * 60
WARN_EVERY = 60 * 60

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id  INTEGER PRIMARY KEY,
    name     TEXT NOT NULL DEFAULT '',
    username TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS chats (
    chat_id   INTEGER PRIMARY KEY,
    title     TEXT NOT NULL DEFAULT '',
    warned_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS stats (
    chat_id    INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    first_seen REAL    NOT NULL DEFAULT 0,
    last_seen  REAL    NOT NULL DEFAULT 0,
    msgs       INTEGER NOT NULL DEFAULT 0,
    words      INTEGER NOT NULL DEFAULT 0,
    obscene    INTEGER NOT NULL DEFAULT 0,
    insults    INTEGER NOT NULL DEFAULT 0,
    dirty      INTEGER NOT NULL DEFAULT 0,
    lit_sum    REAL    NOT NULL DEFAULT 0,
    lit_msgs   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (chat_id, user_id)
);
CREATE TABLE IF NOT EXISTS daily (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    day     TEXT    NOT NULL,
    msgs    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (chat_id, user_id, day)
);
CREATE TABLE IF NOT EXISTS challenges (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    name       TEXT NOT NULL,
    username   TEXT NOT NULL,
    target     TEXT,
    mark       INTEGER NOT NULL DEFAULT 0,
    chat_id    INTEGER,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS challenges_owner ON challenges (user_id, created_at);
"""

_db: aiosqlite.Connection | None = None
_buffer: dict[tuple[int, int], dict] = {}
_daily: dict[tuple[int, int, str], int] = defaultdict(int)
_titles: dict[int, str] = {}
_lock = asyncio.Lock()


def today() -> str:
    return datetime.now(MSK).strftime("%Y-%m-%d")


def days_back(count: int) -> str:
    return (datetime.now(MSK) - timedelta(days=count - 1)).strftime("%Y-%m-%d")


async def init() -> None:
    global _db
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    _db = await aiosqlite.connect(DB_PATH)
    _db.row_factory = aiosqlite.Row
    await _db.executescript(SCHEMA)
    # WAL — чтобы пачка записей не блокировала чтение статистики.
    await _db.execute("PRAGMA journal_mode=WAL")
    await _db.commit()


async def close() -> None:
    await flush()
    if _db is not None:
        await _db.close()


def track(user, chat, parsed: dict) -> None:
    """Накопить сообщение в буфер. Синхронно и без базы — вызывается на каждое
    сообщение группы."""
    now = time.time()
    key = (chat.id, user.id)
    slot = _buffer.get(key)
    if slot is None:
        slot = _buffer[key] = {
            "msgs": 0, "words": 0, "obscene": 0, "insults": 0, "dirty": 0,
            "lit_sum": 0.0, "lit_msgs": 0, "first_seen": now, "last_seen": now,
            "name": user.full_name, "username": user.username or "",
        }
    slot["msgs"] += 1
    slot["words"] += parsed["words"]
    slot["obscene"] += parsed["obscene"]
    slot["insults"] += parsed["insults"]
    slot["dirty"] += parsed["dirty"]
    if parsed["literacy"] is not None:
        slot["lit_sum"] += parsed["literacy"]
        slot["lit_msgs"] += 1
    slot["last_seen"] = now
    slot["name"] = user.full_name
    slot["username"] = user.username or ""
    _daily[(chat.id, user.id, today())] += 1
    _titles[chat.id] = chat.title or ""


async def flush() -> None:
    """Сбросить буфер в базу одной транзакцией."""
    if _db is None or not (_buffer or _daily or _titles):
        return
    async with _lock:
        rows, daily, titles = dict(_buffer), dict(_daily), dict(_titles)
        _buffer.clear()
        _daily.clear()
        _titles.clear()
        for chat_id, title in titles.items():
            await _db.execute(
                "INSERT INTO chats (chat_id, title) VALUES (?, ?) "
                "ON CONFLICT(chat_id) DO UPDATE SET title = excluded.title",
                (chat_id, title),
            )
        for (chat_id, user_id), slot in rows.items():
            await _db.execute(
                "INSERT INTO users (user_id, name, username) VALUES (?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET name = excluded.name, "
                "username = excluded.username",
                (user_id, slot["name"], slot["username"]),
            )
            await _db.execute(
                """
                INSERT INTO stats (chat_id, user_id, first_seen, last_seen, msgs, words,
                                   obscene, insults, dirty, lit_sum, lit_msgs)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, user_id) DO UPDATE SET
                    last_seen = excluded.last_seen,
                    msgs = stats.msgs + excluded.msgs,
                    words = stats.words + excluded.words,
                    obscene = stats.obscene + excluded.obscene,
                    insults = stats.insults + excluded.insults,
                    dirty = stats.dirty + excluded.dirty,
                    lit_sum = stats.lit_sum + excluded.lit_sum,
                    lit_msgs = stats.lit_msgs + excluded.lit_msgs
                """,
                (chat_id, user_id, slot["first_seen"], slot["last_seen"], slot["msgs"],
                 slot["words"], slot["obscene"], slot["insults"], slot["dirty"],
                 slot["lit_sum"], slot["lit_msgs"]),
            )
        for (chat_id, user_id, day), count in daily.items():
            await _db.execute(
                "INSERT INTO daily (chat_id, user_id, day, msgs) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(chat_id, user_id, day) DO UPDATE SET msgs = daily.msgs + excluded.msgs",
                (chat_id, user_id, day, count),
            )
        await _db.commit()


async def flush_loop() -> None:
    while True:
        await asyncio.sleep(FLUSH_EVERY)
        try:
            await flush()
        except Exception as error:  # база не должна ронять бота
            print(f"[db] не удалось сбросить буфер: {error}")


async def battle_stats(chat_id: int, user_id: int) -> dict:
    """Показатели игрока в конкретном чате."""
    await flush()
    cur = await _db.execute(
        "SELECT * FROM stats WHERE chat_id = ? AND user_id = ?", (chat_id, user_id)
    )
    row = await cur.fetchone()
    if row is None:
        return {
            "msgs_today": 0, "msgs_week": 0, "msgs_month": 0, "msgs_total": 0,
            "age": 0, "culture": None, "literacy": None, "known": False,
        }

    async def since(days: int) -> int:
        cursor = await _db.execute(
            "SELECT COALESCE(SUM(msgs), 0) FROM daily "
            "WHERE chat_id = ? AND user_id = ? AND day >= ?",
            (chat_id, user_id, days_back(days)),
        )
        return int((await cursor.fetchone())[0] or 0)

    total = int(row["msgs"] or 0)
    first_seen = float(row["first_seen"] or 0)
    # Меньше 15 сообщений — судить о культуре и грамотности не по чему,
    # такому ставим прочерк и нейтральный балл, а не ноль.
    if total >= 15:
        dirty_rate = (int(row["obscene"]) + 2 * int(row["insults"])) / total
        culture = max(0.0, 1.0 - dirty_rate / 0.12)
        literacy = (row["lit_sum"] / row["lit_msgs"]) if row["lit_msgs"] else None
    else:
        culture = literacy = None

    return {
        "msgs_today": await since(1),
        "msgs_week": await since(7),
        "msgs_month": await since(30),
        "msgs_total": total,
        "age": int(time.time() - first_seen) if first_seen else 0,
        "culture": culture,
        "literacy": literacy,
        "known": True,
    }


async def top_chat(chat_id: int, limit: int = 10):
    await flush()
    cur = await _db.execute(
        """
        SELECT u.name, s.msgs FROM stats s JOIN users u ON u.user_id = s.user_id
        WHERE s.chat_id = ? ORDER BY s.msgs DESC LIMIT ?
        """,
        (chat_id, limit),
    )
    return await cur.fetchall()


async def user_chats(user_id: int, limit: int = 8):
    """Чаты, где у человека есть статистика — для выбора в личке."""
    await flush()
    cur = await _db.execute(
        """
        SELECT s.chat_id, s.msgs, c.title FROM stats s
        LEFT JOIN chats c ON c.chat_id = s.chat_id
        WHERE s.user_id = ? ORDER BY s.msgs DESC LIMIT ?
        """,
        (user_id, limit),
    )
    return await cur.fetchall()


async def chat_title(chat_id: int) -> str:
    cur = await _db.execute("SELECT title FROM chats WHERE chat_id = ?", (chat_id,))
    row = await cur.fetchone()
    return (row["title"] if row else "") or "этот чат"


async def should_warn(chat_id: int) -> bool:
    """Напоминание о правах — не чаще раза в час на чат, чтобы не спамить."""
    cur = await _db.execute("SELECT warned_at FROM chats WHERE chat_id = ?", (chat_id,))
    row = await cur.fetchone()
    if row is not None and time.time() - float(row["warned_at"] or 0) < WARN_EVERY:
        return False
    await _db.execute(
        "INSERT INTO chats (chat_id, warned_at) VALUES (?, ?) "
        "ON CONFLICT(chat_id) DO UPDATE SET warned_at = excluded.warned_at",
        (chat_id, time.time()),
    )
    await _db.commit()
    return True


async def save_challenge(token: str, user, target: str | None, mark: int) -> None:
    """Запоминаем, кто бросил вызов: в callback_data имя не влезает, а на
    карточке хочется показать настоящее имя и ник, а не заглушку."""
    await _db.execute("DELETE FROM challenges WHERE created_at < ?", (time.time() - CHALLENGE_TTL,))
    await _db.execute(
        "INSERT INTO challenges (token, user_id, name, username, target, mark, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(token) DO UPDATE SET created_at = excluded.created_at",
        (token, user.id, user.full_name, f"@{user.username}" if user.username else "без ника",
         target, mark, time.time()),
    )
    await _db.commit()


async def get_challenge(token: str):
    cur = await _db.execute("SELECT * FROM challenges WHERE token = ?", (token,))
    return await cur.fetchone()


async def bind_challenge(user_id: int, chat_id: int, mark: int | None) -> None:
    """Привязать вызов к чату, в котором он опубликован.

    Инлайн-запрос не сообщает чат, зато бот видит опубликованное сообщение как
    обычное — если он в этом чате состоит. Метку берём из невидимых символов в
    подписи; если её вдруг срезали, привязываем свежайший непривязанный вызов
    этого игрока.
    """
    if mark is not None:
        await _db.execute(
            "UPDATE challenges SET chat_id = ? WHERE user_id = ? AND mark = ? AND chat_id IS NULL",
            (chat_id, user_id, mark),
        )
    else:
        await _db.execute(
            """
            UPDATE challenges SET chat_id = ? WHERE token = (
                SELECT token FROM challenges WHERE user_id = ? AND chat_id IS NULL
                ORDER BY created_at DESC LIMIT 1
            )
            """,
            (chat_id, user_id),
        )
    await _db.commit()
