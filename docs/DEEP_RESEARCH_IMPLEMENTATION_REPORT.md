# Deep Research implementation report

Дата: 2026-08-08

## Дополнение 2026-08-09 — final hardening

Это дополнение заменяет более ранние показатели и ограничения ниже там, где они расходятся.

- Telegram push fast path теперь включён через отдельный `TelegramSessionRuntime` с
  `NewMessage`/`MessageEdited`; periodic cursor catch-up сохранён как recovery path.
- Один permanent account принадлежит одному actor/Telethon client. DB lease, heartbeat и
  fencing generation защищают от второго владельца и stale writes.
- Catalog, health, logout и historical sync в production идут через account-scoped durable RPC.
- Очередь поддерживает dialog partition ordering; actor RPC сериализован на account.
- Добавлены durable SLA и commitment deadline jobs с periodic reconciliation fallback.
- Owner AI drafts получили strict schema, pre-provider secret screening и audit metadata.
- Добавлена миграция `20260809_0014_core_hardening`.
- Итоговая локальная проверка: **93 tests passed**, Ruff/ESLint/TypeScript/Next build passed,
  Alembic upgrade-downgrade-reupgrade passed, Docker images built and container import smoke passed.
- Repository-wide Python mypy остаётся непроходящим: 471 baseline errors в 26 файлах.
- Актуальная оценка controlled-pilot readiness: **84%**; live Telegram/DeepSeek и soak/load
  проверки по-прежнему не выдаются за выполненные.

Подробная финальная классификация находится в `FINAL_TECHNICAL_REPORT.md`.

## Результат

Аудит использован как карта рисков, но каждое замечание было повторно проверено по актуальному
коду. Проект сохранён как single-host modular monolith на SQLite. Основной продуктовый путь теперь
собран вокруг общей цепочки:

```text
Telegram message
→ idempotent ingestion
→ local Signal
→ optional AI triage/deep analysis
→ correlated Commitment / OperationalProblem
→ scoped notification
→ assignment and controlled transition
→ remediation verification
→ resolution/reopen audit
→ consolidated report
```

Обновлённая оценка готовности к ограниченному реальному пилоту: **84% ± 4 п.п.** Исходная оценка
аудита была 64% ± 5 п.п. Это не оценка промышленного multi-host deployment: для него по-прежнему
нужны нагрузочные данные, PostgreSQL/очередь при подтверждённой необходимости и отдельная
эксплуатационная работа.

## Подтверждённые замечания аудита

В актуальном коде были подтверждены:

- фактически последовательное выполнение jobs при уже зрелой durable queue;
- разные downstream-пути initial, incremental и scheduled analysis;
- неподключённая к persistent API доменная FSM проблем;
- прямое присваивание status и отсутствие transition audit/evidence;
- слишком слабое правило auto-resolution по любому исходящему сообщению;
- разрыв Employee → Telegram identity → TenantMembership → Permission;
- риск попадания `.env`, SQLite, backup, session и build/runtime data в архив;
- создание Telethon connection на каждый fetch;
- слабый разбор русскоязычных дедлайнов;
- пересечение manager/employee/group/immediate notification thresholds;
- слишком широкий cooldown;
- отсутствие tenant-wide aggregation barrier при нескольких Telegram accounts;
- context truncation, способный вытеснить свежий хвост;
- преимущественно read-only Mini App;
- неполный production metrics surface.

## Что уже было исправлено до этого этапа

Повторно не переписывались уже рабочие части:

- durable SQLite queue: conditional claim, idempotency key, priority, retry, heartbeat, stale lease
  recovery и tenant fairness;
- WAL, foreign keys, busy timeout, backup/restore;
- Telegram phone/code/optional 2FA и encrypted StringSession;
- cursor-based initial/incremental ingestion и duplicate protection;
- strict AI JSON validation/repair, budgets и usage accounting;
- Telegram Mini App `initData` HMAC validation и server-side tenant resolution;
- production CORS flow, client bot menu-button synchronization и backend onboarding state;
- timezone normalization/tzdata и JSON-safe owner-bot FSM;
- frontend service boundary, Telegram-aware mobile layout и existing onboarding.

## Реализованные изменения

### P0 — целостность ядра

**Worker pools.** Один `background-worker` теперь запускает небольшие bounded pools для Telegram,
notifications, AI, heavy/report и general jobs. Pool claim ограничен категориями, поэтому длинный AI
job не блокирует ingestion или critical notification. Tenant fairness, lease, heartbeat, retry и
idempotency сохранены. Записи SQLite ограничены process-wide semaphore; десятки writer workers не
создаются. Границы категорий остаются пригодными для последующего вынесения за интерфейсом queue.

