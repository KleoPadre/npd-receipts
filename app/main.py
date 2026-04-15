import asyncio
import signal

from app.config import config
from app.db import close_pool, init_db
from app.logger import logger
from app.processor import run_cycle

_shutdown = False


def _on_signal(sig, _frame):
    global _shutdown
    logger.info(f'Signal {sig} received — shutting down after current cycle')
    _shutdown = True


async def main():
    global _shutdown

    config.validate()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    logger.info('=== npd-receipts worker started ===')
    logger.info(
        f'INN: {config.NALOG_INN} | '
        f'Interval: {config.TIMER} min | '
        f'Send delay: {config.SEND_DELAY}s | '
        f'Retries: {config.RETRY_COUNT}'
    )

    await init_db()

    while not _shutdown:
        try:
            await run_cycle()
        except Exception as exc:
            logger.exception(f'Unexpected error in cycle: {exc}')

        if _shutdown:
            break

        logger.info(f'Sleeping {config.TIMER} minute(s) until next cycle ...')
        # Спим по 1 секунде — чтобы SIGTERM обрабатывался быстро
        for _ in range(config.TIMER * 60):
            if _shutdown:
                break
            await asyncio.sleep(1)

    await close_pool()
    logger.info('=== npd-receipts worker stopped ===')


if __name__ == '__main__':
    asyncio.run(main())
