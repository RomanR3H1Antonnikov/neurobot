"""
Persistent FSM storage backed by the existing SQLite database.
Replaces the default in-memory MemoryStorage so bot restarts
do not wipe user session state.
"""
import json
import aiosqlite
from typing import Any

from aiogram.fsm.storage.base import BaseStorage, StorageKey


class SQLiteFSMStorage(BaseStorage):
    """Stores aiogram FSM state and data in SQLite tables."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path

    # aiogram calls close() on bot shutdown
    async def close(self) -> None:
        pass

    async def set_state(self, key: StorageKey, state: Any = None) -> None:
        state_str = state.state if hasattr(state, "state") else (state or None)
        async with aiosqlite.connect(self._db_path) as db:
            if state_str is None:
                await db.execute(
                    "DELETE FROM fsm_states WHERE chat_id=? AND user_id=? AND destiny=?",
                    (key.chat_id, key.user_id, key.destiny),
                )
            else:/
                await db.execute(
                    """INSERT INTO fsm_states (chat_id, user_id, destiny, state)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(chat_id, user_id, destiny) DO UPDATE SET state=excluded.state""",
                    (key.chat_id, key.user_id, key.destiny, state_str),
                )
            await db.commit()

    async def get_state(self, key: StorageKey) -> str | None:
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute(
                "SELECT state FROM fsm_states WHERE chat_id=? AND user_id=? AND destiny=?",
                (key.chat_id, key.user_id, key.destiny),
            ) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else None

    async def set_data(self, key: StorageKey, data: dict[str, Any]) -> None:
        payload = json.dumps(data, ensure_ascii=False)
        async with aiosqlite.connect(self._db_path) as db:
            if not data:
                await db.execute(
                    "DELETE FROM fsm_data WHERE chat_id=? AND user_id=? AND destiny=?",
                    (key.chat_id, key.user_id, key.destiny),
                )
            else:
                await db.execute(
                    """INSERT INTO fsm_data (chat_id, user_id, destiny, data)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(chat_id, user_id, destiny) DO UPDATE SET data=excluded.data""",
                    (key.chat_id, key.user_id, key.destiny, payload),
                )
            await db.commit()

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute(
                "SELECT data FROM fsm_data WHERE chat_id=? AND user_id=? AND destiny=?",
                (key.chat_id, key.user_id, key.destiny),
            ) as cursor:
                row = await cursor.fetchone()
                return json.loads(row[0]) if row else {}
