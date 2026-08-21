import aiosqlite
from config import config

_db: aiosqlite.Connection | None = None


async def get_db() -> aiosqlite.Connection:
    global _db
    if _db is None:
        _db = await aiosqlite.connect(config.db_path)
        _db.row_factory = aiosqlite.Row
        await _create_tables(_db)
    return _db


async def close_db() -> None:
    global _db
    if _db:
        await _db.close()
        _db = None


async def _create_tables(db: aiosqlite.Connection) -> None:
    await db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER UNIQUE NOT NULL,
            username    TEXT,
            balance     INTEGER NOT NULL DEFAULT 0,
            created_at  INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
        );

        CREATE TABLE IF NOT EXISTS transactions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL REFERENCES users(id),
            amount      INTEGER NOT NULL,
            type        TEXT NOT NULL,  -- 'topup' | 'spend'
            task_type   TEXT,
            description TEXT,
            created_at  INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
        );

        CREATE TABLE IF NOT EXISTS chat_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL REFERENCES users(id),
            role        TEXT NOT NULL,  -- 'user' | 'assistant'
            content     TEXT NOT NULL,
            created_at  INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
        );

        CREATE TABLE IF NOT EXISTS rate_limits (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      INTEGER NOT NULL REFERENCES users(id),
            task_type    TEXT NOT NULL,
            window_start INTEGER NOT NULL,
            count        INTEGER NOT NULL DEFAULT 1,
            UNIQUE(user_id, task_type, window_start)
        );
    """)
    await db.commit()
