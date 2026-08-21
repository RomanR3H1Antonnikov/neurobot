import time
from typing import Optional
from aiosqlite import Row
from db.database import get_db


# ─── Пользователи ────────────────────────────────────────────────────────────

async def get_or_create_user(telegram_id: int, username: Optional[str] = None) -> Row:
    db = await get_db()
    await db.execute(
        "INSERT OR IGNORE INTO users (telegram_id, username) VALUES (?, ?)",
        (telegram_id, username),
    )
    await db.commit()
    async with db.execute(
        "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
    ) as cur:
        return await cur.fetchone()


async def get_user(telegram_id: int) -> Optional[Row]:
    db = await get_db()
    async with db.execute(
        "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
    ) as cur:
        return await cur.fetchone()


# ─── Баланс ───────────────────────────────────────────────────────────────────

async def get_balance(telegram_id: int) -> int:
    user = await get_user(telegram_id)
    return user["balance"] if user else 0


async def deduct_credits(user_id: int, amount: int, task_type: str) -> bool:
    """Атомарно списывает кредиты. Возвращает False если недостаточно средств."""
    db = await get_db()
    async with db.execute(
        "UPDATE users SET balance = balance - ? WHERE id = ? AND balance >= ? RETURNING id",
        (amount, user_id, amount),
    ) as cur:
        row = await cur.fetchone()
    if not row:
        return False
    await db.execute(
        "INSERT INTO transactions (user_id, amount, type, task_type, description) VALUES (?, ?, 'spend', ?, ?)",
        (user_id, amount, task_type, f"Генерация: {task_type}"),
    )
    await db.commit()
    return True


async def add_credits(user_id: int, amount: int, description: str = "Пополнение") -> int:
    """Добавляет кредиты, возвращает новый баланс."""
    db = await get_db()
    await db.execute(
        "UPDATE users SET balance = balance + ? WHERE id = ?",
        (amount, user_id),
    )
    await db.execute(
        "INSERT INTO transactions (user_id, amount, type, description) VALUES (?, ?, 'topup', ?)",
        (user_id, amount, description),
    )
    await db.commit()
    async with db.execute("SELECT balance FROM users WHERE id = ?", (user_id,)) as cur:
        row = await cur.fetchone()
    return row["balance"]


# ─── История чата ─────────────────────────────────────────────────────────────

CHAT_HISTORY_LIMIT = 20  # последних сообщений в контексте


async def get_chat_history(user_id: int) -> list[dict]:
    db = await get_db()
    async with db.execute(
        """SELECT role, content FROM chat_history
           WHERE user_id = ?
           ORDER BY created_at DESC LIMIT ?""",
        (user_id, CHAT_HISTORY_LIMIT),
    ) as cur:
        rows = await cur.fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


async def add_chat_message(user_id: int, role: str, content: str) -> None:
    db = await get_db()
    await db.execute(
        "INSERT INTO chat_history (user_id, role, content) VALUES (?, ?, ?)",
        (user_id, role, content),
    )
    await db.commit()


async def clear_chat_history(user_id: int) -> None:
    db = await get_db()
    await db.execute("DELETE FROM chat_history WHERE user_id = ?", (user_id,))
    await db.commit()


# ─── Лимиты ───────────────────────────────────────────────────────────────────

HOUR = 3600


async def check_and_increment_rate_limit(user_id: int, task_type: str, limit: int) -> bool:
    """Проверяет лимит и инкрементирует счётчик. False = лимит превышен."""
    db = await get_db()
    window = int(time.time()) // HOUR * HOUR  # начало текущего часового окна

    await db.execute(
        """INSERT INTO rate_limits (user_id, task_type, window_start, count)
           VALUES (?, ?, ?, 1)
           ON CONFLICT(user_id, task_type, window_start)
           DO UPDATE SET count = count + 1""",
        (user_id, task_type, window),
    )
    await db.commit()

    async with db.execute(
        "SELECT count FROM rate_limits WHERE user_id = ? AND task_type = ? AND window_start = ?",
        (user_id, task_type, window),
    ) as cur:
        row = await cur.fetchone()

    return row["count"] <= limit
