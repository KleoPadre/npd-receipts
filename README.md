# npd-receipts — воркер отправки чеков самозанятого в ФНС

Автономный Python-сервис, который читает успешные платежи из таблицы `payments`  
и отправляет чеки в «Мой налог» через прямые HTTP-запросы к `lknpd.nalog.ru`.

> **Без сторонних обёрток.** Весь протокол реализован с нуля в `app/nalog_client.py`:  
> авторизация по ИНН + паролю, автоматический refresh токена, регистрация дохода.  
> Зависимости — только `httpx`, `asyncpg`, `python-dotenv`.

---

## Структура проекта

```
npd-receipts/
├── app/
│   ├── __init__.py
│   ├── main.py          # точка входа, главный цикл + обработка сигналов
│   ├── config.py        # настройки из env/.env
│   ├── db.py            # asyncpg: чтение payments, управление tax
│   ├── nalog_client.py  # HTTP-клиент к lknpd.nalog.ru (авторизация + чеки)
│   ├── processor.py     # бизнес-логика одного цикла
│   └── logger.py        # логирование в stdout + ротируемый файл
├── env/
│   └── .env             # секреты (не коммитить!)
├── wheels/              # .whl-файлы для сборки без интернета
├── logs/                # файлы логов (монтируется с NAS)
├── requirements.txt
├── Dockerfile
├── .dockerignore
└── README.md
```

---

## Как это работает

```
┌─────────────────────────────────────────────────────────────────────────┐
│  СТАРТ                                                                  │
│    config.validate()   — проверка обязательных переменных окружения     │
│    init_db()           — CREATE TABLE IF NOT EXISTS tax                 │
│    login()             — POST /api/v1/auth/lkfl → accessToken           │
└──────────────────────────────┬──────────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  ГЛАВНЫЙ ЦИКЛ (повторяется каждые TIMER минут)                          │
│                                                                         │
│  1. fetch_pending()        — зависшие записи (status IS NULL)           │
│                              подбираем если был краш в прошлом цикле    │
│                                                                         │
│  2. fetch_new_payments()   — SELECT из payments WHERE status='succeeded'│
│                              AND payment_id ещё нет в таблице tax       │
│                                                                         │
│  3. insert_pending()       — резервируем каждый новый платёж            │
│                              INSERT INTO tax (status=NULL)              │
│                              ON CONFLICT DO NOTHING  ← защита дублей    │
│                                                                         │
│  4. для каждого платежа в очереди (pending → new):                      │
│       ┌─────────────────────────────────────────────────────┐           │
│       │  send_receipt()                                     │           │
│       │    POST /api/v1/income  (Bearer accessToken)        │           │
│       │                                                     │           │
│       │    ОК  → mark_success()  status=TRUE                │           │
│       │                                                     │           │
│       │    401/403 → refresh()  POST /api/v1/auth/token     │           │
│       │              если refresh не помог → login() снова  │           │
│       │              повторить запрос с новым токеном        │           │
│       │                                                     │           │
│       │    другая ошибка → попытка 1/2/3 (пауза RETRY_DELAY)│           │
│       │    все провалились → mark_error()  status=FALSE     │           │
│       └─────────────────────────────────────────────────────┘           │
│       sleep(SEND_DELAY)  ← пауза между платежами                        │
│                                                                         │
│  5. sleep(TIMER * 60)    ← ждём до следующего цикла                     │
│     (по 1 сек., чтобы SIGTERM обработался мгновенно)                    │
└─────────────────────────────────────────────────────────────────────────┘
                               │ SIGTERM / SIGINT
                               ▼
                         close_pool()
                    === npd-receipts Worker stopped ===
```

### Протокол API lknpd.nalog.ru

| Эндпоинт               | Метод | Назначение                                  |
|------------------------|-------|---------------------------------------------|
| `/api/v1/auth/lkfl`    | POST  | Вход по ИНН + паролю → token + refreshToken |
| `/api/v1/auth/token`   | POST  | Обновление токена без повторного пароля      |
| `/api/v1/income`       | POST  | Регистрация дохода → approvedReceiptUuid     |

