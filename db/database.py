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

        CREATE TABLE IF NOT EXISTS generations (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            media_type  TEXT NOT NULL,
            file_id     TEXT NOT NULL,
            prompt      TEXT,
            model_label TEXT,
            created_at  INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
        );

        CREATE INDEX IF NOT EXISTS idx_generations_user_type
            ON generations (telegram_id, media_type, created_at DESC);

        CREATE TABLE IF NOT EXISTS pending_jobs (
            corr_id     TEXT PRIMARY KEY,
            telegram_id INTEGER NOT NULL,
            chat_id     INTEGER NOT NULL,
            model_label TEXT NOT NULL DEFAULT '',
            media_type  TEXT NOT NULL DEFAULT 'video',
            prompt      TEXT,
            created_at  INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
        );

        CREATE TABLE IF NOT EXISTS fsm_states (
            chat_id  INTEGER NOT NULL,
            user_id  INTEGER NOT NULL,
            destiny  TEXT NOT NULL DEFAULT 'fsm',
            state    TEXT NOT NULL,
            PRIMARY KEY (chat_id, user_id, destiny)
        );

        CREATE TABLE IF NOT EXISTS fsm_data (
            chat_id  INTEGER NOT NULL,
            user_id  INTEGER NOT NULL,
            destiny  TEXT NOT NULL DEFAULT 'fsm',
            data     TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY (chat_id, user_id, destiny)
        );
    """)
    await db.commit()
