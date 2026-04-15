import logging
import os
import sys
from datetime import datetime, timezone, timedelta
from logging.handlers import RotatingFileHandler

LOG_DIR = os.path.join(os.path.dirname(__file__), '..', 'logs')
LOG_FILE = os.path.join(LOG_DIR, 'npd-receipts.log')

os.makedirs(LOG_DIR, exist_ok=True)

_MSK = timezone(timedelta(hours=3))


class _MskFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, tz=_MSK)
        return dt.strftime(datefmt or '%Y-%m-%d %H:%M:%S')


_fmt = _MskFormatter(
    fmt='[%(asctime)s] %(levelname)-8s %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)


def _handler(h: logging.Handler) -> logging.Handler:
    h.setFormatter(_fmt)
    h.setLevel(logging.DEBUG)
    return h


logger = logging.getLogger('npd-receipts')
logger.setLevel(logging.DEBUG)
logger.addHandler(_handler(logging.StreamHandler(sys.stdout)))
logger.addHandler(_handler(
    RotatingFileHandler(
        LOG_FILE,
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5,
        encoding='utf-8',
    )
))
