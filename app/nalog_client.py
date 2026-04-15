"""
Собственная реализация HTTP-клиента к lknpd.nalog.ru для самозанятых.

Без каких-либо сторонних обёрток — только прямые запросы к API.
Протокол задокументирован в открытых источниках (habr.com/436656,
github.com/shoman4eg/moy-nalog, github.com/alexstep/moy-nalog).

Используемые эндпоинты:
  POST /api/v1/auth/lkfl     — вход по ИНН + паролю → token + refreshToken
                               (устаревший /auth/inn возвращает 404 с ~2025 г.)
  POST /api/v1/auth/token    — обновление токена по refreshToken
  POST /api/v1/income        — регистрация дохода (чек) → approvedReceiptUuid

Управление токеном:
  1. При старте вызывается login() — полная авторизация по ИНН + паролю.
  2. accessToken действителен ~10 часов. Сохраняется в памяти.
  3. При ошибке 401/403 — пробуем refresh через refreshToken.
  4. Если refresh не помог — повторный полный login().
  5. Токен привязан к Device ID и IP-адресу контейнера, поэтому
     Device ID фиксирован на всё время жизни процесса.
"""

import asyncio
import hashlib
import random
import string
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Optional

import httpx

from app.config import config
from app.logger import logger

# ─── Константы ────────────────────────────────────────────────────────────────

BASE_URL = "https://lknpd.nalog.ru/api/v1"

# Часовой пояс для меток времени в чеках.
# МСК (+3) подходит для большинства пользователей РФ.
# Если вы в другом поясе — измените здесь.
TZ_OFFSET_HOURS = 3
TZ_STR = "+03:00"

# Имитируем Android-клиент — именно такой useragent ожидает API.
APP_VERSION = "2.63.0"
SOURCE_TYPE = "ANDROID"
DEVICE_OS = "Android"


# ─── Вспомогательные функции ──────────────────────────────────────────────────

def _random_device_id() -> str:
    """
    Генерирует случайный 21-символьный Device ID.
    Формат совпадает с тем, что генерирует Android-приложение.
    ВАЖНО: токен привязывается к Device ID — не меняйте его между запросами.
    """
    chars = string.ascii_letters + string.digits
    return "".join(random.choices(chars, k=21))


def _now_local() -> str:
    """Текущее время в формате ISO 8601 с оффсетом МСК."""
    tz = timezone(timedelta(hours=TZ_OFFSET_HOURS))
    dt = datetime.now(tz)
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + TZ_STR


def _device_info(device_id: str) -> dict:
    """Объект deviceInfo — обязателен в запросах авторизации."""
    return {
        "sourceDeviceId": device_id,
        "sourceType": SOURCE_TYPE,
        "appVersion": APP_VERSION,
        "metaDetails": {
            "userAgent": f"okhttp/{APP_VERSION}",
        },
    }


# ─── HTTP-клиент к lknpd.nalog.ru ─────────────────────────────────────────────

