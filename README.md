# Опера — SQLite Foundation и фабрика клиентских Telegram-ботов

Локально запускаемый Foundation + Owner Admin Bot + общий runtime manager индивидуальных клиентских Telegram-ботов. Все процессы используют один SQLite-файл на одном сервере. Один процесс `client-bots` динамически обслуживает несколько BotFather-ботов общей кодовой базой, но со строгим tenant-контекстом и отдельным зашифрованным token.

## Реализовано

- SQLAlchemy 2 async + `aiosqlite`;
- Alembic initial migration без PostgreSQL-типов;
- SQLite WAL, foreign keys, `synchronous=NORMAL`, `busy_timeout=5000`;
- UUID как строки, JSON через стандартный SQLAlchemy `JSON`;
- tenant, settings, AI profile, encrypted BotFather token, bot instance и audit log;
- единственный owner по Telegram ID и внутренний owner API token;
- persistent aiogram FSM в `fsm_states` с TTL и очисткой;
- durable `background_jobs` с idempotency, priority, retry, failed state и восстановлением stale leases;
- отдельные backend, owner-bot, scheduler и background-worker процессы;
- отдельный `client-bots` runtime manager с long polling, heartbeat, backoff и восстановлением после рестарта;
- динамический запуск, остановка, перезапуск, проверка, ротация token и soft-delete bot instance;
- owner-only авторизация в клиентском боте и product events со статистикой;
- inline-first интерфейс обоих Telegram-ботов: callback-навигация, редактируемые экраны и строгая блокировка пересекающихся FSM-сценариев;
- параллельные SQLite workers с атомарным claim, уникальным `locked_by` и heartbeat;
- консистентный backup через SQLite backup API и проверку целостности;
- тесты CRUD, API, миграций, FSM persistence, job queue, конкурентных записей и backup/restore.
- безопасное подключение рабочего Telegram через Telethon StringSession: телефон → код → optional 2FA;
- выбор рабочей папки, отдельное согласие на классификацию личных диалогов и периоды 3/7/14/30 дней;
- постепенная initial sync по durable queue с batch, паузами, FloodWait, retry, offset, idempotency и продолжением после рестарта;
- live progress в одном сообщении, итоговые метрики, проблемы с evidence и подтверждение спорных личных диалогов;
- динамическая Mini App с onboarding-состояниями и tenant bootstrap, защищённым Telegram WebApp `initData`.
- единый tenant-aware scheduler с устойчивым расписанием, ранним стартом анализа и fair SQLite queue;
- валидируемые JSON-пакеты DeepSeek, локальная предобработка, отчёты, метрики использования AI и health-срез;
- модели сотрудников, отделов, ролей, memberships и permissions для следующего этапа продукта.

## Секреты и обязательные переменные

Создайте локальный файл:

```bash
cp .env.example .env
```

Обязательные значения:

```text
DATABASE_URL=sqlite+aiosqlite:///./data/app.db
TELEGRAM_OWNER_BOT_TOKEN=
PLATFORM_OWNER_TELEGRAM_ID=
PLATFORM_OWNER_TELEGRAM_USERNAME=
OWNER_API_TOKEN=
APP_ENCRYPTION_KEY=
DEEPSEEK_API_KEY=
TELEGRAM_API_ID=
TELEGRAM_API_HASH=
CLIENT_MINI_APP_URL=
```

`DEEPSEEK_API_KEY` пока может быть пустым. Локальный `.env` должен иметь права `0600` и исключён из Git. Любой ключ, переданный через чат или опубликованный в логе, считается скомпрометированным: сначала отзовите его, затем внесите новый непосредственно в `.env`.

Генерация новых локальных ключей:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
openssl rand -hex 32
```

Первый результат — `APP_ENCRYPTION_KEY`, второй — `OWNER_API_TOKEN`.

## Запуск

Точная команда из корня репозитория:

```bash
docker compose up --build
```

Запускаются пять логически разделённых процессов:

- `backend` — применяет миграцию и запускает FastAPI на `http://localhost:8000`;
- `owner-bot` — административный Telegram-бот с SQLite FSM;
- `background-worker` — polling worker SQLite job queue; сервис можно масштабировать несколькими процессами на том же host/volume;
- `scheduler` — единственный процесс, который восстанавливает расписание tenant и ставит анализы в общую очередь;
- `client-bots` — фабрика и runtime manager всех активных клиентских ботов.

Все используют bind mounts:

```text
./data:/app/data
./backups:/app/backups
```

