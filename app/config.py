import os
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', 'env', '.env'))


class Config:
    NALOG_INN: str = os.getenv('NALOG_INN', '')
    NALOG_PASSWORD: str = os.getenv('NALOG_PASSWORD', '')

    DATABASE_URL: str = os.getenv('DATABASE_URL', '')

    TIMER: int = int(os.getenv('TIMER', '60'))
    SEND_DELAY: int = int(os.getenv('SEND_DELAY', '10'))

    RECEIPT_NAME: str = 'Оплата подписки для доступа к телеграм боту'

    RETRY_COUNT: int = 3
    RETRY_DELAY: int = 7  # секунд между попытками

    def validate(self) -> None:
        missing = [
            k for k in ('NALOG_INN', 'NALOG_PASSWORD', 'DATABASE_URL')
            if not getattr(self, k)
        ]
        if missing:
            raise ValueError(f'Не заданы обязательные переменные: {", ".join(missing)}')


config = Config()