class LknpdHttpClient:
    """
    Низкоуровневый HTTP-клиент.
    Занимается только сетью: авторизация + регистрация дохода.
    Retry-логика и управление состоянием — в NalogClient выше.
    """

    def __init__(self) -> None:
        self._inn: str = config.NALOG_INN
        self._password: str = config.NALOG_PASSWORD

        # Device ID фиксируем на старте процесса — токен к нему привязан.
        self._device_id: str = _random_device_id()

        self._token: Optional[str] = None
        self._refresh_token: Optional[str] = None

        self._http: Optional[httpx.AsyncClient] = None

    # ── httpx session ─────────────────────────────────────────────────────────

    def _session(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                base_url=BASE_URL,
                timeout=30.0,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/plain, */*",
                    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
                    "Device-Id": self._device_id,
                    "Device-Os": DEVICE_OS,
                    "App-Version": APP_VERSION,
                    "User-Agent": f"okhttp/{APP_VERSION}",
                    "Referrer": "https://lknpd.nalog.ru/",
                },
            )
        return self._http

    def _auth_header(self) -> dict:
        if not self._token:
            raise RuntimeError("Клиент не авторизован (token отсутствует)")
        return {"Authorization": f"Bearer {self._token}"}

    async def _post(
        self,
        path: str,
        body: dict,
        *,
        auth: bool = False,
    ) -> dict:
        headers = self._auth_header() if auth else {}
        resp = await self._session().post(path, json=body, headers=headers)
        resp.raise_for_status()
        return resp.json()

    # ── Авторизация ───────────────────────────────────────────────────────────

    async def login(self) -> None:
        """
        Полная авторизация по ИНН + паролю.

        POST /api/v1/auth/lkfl
        Тело: { "username": "...", "password": "...", "deviceInfo": {...} }
        Ответ: { "token": "...", "refreshToken": "...", "tokenExpireIn": 36000, ... }

        Примечание: эндпоинт /auth/inn устарел и возвращает 404.
        Актуальный эндпоинт — /auth/lkfl, поле inn переименовано в username.
        """
        body = {
            "username": self._inn,
            "password": self._password,
            "deviceInfo": _device_info(self._device_id),
        }
        data = await self._post("/auth/lkfl", body)

        self._token = data["token"]
        self._refresh_token = data.get("refreshToken", "")
        expire_in = data.get("tokenExpireIn", 36000)

        logger.info(
            f"lknpd: авторизация выполнена | "
            f"device_id={self._device_id[:6]}… | "
            f"token expires in {expire_in}s"
        )

    async def refresh(self) -> bool:
        """
        Обновление accessToken через refreshToken (без повторного ввода пароля).

        POST /api/v1/auth/token
        Тело: { "refreshToken": "...", "deviceInfo": {...} }
        Ответ: { "token": "...", "refreshToken": "...", ... }

        Возвращает True при успехе, False при любой ошибке.

        ВНИМАНИЕ: согласно документации shoman4eg/moy-nalog, токен привязан
        к IP и Device ID. Refresh работает только если запрос идёт с того же
        окружения (IP контейнера), где был выполнен login().
        """
        if not self._refresh_token:
            logger.warning("lknpd: refresh_token отсутствует, refresh невозможен")
            return False

        try:
            body = {
                "refreshToken": self._refresh_token,
                "deviceInfo": _device_info(self._device_id),
            }
            data = await self._post("/auth/token", body)

            self._token = data["token"]
            # Сервер может вернуть новый refreshToken
            self._refresh_token = data.get("refreshToken", self._refresh_token)
            logger.info("lknpd: токен успешно обновлён через refreshToken")
            return True

        except httpx.HTTPStatusError as exc:
            logger.warning(
                f"lknpd: refresh не удался (HTTP {exc.response.status_code}): "
                f"{exc.response.text[:200]}"
            )
        except Exception as exc:
            logger.warning(f"lknpd: refresh не удался: {exc}")

        # Сбрасываем токены — они больше не действительны
        self._token = None
        self._refresh_token = None
        return False

    async def ensure_authenticated(self) -> None:
        """Гарантирует наличие токена (вызывает login при необходимости)."""
        if not self._token:
            await self.login()

    # ── Регистрация дохода ────────────────────────────────────────────────────

    async def add_income(self, payment_id: str, name: str, amount: Decimal, quantity: int = 1) -> str:
        """
        Регистрирует доход в ФНС.

        POST /api/v1/income
        Authorization: Bearer <token>

        Тело запроса (из перехвата трафика и открытых реализаций):
        {
          "paymentType": "CASH",
          "inn": null,
          "ignoreMaxTotalIncomeRestriction": false,
          "client": {
            "contactPhone": null,
            "displayName": null,
            "incomeType": "FROM_INDIVIDUAL"
          },
          "requestTime": "2026-03-29T12:00:00+03:00",
          "operationTime": "2026-03-29T12:00:00+03:00",
          "services": [{
            "name": "...",
            "amount": 100.00,
            "quantity": 1
          }],
          "totalAmount": 100.00,
          "nonce": "<16-значная строка для идемпотентности>"
        }

        Возвращает approvedReceiptUuid — идентификатор чека в ФНС.
        """
        await self.ensure_authenticated()

        total = round(float(amount) * quantity, 2)
        now = _now_local()

        # nonce обеспечивает идемпотентность на стороне ФНС:
        # повторный запрос с тем же nonce вернёт тот же approvedReceiptUuid
        # без создания нового чека.
        # ВАЖНО: nonce детерминирован от payment_id — это гарантирует, что
        # любое количество повторных попыток (retry, перезапуск контейнера,
        # обработка pending-записей) не приведёт к дублю в налоговой.
        nonce = hashlib.md5(payment_id.encode()).hexdigest()[:16]

        body = {
            "paymentType": "CASH",
            "inn": None,
            "ignoreMaxTotalIncomeRestriction": False,
            "client": {
                "contactPhone": None,
                "displayName": None,
                "incomeType": "FROM_INDIVIDUAL",
            },
            "requestTime": now,
            "operationTime": now,
            "services": [
                {
                    "name": name,
                    "amount": float(amount),
                    "quantity": quantity,
                }
            ],
            "totalAmount": total,
            "nonce": nonce,
        }

        try:
            data = await self._post("/income", body, auth=True)

        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code

            if status in (401, 403):
                # Токен протух — пробуем обновить
                logger.info(f"lknpd: {status} при отправке чека — пробуем refresh")
                ok = await self.refresh()
                if not ok:
                    logger.info("lknpd: refresh не помог — повторная авторизация")
                    await self.login()
                # Повторяем запрос с новым токеном
                data = await self._post("/income", body, auth=True)
            else:
                raise

        receipt_uuid = data.get("approvedReceiptUuid")
        if not receipt_uuid:
            raise RuntimeError(
                f"lknpd: неожиданный ответ (нет approvedReceiptUuid): {data}"
            )

        return receipt_uuid

    async def close(self) -> None:
        if self._http and not self._http.is_closed:
            await self._http.aclose()
            logger.debug("lknpd: HTTP-сессия закрыта")


# ─── Публичный фасад с retry-логикой ──────────────────────────────────────────

class NalogClient:
    """
    Фасад над LknpdHttpClient.
    Добавляет retry-логику, пересоздание клиента при auth-ошибках,
    и единый интерфейс для processor.py.

    Заменяет ранее использовавшийся broken PyPI-пакет moy-nalog.
    """

    def __init__(self) -> None:
        self._inner: Optional[LknpdHttpClient] = None

    def _get_or_create(self) -> LknpdHttpClient:
        if self._inner is None:
            self._inner = LknpdHttpClient()
        return self._inner

    async def send_receipt(self, payment_id: str, amount: Decimal) -> str:
        """
        Отправляет чек в ФНС для одного платежа.

        Параметры:
            payment_id — идентификатор платежа (для логов)
            amount     — сумма в рублях

        Возвращает approvedReceiptUuid при успехе.
        Бросает RuntimeError после RETRY_COUNT неудачных попыток.
        """
        last_exc: Optional[Exception] = None

        for attempt in range(1, config.RETRY_COUNT + 1):
            try:
                client = self._get_or_create()
                receipt_uuid = await client.add_income(
                    payment_id=payment_id,
                    name=config.RECEIPT_NAME,
                    amount=amount,
                    quantity=1,
                )
                return receipt_uuid

            except Exception as exc:
                last_exc = exc
                logger.warning(
                    f"Payment {payment_id}: "
                    f"попытка {attempt}/{config.RETRY_COUNT} не удалась — {exc}"
                )

                # Пересоздаём клиент полностью при auth-ошибках
                err_lower = str(exc).lower()
                is_auth_err = any(
                    kw in err_lower
                    for kw in ("401", "403", "unauthori", "forbidden", "token", "auth")
                )
                if is_auth_err:
                    logger.info(
                        "lknpd: auth-ошибка — пересоздаём клиент с новой авторизацией"
                    )
                    if self._inner:
                        await self._inner.close()
                    self._inner = None

                if attempt < config.RETRY_COUNT:
                    await asyncio.sleep(config.RETRY_DELAY)

        raise RuntimeError(
            f"Все {config.RETRY_COUNT} попытки провалились "
            f"для payment {payment_id}: {last_exc}"
        ) from last_exc


# Синглтон — используется в processor.py
nalog_client = NalogClient()