Проверка API:

```bash
curl http://localhost:8000/health
curl http://localhost:8000/ready
```

Локальный режим без Docker (в пяти терминалах, после `.venv/bin/alembic upgrade head`):

```bash
.venv/bin/python -m services.backend.main
.venv/bin/python -m services.owner_bot.main
.venv/bin/python -m services.backend.jobs.worker
.venv/bin/python -m services.backend.scheduler.main
.venv/bin/python -m services.backend.client_bots.main
```

Воспроизводимая проверка без реального Telegram API:

```bash
.venv/bin/python -m scripts.smoke_test
.venv/bin/python -m scripts.secret_scan
```

## Миграции

При `docker compose up` миграция применяется автоматически. Точная ручная команда:

```bash
docker compose run --rm backend alembic upgrade head
```

Проверка downgrade/re-upgrade на тестовой базе выполняется тестом `test_migrations.py`. Для ручного отката:

```bash
docker compose run --rm backend alembic downgrade base
docker compose run --rm backend alembic upgrade head
```

Не выполняйте downgrade на рабочей базе без резервной копии.

## Административный бот

Создайте owner bot через `@BotFather`, заполните `TELEGRAM_OWNER_BOT_TOKEN` и разрешённый `PLATFORM_OWNER_TELEGRAM_ID`. Любой другой Telegram user ID получает отказ в middleware.

Основной сценарий:

1. Откройте owner bot и выполните `/start`; старая reply-клавиатура будет удалена.
2. Нажмите inline-кнопку **Создать** и пройдите сокращённый FSM.
3. Перезапустите сервисы — созданный клиент и незавершённое FSM-состояние останутся в `data/app.db`.
4. Проверьте сводку и нажмите **Подтвердить** — до этого момента клиент не записывается.
5. Откройте AI-настройки или клиентского бота из карточки клиента.
6. Через **Подключить бота** отправьте BotFather token.
7. Token проверяется через `getMe`, шифруется Fernet и сохраняется в `encrypted_secrets`; API и бот token обратно не показывают.
8. Runtime manager замечает новую запись без общего рестарта и запускает polling.
9. В карточке доступны запуск, остановка, перезапуск, getMe-проверка, ссылка, статистика, ошибки, ротация token и удаление.

Клиентский бот разрешает доступ только `tenant.owner_telegram_user_id`. Посторонний пользователь не получает tenant-данные, а попытка записывается как `unauthorized_access_attempt`. Inline-меню: **Сводка**, **Важное**, **Отчёты**, **Подключения**, **Открыть панель**, **Настройки**. Клиентский интерфейс не показывает владельца платформы, поддержку, публичные цены или тарифы.

Для user-session создайте Telegram application на `my.telegram.org`, внесите её API ID/hash в `.env` и перезапустите `client-bots` и `background-worker`. В клиентском боте откройте **Подключения**. Мастер попросит создать рабочую папку, удалит сообщения с кодом/2FA после обработки и не включит личные диалоги без отдельного согласия. По умолчанию анализируется 7 дней. Кнопки остановки, отключения и полной очистки доступны в том же inline-потоке.

Mini App отправляет backend только подписанную строку `Telegram.WebApp.initData`. Backend проверяет HMAC и срок `auth_date`, затем сопоставляет Telegram user ID с владельцем tenant. Для отдельного frontend origin укажите HTTPS `CLIENT_MINI_APP_URL`, а во frontend — `NEXT_PUBLIC_API_BASE_URL`.

Frontend готовится к Vercel из корня репозитория — именно там лежит `package.json`. Основной `npm run build` выполняет стандартный `next build`; прежний Cloudflare-вариант сохранён как `npm run build:cloudflare`. После получения HTTPS URL в Vercel укажите его в backend `.env` как `CLIENT_MINI_APP_URL`, а адрес публичного backend API задайте в Vercel как `NEXT_PUBLIC_API_BASE_URL`.

Архитектурный UI-контракт зафиксирован в `docs/telegram-inline-ui.md`. Подробное ТЗ следующего вертикального этапа находится в `docs/NEXT_STAGE_PROMPT.md`.

Отмена FSM: `/cancel`. Тестовая background job через owner bot:

```text
/test_job
```

## Background jobs