**Общий lifecycle анализа.** Initial sync больше не создаёт отдельный тип проблем напрямую. После
persist сообщений он ставит тот же local-signal job, что incremental ingestion. Scheduled candidates
сначала коррелируются в canonical Signal, а fingerprint проблемы совместим с incremental path.

**Persistent Problem FSM.** `packages/ops_core` используется persistent adapter-слоем. Добавлены:

- server-side validation переходов;
- responsible employee и assignment validation;
- deadline;
- immutable transition history;
- resolution evidence и closure reason;
- verification history;
- reopen;
- запрет произвольного PATCH строки status.

**Remediation verifier.** Проверка использует evidence проблемы, ожидаемое действие и только новые
сообщения. Надёжные deterministic признаки обрабатываются локально; смысловые случаи могут пройти
малый AI verification. Короткое подтверждение вроде «Ок» не закрывает проблему. `uncertain` и слабая
confidence не вызывают auto-resolution.

**Employee/RBAC.** Создание и изменение Employee синхронизирует TenantMembership, role и Permission.
При смене Telegram ID старая membership деактивируется. Employee API scope ограничен собственными
problem/commitment/employee records; manager/owner работают в рамках tenant. Tenant ID по-прежнему
не принимается от frontend как источник авторизации.

**Secrets/runtime data.** Расширен `.gitignore`, усилен secret scan и добавлен безопасный
`scripts/export_source.py`. Экспорт строится из разрешённого tracked source set и исключает `.env`,
БД, backups, sessions, `.git`, `.venv`, `node_modules`, `.next`, логи и runtime data.

### P1 — корректность анализа

**Telethon.** В worker переиспользуется один serialized long-lived client на session/account,
подключение восстанавливается после disconnect, idle clients удаляются, FloodWait сохраняет
контролируемый retry path. Periodic polling остаётся reconciliation/recovery механизмом.

**Critical fast lane.** Tenant может задать явные hard rules с `contains_any`, `contains_all`,
signal type и criticality. Общие слова вроде «срочно» не являются default P0. Rule создаёт provisional
alert, после AI он подтверждается, обновляется или отменяется/корректируется.

**Deadline parser.** С учётом IANA timezone поддержаны: сегодня, завтра, послезавтра, до/в день
недели, к времени, `15.08`, русские названия месяцев, через N дней и следующая неделя. Прошедшее
время/дата корректно переносятся вперёд.

**Notifications.** Manager, employee, group и immediate thresholds разделены. Учитываются AI flags
`requires_employee_notification`, `requires_manager_notification`, `recommended_deadline_minutes`
и `needs_deep_analysis`. Dedup/cooldown включает tenant, destination, dialog, signal/problem type и
severity bucket. Critical immediate alert может bypass обычный cooldown.

**Scheduled analysis.** Root `Tenant Analysis Run` создаёт account child runs и один aggregation
barrier. Report генерируется один раз после всех account runs; отдельные почти одинаковые reports
на каждый аккаунт больше не создаются.

**Context.** Сначала резервируется свежий хвост, затем релевантные historical evidence и compact
summary старой истории. Старые сообщения не могут вытеснить последние важные события.

### Mini App как control center

Существующая mobile-first структура сохранена. Через typed API/service boundary добавлены:

- live problems list и detail;
- evidence, expected action, responsible, deadline, transitions и verification history;
- допустимые actions acknowledge/assign/in-progress/waiting/resolve/reopen/false-positive;
- отдельный commitments screen с deadline, responsible/link и manual completion;
- создание/редактирование Employee, Telegram mapping, role, access, notifications и threshold;
- реальные GET/PATCH settings: timezone, schedule/history и notification policy;
- управление group notification enablement/threshold;
- report detail, metrics, sections и linked problem IDs.

Backend остаётся source of truth для переходов, permissions и tenant boundaries. Визуальные
компоненты не знают Telegram HMAC, tenant resolution или API URL.

### Observability и hardening

Доступны:

- `/health/live`;
- `/health/ready` (совместно с legacy `/ready`);
- `/health/details`;
- `/metrics` в JSON.

Metrics включают queue depth/by category, oldest job age, job p50/p95/p99, retry/failure rate,
Telegram fetch p50/p95/p99, message→signal и signal→notification latency, AI latency/errors/invalid
JSON, persisted FloodWait/SQLite lock failures, overdue reports и notification failures.

