"""
Работа с PostgreSQL через asyncpg.

Таблица payments — только читается (SELECT).
Таблица tax — создаётся и управляется этим сервисом.

Схема tax:
    id           SERIAL PRIMARY KEY
    payment_id   TEXT NOT NULL UNIQUE   — invoice_id из payments
    status       BOOLEAN                — NULL=в обработке, TRUE=успех, FALSE=ошибка
    error_text   TEXT
    created_at   TIMESTAMP DEFAULT NOW()
    processed_at TIMESTAMP
"""

from typing import Optional

import asyncpg

from app.config import config
from app.logger import logger

_pool: Optional[asyncpg.Pool] = None

# ─── SQL ──────────────────────────────────────────────────────────────────────

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS tax (
    id           SERIAL PRIMARY KEY,
    payment_id   TEXT NOT NULL,
    status       BOOLEAN,
    error_text   TEXT,
    created_at   TIMESTAMP DEFAULT NOW(),
    processed_at TIMESTAMP
);
"""

_CREATE_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS tax_payment_id_idx ON tax (payment_id);
"""

_FETCH_NEW = """
    SELECT p.invoice_id AS payment_id, p.amount_rub AS amount
    FROM payments p
    WHERE p.status = 'succeeded'
      AND NOT EXISTS (
          SELECT 1 FROM tax t WHERE t.payment_id = p.invoice_id
      )
    ORDER BY p.created_at;
"""

_FETCH_PENDING = """
    SELECT t.payment_id, p.amount_rub AS amount
    FROM tax t
    JOIN payments p ON p.invoice_id = t.payment_id
    WHERE t.status IS NULL
    ORDER BY t.id;
"""

_FETCH_FAILED = """
    SELECT t.payment_id, p.amount_rub AS amount
    FROM tax t
    JOIN payments p ON p.invoice_id = t.payment_id
    WHERE t.status = FALSE
    ORDER BY t.id;
"""


# ─── Пул соединений ───────────────────────────────────────────────────────────

async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            config.DATABASE_URL,
            min_size=1,
            max_size=5,
        )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


# ─── Инициализация ────────────────────────────────────────────────────────────

async def init_db() -> None:
    """Создаёт таблицу tax и индекс если их нет."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(_CREATE_TABLE)
        await conn.execute(_CREATE_INDEX)
    logger.info('База данных инициализирована (таблица tax готова)')


# ─── Чтение данных ────────────────────────────────────────────────────────────

async def fetch_new_payments() -> list[asyncpg.Record]:
    """Возвращает платежи со статусом 'succeeded', ещё не попавшие в tax."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch(_FETCH_NEW)


async def fetch_pending() -> list[asyncpg.Record]:
    """Возвращает записи tax с status IS NULL (зарезервированы, но не обработаны)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch(_FETCH_PENDING)


async def fetch_failed() -> list[asyncpg.Record]:
    """Возвращает записи tax с status = FALSE (все попытки исчерпаны в прошлых циклах)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch(_FETCH_FAILED)


# ─── Запись результатов ───────────────────────────────────────────────────────

async def insert_pending(payment_id: str) -> None:
    """
    Резервирует слот в tax (status = NULL) — идемпотентно.
    Защита от дублей: если запись уже есть — ничего не делает.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO tax (payment_id)
            VALUES ($1)
            ON CONFLICT (payment_id) DO NOTHING
            """,
            payment_id,
        )


async def mark_success(payment_id: str) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE tax
            SET status = TRUE, processed_at = NOW(), error_text = NULL
            WHERE payment_id = $1
            """,
            payment_id,
        )


async def mark_error(payment_id: str, error_text: str) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE tax
            SET status = FALSE, processed_at = NOW(), error_text = $2
            WHERE payment_id = $1
            """,
            payment_id,
            error_text,
        )