Рабочий контур использует гибридную обработку: каждый активный Telegram account получает
короткий incremental poll, новые сообщения идемпотентно сохраняются по dialog cursor и проходят
локальный signal engine. Только значимые `Signal` ставятся в дешёвый `signal.ai_triage` с компактным
`DialogState`; полный `analysis.pipeline` оставлен для глубокого анализа и отчётов. Обязательства
хранятся отдельно в `Commitment` и ежечасно проверяются SQL/local reconciliation без повторного
чтения всей переписки. Все tenants и все их Telegram sessions обслуживаются общей priority queue.

Приоритетные типы: `telegram.fetch_updates`, `telegram.history_sync`, `signal.local_scan`,
`signal.ai_triage`, `commitment.reconcile`, `problem.evaluate`, `analysis.hourly`, `analysis.deep`,
`notification.employee`, `notification.manager`, `notification.group`, `report.employee`,
`report.client`, `report.company`, `maintenance.session_health`. Классы ресурсов — `light`,
`ai_fast`, `heavy`; лимиты AI и heavy jobs задаются через `.env`.

Создать тестовую задачу из контейнера:

```bash
docker compose exec backend python -m services.backend.jobs.cli enqueue-test
```

Проверить retry-сценарий:

```bash
docker compose exec backend python -m services.backend.jobs.cli enqueue-test --type system.fail_once
```

Каждый worker получает уникальный ID `configured-name:hostname:pid`. Claim выполняется условным `UPDATE` в короткой транзакции, поэтому несколько процессов не выполняют один job одновременно. Во время handler worker обновляет `locked_at`; после ошибки увеличивается `attempts` и планируется exponential retry. При старте восстанавливаются только действительно stale leases. Для повторной постановки используйте `idempotency_key`.

## Резервное копирование и восстановление

Точная команда backup:

```bash
docker compose exec backend python -m services.backend.scripts.backup_sqlite
```

Создаётся `./backups/app-<UTC timestamp>.db` с правами `0600`. Используется SQLite backup API, а не копирование активного файла; после создания выполняется `PRAGMA integrity_check`.

Восстановление:

```bash
docker compose stop backend owner-bot background-worker client-bots
docker compose run --rm backend python -m services.backend.scripts.restore_sqlite \
  /app/backups/app-YYYYMMDDTHHMMSSffffffZ.db --confirm-stopped
docker compose up -d
```

Перед восстановлением сохраните отдельную актуальную копию текущей базы.

## Тесты

Точная команда:

```bash
docker compose run --rm backend pytest -q
```

Локальный вариант:

```bash
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q
```

Набор включает migrations, API, persistent FSM, backup/restore, один и три client runtime, dynamic start/stop/restart/recovery, owner authorization, tenant isolation, product-event statistics, безопасную ошибку token, несколько SQLite workers, secret scan и smoke subprocess.

## Ограничения SQLite MVP

- несколько worker/process поддерживаются, но SQLite всё равно сериализует конкурирующие записи;
- записи ограничиваются короткими транзакциями и повторяются при `database is locked`; при устойчивой высокой конкуренции latency растёт;
- WAL улучшает совместную работу читателей и writer-процессов, но не превращает SQLite в распределённую БД;
- все процессы должны работать на одном сервере и общем локальном volume; сетевой filesystem не поддерживается;
- runtime manager сейчас использует long polling и предполагает один активный менеджер для каждого bot instance; межпроцессное распределение client bots ещё не реализовано;
- backup-файлы содержат зашифрованные секреты и сами должны храниться как секретные данные;
- нет внешней очереди, distributed locks, multi-host failover и горизонтального масштабирования;
- полноценная AI-обработка требует действующего `DEEPSEEK_API_KEY`; без него job завершится контролируемой ошибкой и останется наблюдаемым в очереди;
- Telethon использует глобальную Telegram application, но каждая user session, выбор диалогов и progress изолированы по tenant;
- классификация личных диалогов использует ограниченный набор контекстных сигналов; низкая уверенность всегда требует ручного подтверждения;
- frontend build требует Node-зависимости из `package-lock.json`; при недоступности npm registry Python/backend проверки остаются независимыми.

## Когда переходить на PostgreSQL или Redis

Переход на PostgreSQL нужен, если появляются несколько серверов, несколько активных writer-процессов, устойчивые задержки из-за locks, высокая частота записей, большие аналитические выборки, требования HA/replication или необходимость database-level tenant isolation.

Redis или отдельная очередь нужны при высокой пропускной способности jobs, distributed rate limits/locks, очередях с короткой задержкой, нескольких серверах bot/backend или гарантированной межсерверной доставке событий. Несколько локальных SQLite workers сами по себе не требуют Redis.
