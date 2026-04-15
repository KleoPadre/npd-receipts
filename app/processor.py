"""
Бизнес-логика одного цикла обработки платежей.

Алгоритм:
  1. Подобрать зависшие записи (status IS NULL) — на случай краша.
  2. Найти новые succeeded-платежи, которых нет в tax.
  3. Зарезервировать новые (INSERT с status=NULL) — защита от дублей.
  4. Обработать все: pending первыми, затем новые.
  5. Между платежами — пауза SEND_DELAY секунд.
"""

import asyncio

from app import db
from app.config import config
from app.logger import logger
from app.nalog_client import nalog_client


async def process_payment(payment_id: str, amount) -> bool:
    """
    Отправляет один чек. Возвращает True при успехе.
    В случае ошибки записывает её текст в БД и возвращает False.
    """
    try:
        receipt_uuid = await nalog_client.send_receipt(payment_id, amount)
        await db.mark_success(payment_id)
        logger.info(f'Payment {payment_id} SUCCESS (receipt={receipt_uuid})')
        return True

    except Exception as exc:
        error_text = str(exc)
        await db.mark_error(payment_id, error_text)
        logger.error(f'Payment {payment_id} ERROR: {error_text}')
        return False


async def run_cycle() -> None:
    """Один полный цикл: найти → зарезервировать → обработать."""

    # 1. Зависшие записи (упали в середине прошлого цикла)
    pending = await db.fetch_pending()
    if pending:
        logger.info(f'Resuming {len(pending)} previously pending payment(s)')

    # 2. Ошибочные записи (все попытки исчерпаны в предыдущих циклах)
    failed = await db.fetch_failed()
    if failed:
        logger.info(f'Retrying {len(failed)} previously failed payment(s)')

    # 3. Новые платежи
    new_payments = await db.fetch_new_payments()
    logger.info(f'Found {len(new_payments)} new payment(s)')

    # 4. Резервируем новые — сразу все, чтобы не было дублей при параллельном запуске
    for row in new_payments:
        await db.insert_pending(row['payment_id'])

    # 5. Обрабатываем: pending → failed → new
    queue = list(pending) + list(failed) + list(new_payments)

    if not queue:
        logger.info('Cycle done: nothing to process')
        return

    success = 0
    errors = 0

    for i, row in enumerate(queue):
        payment_id = str(row['payment_id'])
        amount = row['amount']

        ok = await process_payment(payment_id, amount)
        if ok:
            success += 1
        else:
            errors += 1

        # Пауза между отправками (кроме последнего)
        if i < len(queue) - 1:
            await asyncio.sleep(config.SEND_DELAY)

    logger.info(
        f'Cycle done: {success} success, {errors} error(s) '
        f'out of {len(queue)} total'
    )
