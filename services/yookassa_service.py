"""
Прямой API ЮKassa — используется для способа оплаты "по ссылке" (карта, СБП и др.),
в дополнение к оплате через встроенное окно Telegram (bot/handlers/billing.py, provider_token).

Статус платежа всегда перепроверяется напрямую у ЮKassa через get_payment() —
телу вебхука не доверяем, иначе его можно подделать без секретного ключа.
"""
import base64
import logging
import uuid
import aiohttp

from config import config

logger = logging.getLogger(__name__)

API_BASE = "https://api.yookassa.ru/v3"


def _auth_header() -> dict:
    raw = f"{config.yookassa_shop_id}:{config.yookassa_secret_key}".encode()
    return {"Authorization": "Basic " + base64.b64encode(raw).decode()}


async def create_payment(amount_rub: int, description: str, telegram_id: int, chat_id: int, return_url: str) -> dict:
    """Создаёт платёж в ЮKassa. Возвращает объект платежа (включая id и confirmation.confirmation_url)."""
    payload = {
        "amount": {"value": f"{amount_rub:.2f}", "currency": "RUB"},
        "capture": True,
        "confirmation": {"type": "redirect", "return_url": return_url},
        "description": description,
        "metadata": {"telegram_id": str(telegram_id), "chat_id": str(chat_id)},
    }
    headers = {
        **_auth_header(),
        "Idempotence-Key": str(uuid.uuid4()),
        "Content-Type": "application/json",
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{API_BASE}/payments", json=payload, headers=headers,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            body = await resp.json()
            if resp.status >= 400:
                logger.error("YooKassa create_payment failed: status=%s body=%s", resp.status, body)
                raise RuntimeError(body.get("description") or f"YooKassa HTTP {resp.status}")
            return body


async def get_payment(payment_id: str) -> dict:
    """Запрашивает актуальный статус платежа напрямую у ЮKassa."""
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{API_BASE}/payments/{payment_id}", headers=_auth_header(),
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            body = await resp.json()
            if resp.status >= 400:
                logger.error("YooKassa get_payment failed: status=%s body=%s", resp.status, body)
                raise RuntimeError(body.get("description") or f"YooKassa HTTP {resp.status}")
            return body


async def confirm_and_credit(payment_id: str, bot=None) -> tuple[str, int | None]:
    """Если ЮKassa подтверждает, что платёж оплачен — зачисляет кредиты (ровно один раз).

    Вызывается и из вебхука, и из кнопки "Проверить оплату" — обе ветки безопасны
    при одновременном срабатывании благодаря mark_yookassa_payment_succeeded().

    Возвращает (status, balance):
      "unknown"          — платёж не найден в нашей БД
      "not_paid"         — ЮKassa ещё не подтвердила оплату
      "already_credited" — кредиты уже были зачислены раньше
      "paid_now"         — кредиты зачислены именно этим вызовом
    """
    from db.queries import (
        get_yookassa_payment, mark_yookassa_payment_succeeded,
        get_or_create_user, add_credits, get_balance,
    )

    row = await get_yookassa_payment(payment_id)
    if not row:
        return "unknown", None

    if row["status"] == "succeeded":
        return "already_credited", await get_balance(row["telegram_id"])

    payment = await get_payment(payment_id)
    if payment.get("status") != "succeeded":
        return "not_paid", None

    if not await mark_yookassa_payment_succeeded(payment_id):
        return "already_credited", await get_balance(row["telegram_id"])

    user = await get_or_create_user(row["telegram_id"])
    new_balance = await add_credits(user["id"], row["amount"], description="Пополнение через ЮKassa (ссылка)")

    if bot is not None:
        try:
            await bot.send_message(
                row["chat_id"],
                f"✅ <b>Баланс пополнен!</b>\n\n"
                f"Зачислено: <b>{row['amount']} ₽</b>\n"
                f"Текущий баланс: <b>{new_balance} ₽</b>",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error("YooKassa: не удалось уведомить telegram_id=%s: %s", row["telegram_id"], e)

    return "paid_now", new_balance