> ⚠️ **Важно.** Эндпоинт `/api/v1/auth/inn` устарел и возвращает `404` начиная примерно с середины 2025 года.  
> Актуальный эндпоинт — `/api/v1/auth/lkfl`. Поле `inn` в теле запроса переименовано в `username`.

**Управление токеном:**
1. При старте — `login()` (ИНН + пароль) через `/auth/lkfl`.
2. При ошибке `401/403` в запросе чека — сначала `refresh()` через `/auth/token`.
3. Если refresh не помог — повторный `login()`.
4. Токен привязан к Device ID и IP контейнера — не меняйте их между запусками.

---

## Переменные окружения

Файл `env/.env`:

| Переменная       | Обязательная | Описание                               | По умолчанию |
|------------------|:---:|----------------------------------------|:---:|
| `NALOG_INN`      | ✅  | ИНН самозанятого (12 цифр)             | —   |
| `NALOG_PASSWORD` | ✅  | Пароль от «Мой налог»                  | —   |
| `DATABASE_URL`   | ✅  | PostgreSQL DSN                         | —   |
| `TIMER`          | ❌  | Интервал между циклами (минуты)        | 60  |
| `SEND_DELAY`     | ❌  | Пауза между отправками чеков (секунды) | 10  |

Пример заполненного файла:
```env
NALOG_INN=123456789012
NALOG_PASSWORD=MyStr0ngPass
DATABASE_URL=postgresql://user:pass@192.168.1.100:5432/mydb
TIMER=60
SEND_DELAY=10
```