Structured JSON logging allow-list расширен безопасными полями `tenant_id`, `account_id`,
`dialog_id`, `job_id`, `correlation_id`, `stage`, `category`, `worker_id`, duration/status/retry. Текст
сообщений, token и sessions в обычный log context не добавляются.

## Миграции

- `20260808_0010_problem_lifecycle.py`: lifecycle columns, `problem_transitions`,
  `problem_verifications`, legacy `open → new` normalization;
- `20260808_0011_notification_policy.py`: отдельные notification thresholds;
- `20260808_0012_critical_fast_lane.py`: tenant-specific critical rules.

Миграционный тест применяет всю цепочку Alembic на пустой SQLite database.

## Тесты

Добавлены/расширены проверки:

- category-aware worker claim и одновременные SQLite workers;
- idempotent/repeated jobs, stale lease recovery и lock settings;
- duplicate Telegram messages и cursor recovery после нового ingestion instance;
- initial sync через общий Signal lifecycle;
- problem transition validation, audit, assignment, closure и reopen;
- remediation acknowledgment vs actual completion;
- employee membership/role/permission synchronization и employee isolation;
- deadline parsing и rollover edge cases;
- overdue commitment correlation/closure;
- notification dedup, scoped cooldown и critical bypass;
- provisional critical alert plus AI cancellation;
- multi-account aggregation barrier и один consolidated report;
- fresh-tail context with historical evidence;
- long-lived Telethon client reuse/close;
- secret scan/source export;
- health/ready/metrics contracts.

Фактически выполнено локально:

```text
.venv/bin/pytest -q
86 passed in 7.90s

.venv/bin/ruff check services/backend app tests/backend/...
All checks passed

./node_modules/.bin/tsc --noEmit
passed

npm run lint
passed

npm run build
Next.js production build passed

docker compose config --quiet
passed

docker compose build
backend, background-worker, client-bots, owner-bot and scheduler images built
```

Python `mypy` не указан в проекте как рабочий quality gate: пробный полный запуск обнаружил сотни
существовавших до этого этапа ошибок аннотаций во всём backend. Новых ошибок не маскировали, но
утверждать, что repository-wide mypy проходит, нельзя.

## Что осталось и известные ограничения

- Telegram push/live updates не включены: `receive_updates=False`, periodic cursor polling остаётся
  надёжным recovery path. Добавлять второй частично управляемый event loop без отдельного connector
  runtime было бы рискованнее для пилота.
- Auth перебирает активные bot tokens для проверки `initData`; при десятках tenants это приемлемо,
  но позднее нужен безопасный bot identity hint/cache, не доверяющий query tenant ID.
- SQLite остаётся single-host/single-writer storage. Нет multi-host failover и внешних distributed
  locks; переход должен основываться на telemetry lock/latency, а не выполняться заранее.
- `/metrics` — pilot JSON endpoint по последним persisted records, не Prometheus/OpenTelemetry и не
  долговременное observability хранилище.
- Нет production load/soak benchmark на реальном объёме 10–50 компаний и реальной DeepSeek/Telegram
  latency. Это обязательный release exercise перед расширением пилота.
- Report UI показывает структурированные backend sections без финальной предметной визуализации;
  отдельные Dialogues/Clients/Meetings/Pipeline/Finance/System Problems screens остаются будущим P2.
- GroupIntegration — разрешённая destination для notification/reminder, а не независимый ingestion
  source: сообщения по-прежнему получает явно подключённый Telegram user account.
- Semantic verification зависит от доступности AI provider; при timeout/error проблема остаётся
  открытой, что является fail-safe поведением.
- KMS/HSM, отдельная service identity для connector и юридическая проверка Telegram user-session
  эксплуатации выходят за рамки текущего single-host пилота.

## Рекомендации следующего scale stage

1. Провести 7–14-дневный shadow/pilot прогон на нескольких согласованных tenants и зафиксировать
   queue age, p95/p99, FloodWait, lock retry, false-positive и notification delivery SLO.
2. После telemetry решить, нужен ли сначала PostgreSQL, отдельный connector process или Redis queue;
   не мигрировать все компоненты одновременно.
3. Добавить supervised live-update fast path в Telegram connector, сохранив polling reconciliation.
4. Экспортировать `/metrics` в Prometheus/OpenTelemetry и настроить alerts на queue age, scheduler
   heartbeat, overdue reports, notification failures и AI invalid JSON.
5. Провести restore drill, graceful restart/rolling restart и provider outage game day на VPS.
6. Довести report/evidence drill-down и role-specific mobile UX по данным реального пилота.
7. Провести отдельный security/privacy review Telegram sessions, retention и operator access до
   масштабного коммерческого подключения.