> **Важно.** Если вы не помните пароль от «Мой налог» —  
> восстановить его можно только через [Личный кабинет налогоплательщика](https://lkfl2.nalog.ru/lkfl/login).  
> Аккаунты на lknpd.nalog.ru и lkfl2.nalog.ru — одни и те же.

---

## База данных

Таблица `payments` — **только читается** (`SELECT`). Сервис её не трогает.

Сервис автоматически создаёт таблицу `tax` при первом запуске:

```sql
CREATE TABLE tax (
    id           SERIAL PRIMARY KEY,
    payment_id   TEXT NOT NULL,
    status       BOOLEAN,        -- NULL=в обработке, TRUE=успех, FALSE=ошибка
    error_text   TEXT,
    created_at   TIMESTAMP DEFAULT NOW(),
    processed_at TIMESTAMP
);

CREATE UNIQUE INDEX tax_payment_id_idx ON tax (payment_id);
```

### Если платежи уже отправляли вручную

Чтобы сервис не дублировал уже отправленные чеки — пометьте их как выполненные:

```sql
INSERT INTO tax (payment_id, status, processed_at)
SELECT invoice_id, TRUE, NOW()
FROM payments
WHERE status = 'succeeded'
ON CONFLICT (payment_id) DO NOTHING;
```

---

## Устойчивость и защита от дублей

| Проблема                    | Решение                                                              |
|-----------------------------|----------------------------------------------------------------------|
| Дубли чеков                 | `UNIQUE(payment_id)` + `INSERT … ON CONFLICT DO NOTHING`            |
| Краш в середине цикла       | При старте подбираем записи с `status IS NULL` → дообрабатываем     |
| Протухший токен             | Автоматический `refresh()` через `POST /api/v1/auth/token`          |
| Полная потеря токена        | Повторный `login()` через ИНН + пароль                              |
| Недоступность API ФНС       | 3 попытки с паузой 7с; при провале — `status=FALSE` + текст ошибки  |
| Перезапуск контейнера       | Состояние — в БД; `.env` и `logs/` монтируются с NAS                |
| Влияние на бота             | Только `SELECT` из `payments`; своя изолированная таблица `tax`     |

---

# Деплой на Synology NAS DS220+ с нуля

## Что потребуется

| Компонент | Условие |
|-----------|---------|
| MacBook   | macOS 12+, **Docker Desktop** установлен |
| Synology DS220+ | DSM 7.x |
| **Container Manager** | установлен через Package Center DSM |
| PostgreSQL | доступен с NAS по сети |

---

## Шаг 0 — Подготовка MacBook

### 0.1 Установить Docker Desktop

Если Docker Desktop ещё не установлен:

1. Перейдите на [docker.com/products/docker-desktop](https://www.docker.com/products/docker-desktop/)
2. Скачайте версию для вашего Mac:
   - **Apple Silicon (M1/M2/M3)** → "Mac with Apple Silicon"
   - **Intel** → "Mac with Intel Chip"
3. Откройте `.dmg`, перетащите Docker в папку Applications.
4. Запустите Docker Desktop и дождитесь иконки кита в меню-баре со статусом **«Running»**.

Проверьте в терминале:
```bash
docker --version
# Docker version 27.x.x, build ...

docker buildx version
# github.com/docker/buildx v0.x.x ...
```

### 0.2 Включить поддержку кросс-платформенной сборки

DS220+ работает на Intel Celeron (x86_64). Если ваш Mac на Apple Silicon — нужна эмуляция:

```bash
docker run --privileged --rm tonistiigi/binfmt --install all
```

Проверьте, что `linux/amd64` доступна:
```bash
docker buildx ls
# NAME/NODE     DRIVER/ENDPOINT   STATUS    PLATFORMS
# default *     docker            running   linux/amd64, linux/arm64, ...
```

---

## Шаг 1 — Заполнить .env

```bash
cd npd-receipts
nano env/.env
```

Заполните все три обязательных поля. Файл должен выглядеть так:

```env
NALOG_INN=123456789012
NALOG_PASSWORD=МойПароль
DATABASE_URL=postgresql://dbuser:dbpass@192.168.1.100:5432/botdb
TIMER=60
SEND_DELAY=10
```

> ⚠️ Не коммитьте `.env` в Git — он содержит пароль от налоговой.

---

## Шаг 2 — Скачать зависимости локально (wheels)

Этот шаг нужен, чтобы сборка Docker-образа не зависела от интернета  
(PyPI бывает недоступен при сборке из-за DNS или SSL в Docker Desktop).

`asyncpg` — единственный бинарный пакет, остальные pure-python. Скачиваем раздельно:

```bash
rm -rf wheels

# asyncpg — бинарный, нужна конкретная платформа Linux x86_64
pip download \
  --platform manylinux2014_x86_64 \
  --python-version 311 \
  --only-binary=:all: \
  --no-deps \
  asyncpg==0.30.0 \
  -d ./wheels

# остальные зависимости — pure-python, платформа не важна
pip download \
  --no-cache-dir \
  httpx==0.27.2 \
  python-dotenv==1.0.1 \
  -d ./wheels
```

После выполнения в папке `wheels/` должны появиться файлы примерно такие:

```
wheels/
├── asyncpg-0.30.0-cp311-cp311-manylinux_2_17_x86_64.whl
├── httpx-0.27.2-py3-none-any.whl
├── httpcore-1.0.x-py3-none-any.whl
├── h11-0.14.0-py3-none-any.whl
├── certifi-20xx.x.x-py3-none-any.whl
├── anyio-4.x.x-py3-none-any.whl
├── sniffio-1.x.x-py3-none-any.whl
├── idna-3.x-py3-none-any.whl
└── python_dotenv-1.0.1-py3-none-any.whl
```

---

## ⚠️ Возможные проблемы при сборке на Mac

### Проблема 1: `pip download` не скачал `typing_extensions`

**Симптом:** при `docker buildx build` падает ошибка вида:
```
ERROR: Could not find a version that satisfies the requirement typing_extensions>=4.5
```

**Причина:** `typing_extensions` уже установлен на вашем Mac, поэтому `pip download` решил, что качать его не нужно.

**Решение:** докачайте его отдельно:
```bash
pip download "typing_extensions>=4.5" -d ./wheels
```

---

### Проблема 2: `pip download` скачал ARM-wheel вместо x86_64

**Симптом:** при запуске контейнера на NAS ошибка:
```
exec format error
```

**Причина:** `pip download` без явного `--platform` скачивает wheel под архитектуру вашего Mac (ARM), а не под Linux x86_64.

**Решение:** для `asyncpg` всегда используйте явный `--platform`:
```bash
pip download \
  --platform manylinux2014_x86_64 \
  --python-version 311 \
  --only-binary=:all: \
  --no-deps \
  asyncpg==0.30.0 \
  -d ./wheels
```

---

### Проблема 3: `--only-binary` конфликтует с pure-python пакетами

**Симптом:** `pip download` с `--only-binary=:all:` падает на `httpx` или `python-dotenv`:
```
ERROR: Could not find a version that satisfies the requirement httpx==0.27.2
(from versions: none)
```

**Причина:** `--only-binary=:all:` запрещает скачивать source-dist, а некоторые индексы отдают только sdist для pure-python пакетов.

**Решение:** качайте бинарный `asyncpg` отдельно от pure-python пакетов (как показано в Шаге 2).

---

### Проблема 4: сборка образа падает на `pip install` без интернета

**Симптом:** `docker buildx build` зависает или падает на шаге установки пакетов.

**Причина:** Dockerfile пытается обратиться в интернет, хотя должен ставить из `./wheels`.

**Проверьте** Dockerfile — флаги `--no-index --find-links=./wheels` обязательны:
```dockerfile
RUN pip install --no-cache-dir --no-index --find-links=./wheels -r requirements.txt
```

---

### Проблема 5: `404 Not Found` при авторизации в ФНС

**Симптом** в логах:
```
Client error '404 Not Found' for url 'https://lknpd.nalog.ru/api/v1/auth/inn'
```

**Причина:** ФНС упразднила эндпоинт `/auth/inn` в 2025 году.

**Решение:** в `app/nalog_client.py` должен использоваться актуальный эндпоинт `/auth/lkfl` с полем `username` вместо `inn`. Эта версия сервиса уже содержит исправление.

---

## Шаг 3 — Собрать Docker-образ

```bash
docker buildx build \
  --platform linux/amd64 \
  -t npd-receipts:latest \
  --output type=docker \
  .
```

Флаг `--platform linux/amd64` обязателен — иначе на Mac с Apple Silicon образ соберётся под ARM и не запустится на NAS.

Успешное завершение выглядит так:
```
=> => naming to docker.io/library/npd-receipts:latest
```

---

## Шаг 4 — Экспортировать образ в файл

```bash
docker save npd-receipts:latest | gzip > npd-receipts.tar.gz
```

Файл `npd-receipts.tar.gz` появится в папке проекта. Его размер — около 60–80 МБ.

---

## Шаг 5 — Загрузить образ на NAS

1. Откройте **DSM** в браузере (`http://<IP-адрес NAS>:5000`)
2. Запустите **Container Manager** (найдите в главном меню)
3. Перейдите в раздел **Image** (левая панель)
4. Нажмите **Add → Import from file**
5. Выберите файл `npd-receipts.tar.gz`
6. Дождитесь появления `npd-receipts:latest` в списке образов

---

## Шаг 6 — Создать папки на NAS

Откройте **File Station** в DSM и создайте папки:

```
/volume1/docker/npd-receipts/env/
/volume1/docker/npd-receipts/logs/
```

Затем загрузите файл `env/.env` (с заполненными значениями) в папку `/volume1/docker/npd-receipts/env/`.

> Папка `logs/` будет заполняться автоматически — создайте её пустой.

---

## Шаг 7 — Создать контейнер

1. **Container Manager → Container → Create**
2. Выберите образ `npd-receipts:latest`
3. Имя контейнера: `npd-receipts`
4. Включите **Auto-restart** (перезапуск при сбое / перезагрузке NAS)
5. Перейдите на вкладку **Volume** и добавьте два маппинга:

| Путь на NAS                     | Путь в контейнере | Режим       |
|---------------------------------|-------------------|-------------|
| `/volume1/docker/npd-receipts/env`     | `/app/env`        | Read/Write  |
| `/volume1/docker/npd-receipts/logs`    | `/app/logs`       | Read/Write  |

6. Нажмите **Apply** — контейнер запустится автоматически.

---

## Шаг 8 — Проверить работу

### Через Container Manager

Container Manager → контейнер `npd-receipts` → вкладка **Log**.

### Через File Station

Откройте `/volume1/docker/npd-receipts/logs/npd-receipts.log`.

Нормальный старт:
```
[2026-03-29 12:00:00] INFO     === npd-receipts Worker started ===
[2026-03-29 12:00:00] INFO     INN: 123456789012 | Interval: 60 min | Send delay: 10s | Retries: 3
[2026-03-29 12:00:01] INFO     База данных инициализирована (таблица tax готова)
[2026-03-29 12:00:01] INFO     lknpd: авторизация выполнена | device_id=aB3kFx… | token expires in 36000s
[2026-03-29 12:00:01] INFO     Found 2 new payment(s)
[2026-03-29 12:00:11] INFO     Payment inv_abc123 SUCCESS (receipt=a1b2c3d4e5f6g7h8)
[2026-03-29 12:00:21] INFO     Payment inv_abc124 SUCCESS (receipt=h8g7f6e5d4c3b2a1)
[2026-03-29 12:00:21] INFO     Cycle done: 2 success, 0 error(s) out of 2 total
[2026-03-29 12:00:21] INFO     Sleeping 60 minute(s) until next cycle ...
```

### Распространённые ошибки

| Симптом в логах | Причина | Решение |
|-----------------|---------|---------|
| `404` при авторизации | Устаревший эндпоинт `/auth/inn` | Убедитесь что используется `/auth/lkfl` |
| `401` при авторизации | Неверный ИНН или пароль | Проверьте `NALOG_INN` и `NALOG_PASSWORD` |
| `Could not resolve host` | DNS не работает в контейнере | Перезапустите контейнер; проверьте DNS NAS |
| `connection refused` на DB | Неверный `DATABASE_URL` | Проверьте хост, порт, логин, пароль |
| `Missing required env vars` | Не заполнен `.env` | Проверьте файл в `/volume1/docker/npd-receipts/env/` |

---

## Обновление сервиса

1. Внесите изменения в код на MacBook
2. Повторите **Шаги 2–4** (скачать wheels → собрать → экспортировать)
3. В Container Manager — **остановите** и **удалите** контейнер `npd-receipts`
4. Загрузите новый образ (**Шаг 5**), удалив старый
5. Пересоздайте контейнер (**Шаг 7**)

Папки `env/` и `logs/` на NAS **не трогайте** — данные сохранятся.

---

## Справочник по командам MacBook

```bash
# Скачать asyncpg (бинарный, нужна платформа)
pip download \
  --platform manylinux2014_x86_64 \
  --python-version 311 \
  --only-binary=:all: \
  --no-deps \
  asyncpg==0.30.0 \
  -d ./wheels

# Скачать остальные зависимости (pure-python)
pip download \
  --no-cache-dir \
  httpx==0.27.2 \
  python-dotenv==1.0.1 \
  -d ./wheels

# Если не хватает typing_extensions
pip download "typing_extensions>=4.5" -d ./wheels

# Собрать образ для x86_64 (DS220+)
docker buildx build \
  --platform linux/amd64 \
  -t npd-receipts:latest \
  --output type=docker \
  .

# Экспортировать образ
docker save npd-receipts:latest | gzip > npd-receipts.tar.gz

# Посмотреть логи локально (если запустили для теста)
docker run --rm \
  -v $(pwd)/env:/app/env \
  -v $(pwd)/logs:/app/logs \
  npd-receipts:latest
```
