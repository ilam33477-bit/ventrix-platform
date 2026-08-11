# Аудит проекта Ventrix / B2B AI Checker

## Итоговая оценка

Я разобрал проект из архива как целую систему: FastAPI-бэкенд, Telegram user-session через Telethon, клиентские Telegram-боты, scheduler, SQLite-очередь, background worker, локальные детекторы, AI-triage через DeepSeek, обязательства, уведомления, регулярный анализ, отчёты и Mini App.

**Главный вывод:** проект уже не является прототипом интерфейса. Внутри есть достаточно серьёзное backend-ядро с персистентной очередью, идемпотентностью, повторными попытками, Telegram-сессиями, tenant-изоляцией, AI-бюджетами, сигналами, обязательствами, reconciliation и системой уведомлений. Но сейчас архитектура находится примерно в середине перехода от **«рабочего MVP» к «полноценной production-системе»**. Это особенно видно по трём местам: параллелизм фактически почти не используется, жизненный цикл проблемы реализован лишь частично, а Mini App значительно отстаёт от уже существующего backend API. См. `services/backend/jobs/worker.py:L53-L102`, `services/backend/jobs/queue.py:L81-L340`, `services/backend/api/client_router.py:L1033-L1078`, `docs/product-and-architecture.md:L104-L140`.

Моя оценка реализации **всего задуманного продукта — около 64% ± 5 п.п.** Это не процент строк кода, а оценка законченности бизнес-сценариев: подключить аккаунт → получать сообщения → детектировать проблему → определить ответственного → уведомить → управлять проблемой → проверить исправление → отразить в отчёте → дать сотруднику/руководителю полноценный интерфейс. По отдельности картина выглядит примерно так:

| Область | Оценка |
|---|---:|
| Фундамент, tenant-модель, БД, шифрование, очередь | **85%** |
| Подключение Telegram и загрузка истории | **80%** |
| Инкрементальный анализ новых сообщений | **75%** |
| AI-triage, бюджеты, дедупликация | **75%** |
| Критические уведомления | **70%** |
| Регулярный анализ и отчёты | **70%** |
| Полноценный lifecycle проблемы | **35–40%** |
| Работа сотрудников / RBAC end-to-end | **40%** |
| Mini App как полноценная панель управления | **45%** |
| Производительность и горизонтальное масштабирование | **45–50%** |
| Production hardening / observability / нагрузочное подтверждение | **50%** |

Эта оценка сопоставлена с собственным продуктовым документом проекта, где конечной единицей системы заявлена не просто AI-находка, а **проблема с доказательствами, ответственным, дедлайном, жизненным циклом и проверкой фактического исправления**. `docs/product-and-architecture.md:L3-L21`, `docs/product-and-architecture.md:L104-L125`.

Самая важная мысль: **основная интеллектуальная backend-машина уже существует. Основной недоделанный пласт сейчас — объединение её частей в один единый lifecycle и полноценное управление этим lifecycle из Mini App.**

## Как система сейчас устроена глобально

В очень простых словах текущую систему можно представить так:

```text
Telegram-аккаунт сотрудника
        ↓
Telethon
        ↓
выбранные рабочие папки / диалоги
        ↓
новые сообщения
        ↓
локальный быстрый анализ
        ↓
Signal
        ↓
при необходимости AI triage
        ↓
OperationalProblem / Commitment
        ↓
уведомление
  ↙        ↓        ↘
админ   сотрудник   группа
        ↓
периодическая проверка
        ↓
закрыто / просрочено / всё ещё открыто
        ↓
регулярный глубокий анализ
        ↓
отчёт
        ↓
Mini App / клиентский бот
```

### Основные процессы

Приложение по умолчанию запускается как несколько отдельных процессов: `backend`, `owner-bot`, `background-worker`, `scheduler` и `client-bots`. Все они используют одну локальную SQLite-базу через общий volume. `docker-compose.yml:L1-L64`.

Это хорошая MVP-модель: падение клиентского бота не обязательно роняет FastAPI, scheduler отделён от выполнения заданий, а тяжёлые операции вынесены из API-запросов в очередь. В документации это прямо зафиксировано как single-server modular monolith. `docs/product-and-architecture.md:L23-L63`, `docs/SCHEDULER_ANALYSIS_ARCHITECTURE.md:L3-L13`.

Важное отличие от обычного Telegram-бота: **рабочие сообщения анализируются не самим ботом**, а подключённым Telegram user account через Telethon. Пользователь проходит авторизацию, выбирает рабочую папку, глубину истории и разрешение на личные рабочие диалоги. Mini App уже реализует телефон → код → 2FA → папка → история → старт синхронизации. `app/mini-app/features/connections/connection-manager.tsx:L23-L119`, `app/mini-app/api/client.ts:L78-L118`.

То есть архитектурно сейчас:

> **Telegram user-session = источник данных.  
> Tenant bot = интерфейс, уведомления и Mini App.**

Это важное разграничение.

### Три разных контура анализа

При изучении проекта обнаружилась важная архитектурная особенность: сейчас фактически существуют **три несколько разных механизма анализа**.

**Первичная история.** При первом подключении загружается история диалога порциями, а затем выполняется отдельный эвристический анализ. Он определяет, например, клиента без ответа, жалобу, обещания и потенциальные сделки. `services/backend/telegram_sessions/sync.py:L49-L168`, `services/backend/telegram_sessions/sync.py:L217-L371`.

**Новые сообщения.** Здесь используется более современная цепочка:

```text
TelegramMessage
→ deterministic local signal
→ Signal
→ Commitment при необходимости
→ AI triage
→ OperationalProblem
→ Notification
```

Это видно в `services/backend/intelligence/local_signals.py:L47-L165`, `services/backend/intelligence/signals.py:L68-L222`, `services/backend/intelligence/ai_triage.py:L56-L263`.

**Регулярный глубокий анализ.** Scheduler отдельно запускает `analysis.pipeline`, который подготавливает диалоги, отправляет AI-batches и строит отчёт. `services/backend/scheduler/service.py:L142-L195`, `services/backend/analysis/service.py:L86-L195`.

Именно эта тройственность сейчас является одним из главных источников сложности: одна и та же бизнес-ситуация потенциально может проходить через разные модели данных в зависимости от того, была она найдена при первоначальной загрузке, при новом сообщении или при регулярном анализе.

В идеальной следующей архитектуре все три пути должны сходиться примерно сюда:

```text
любое наблюдение
      ↓
единый BusinessEvent / Signal
      ↓
единый dedup/correlation
      ↓
единый Problem lifecycle
      ↓
единые notifications
      ↓
единая verification
      ↓
единая reporting model
```

Это лучше соответствует исходной архитектурной задумке самого проекта. `docs/product-and-architecture.md:L27-L47`, `docs/product-and-architecture.md:L127-L139`.

## Сообщения, очередь, параллелизм и нагрузка

### Как новые сообщения попадают в систему

Это сейчас **не настоящий Telegram event-stream в режиме push**.

Telethon-клиенты создаются с:

```python
receive_updates=False
```

то есть live-update stream отключён. `services/backend/telegram_sessions/gateway.py:L105-L118`.

Вместо этого scheduler регулярно создаёт jobs `telegram.fetch_updates`. По текущим настройкам incremental sync запускается примерно каждые **30 секунд**, сам scheduler также работает polling-циклом. `services/backend/config.py:L32-L63`, `services/backend/scheduler/service.py:L197-L256`.

Поэтому текущий механизм правильнее называть:

> **near-real-time polling**, а не real-time events.

В идеальных условиях новое сообщение будет обнаружено примерно в пределах одного polling-интервала, а затем к этому добавятся ожидание в очереди, Telegram API, локальный анализ, AI-запрос и отправка уведомления. При пустой системе это может ощущаться почти мгновенно; при очереди задержка может заметно вырасти. `services/backend/scheduler/service.py:L197-L256`, `services/backend/jobs/worker.py:L72-L102`.

### Что происходит после fetch

Для одной Telegram connection ingestion проходит выбранные диалоги, получает сообщения после сохранённого cursor, сохраняет новые сообщения и создаёт локальные сигналы. `services/backend/telegram_sessions/incremental.py:L41-L100`.

Для каждого нового сообщения дополнительно выполняется поиск дубликата, загрузка соседнего контекста, сохранение и локальный анализ. `services/backend/telegram_sessions/incremental.py:L162-L227`.

Механизм корректен с точки зрения MVP и идемпотентности, но при большом потоке становится довольно «разговорчивым» с SQLite: много отдельных SELECT/INSERT на сообщение.

### Очередь сделана заметно лучше, чем сам worker

Очередь — одна из самых сильных частей проекта.

Job содержит tenant, account/dialog context, idempotency key, priority, attempts, scheduled time, category и cost-класс. `services/backend/jobs/queue.py:L81-L134`.

При claim очередь учитывает:

- количество уже работающих задач tenant;
- тяжёлые jobs tenant;
- лимиты категорий;
- приоритет;
- fairness между tenant;
- атомарный перевод job в `running`.  
  `services/backend/jobs/queue.py:L136-L273`.

Есть heartbeat, retry, exponential backoff и восстановление протухшей lease. `services/backend/jobs/queue.py:L275-L369`.

То есть **очередь уже проектировалась с расчётом на несколько workers**.

Проблема в другом.

### Фактически параллелизм сейчас почти отсутствует

`BackgroundWorker.run_once()` забирает **одну job**, запускает heartbeat и затем делает:

```python
result = await handler(lease)
```

После завершения он берёт следующую. `services/backend/jobs/worker.py:L72-L102`.

Основной цикл также последовательно вызывает `worker.run_once()`. `services/backend/jobs/worker.py:L227-L242`.

И в стандартном `docker-compose.yml` существует ровно **один** `background-worker`. `docker-compose.yml:L30-L40`.

Отсюда принципиальный вывод:

> Ограничения вроде `2 AI jobs`, `2 tenant jobs`, `1 telegram job`, `1 heavy job` уже предусмотрены очередью, но при стандартном запуске большая их часть фактически не используется, потому что один worker всё равно выполняет одну job за раз.

Например:

```text
AI triage #1   8 секунд
↓
fetch Telegram
↓
report
↓
critical notification
↓
AI triage #2
```

Если первая AI-job зависла на HTTP-запросе на 15 секунд, worker не начнёт Telegram fetch или следующую notification job в это время. Heartbeat работает отдельной coroutine, но вторая бизнес-job не выполняется параллельно. `services/backend/jobs/worker.py:L66-L102`.

Это **главное текущее бутылочное горлышко по latency**.

### Почему нельзя просто запустить двадцать workers

Архитектура допускает запуск нескольких worker-процессов — claim job сделан достаточно аккуратно. Но база остаётся SQLite.

Для SQLite включены WAL, `synchronous=NORMAL` и `busy_timeout`; кроме этого, внутри каждого процесса write-транзакции ограничиваются semaphore. `services/backend/database.py:L23-L34`, `services/backend/database.py:L52-L72`, `services/backend/database.py:L104-L129`.

README проекта сам правильно фиксирует пределы: SQLite сериализует записи, общая схема ориентирована на один host, отсутствуют внешняя распределённая очередь и distributed locks; PostgreSQL/Redis обозначены как следующий этап при высокой конкуренции и multi-host scaling. `README.md:L230-L248`.

Поэтому правильное развитие будет не:

```text
1 worker → 30 workers на SQLite
```

а скорее:

```text
сейчас:
SQLite + 1 worker

ближайший MVP:
SQLite + 2–4 аккуратно разделённых workers

после подтверждённой нагрузки:
PostgreSQL
+
Redis / broker
+
telegram workers
+
AI workers
+
notification workers
+
report workers
```

### Ещё один дорогой участок — Telethon

В текущей реализации `fetch_new_messages()` создаёт Telegram client, подключается, получает данные и disconnect-ится для отдельного диалога. `services/backend/telegram_sessions/gateway.py:L252-L299`.

А incremental ingestion проходит выбранные dialogs последовательно. `services/backend/telegram_sessions/incremental.py:L41-L100`.

При условных 5–20 чатах это приемлемо.

При 100–500 диалогах на аккаунт это уже становится дорогой схемой:

```text
connect
fetch chat 1
disconnect

connect
fetch chat 2
disconnect

connect
fetch chat 3
disconnect
...
```

Здесь одна из самых больших будущих оптимизаций — **долго живущий Telethon client на Telegram account**, reuse connection и в перспективе получение live updates.

### Производительность в текущей форме

Грубо:

**Малый пилот:** архитектура достаточная.

Например:

```text
5–20 компаний
несколько Telegram accounts
десятки рабочих чатов на account
умеренный поток сообщений
```

система вполне соответствует классу MVP.

**Десятки/сотни активно пишущих компаний:** начнут проявляться:

```text
single worker
    ↓
AI latency
    ↓
job backlog
    ↓
задержки Telegram sync
    ↓
задержки critical alerts
```

и параллельно:

```text
много dialogs
    ↓
много Telethon connect/disconnect
    ↓
network / Telegram latency
```

плюс:

```text
много new messages
    ↓
много SQLite reads/writes
    ↓
single-writer contention
```

Это следует непосредственно из текущей worker/SQLite/Telethon-модели. `services/backend/jobs/worker.py:L72-L102`, `services/backend/database.py:L104-L129`, `services/backend/telegram_sessions/gateway.py:L252-L299`.

## Анализ, критические события, уведомления и отчёты

### Ключевые слова уже есть, но они не являются «аварийной кнопкой»

В проекте действительно существует локальный быстрый анализ текста. Он ищет категории вроде:

- цена / коммерческий интерес;
- договор / КП;
- оплата / счёт;
- документы;
- обещание сотрудника;
- жалоба;
- следующий шаг;
- дата/время;
- повторное обращение;
- отсутствие ответа.  
  `services/backend/intelligence/local_signals.py:L10-L35`, `services/backend/intelligence/local_signals.py:L50-L165`.

Но в самом коде прямо зафиксировано, что этот компонент — дешёвый deterministic candidate generator и **сам по себе не объявляет критический инцидент**. `services/backend/intelligence/local_signals.py:L47-L48`.

То есть сейчас нет логики:

```text
увидели "СРОЧНО"
→ немедленно отправили админу
```

Текущая цепочка скорее:

```text
сообщение
↓
regex/heuristic detector
↓
local score
↓
Signal
↓
если достаточно важно → AI triage
↓
AI criticality
↓
Problem
↓
Notification
```

`services/backend/intelligence/signals.py:L68-L169`, `services/backend/intelligence/ai_triage.py:L56-L263`.

Это, на мой взгляд, концептуально правильно: одно слово «срочно» не должно превращаться в критический инцидент без контекста.

Но для **абсолютно критичных паттернов** можно добавить отдельный deterministic fast lane:

```text
message
 ├─ ordinary candidate → AI triage
 └─ hard-critical rule → immediate provisional alert
                         ↓
                      AI confirms
```

Например для специально настраиваемых клиентом маркеров, где цена false negative выше цены false positive.

### AI triage реализован достаточно серьёзно

AI получает не просто отдельную фразу, а:

- signal features;
- dialog state;
- текущее сообщение;
- несколько предыдущих сообщений;
- открытые commitments;
- SLA/context.  
  `services/backend/intelligence/ai_triage.py:L122-L183`.

Схема результата предусматривает criticality, category, причину, recommended action, deadline и флаги уведомления менеджера/сотрудника, а также необходимость deep analysis. `services/backend/intelligence/triage.py:L8-L27`.

Предусмотрены soft/hard AI budgets. При превышении soft limit порог дальнейшего AI-триажа повышается; hard limit может отложить некритичный запрос, чтобы tenant не потратил неограниченный бюджет. `services/backend/intelligence/signals.py:L147-L169`, `services/backend/intelligence/ai_triage.py:L185-L206`.

Это хорошая production-oriented идея.

### Но часть AI-ответа пока не доведена до действия

Здесь есть важный разрыв.

Модель возвращает, среди прочего:

```text
requires_employee_notification
requires_manager_notification
recommended_deadline_minutes
needs_deep_analysis
```

`services/backend/intelligence/triage.py:L8-L27`.

Однако downstream notification planning в основном повторно принимает решение по criticality и настройкам tenant/employee/group, а не использует все эти AI-флаги как полноценную policy-модель. Аналогично `needs_deep_analysis` не превращён в полноценный автоматически запускаемый deep-analysis branch. `services/backend/intelligence/ai_triage.py:L208-L263`, `services/backend/intelligence/notifications.py:L38-L74`.

То есть **schema AI уже богаче, чем фактически подключённая бизнес-логика**.

### Обещания сотрудников — одна из наиболее интересных частей

При исходящем сообщении сотрудника local detector может распознать обещание и создать `Commitment`:

```text
«отправлю КП завтра»
          ↓
employee_commitment
          ↓
Commitment
          ↓
responsible employee
          ↓
deadline
```

`services/backend/intelligence/signals.py:L197-L222`.

Reconciliation затем проверяет открытые commitments. Если после обещания найдено более позднее исходящее сообщение с completion pattern, commitment может быть закрыт; если дедлайн прошёл — создаются overdue Signal + OperationalProblem и затем планируется уведомление. `services/backend/intelligence/reconciliation.py:L41-L90`, `services/backend/intelligence/reconciliation.py:L108-L205`.

Это уже соответствует важной части концепции Ventrix: не просто классифицировать сообщение, а **помнить обещание во времени**.

### Но разбор дедлайна сейчас слабый

Есть конкретная логическая недоделка: regex может увидеть явную дату, но deadline parser в текущем виде в основном работает через относительные смещения вроде сегодня/завтра/послезавтра и время по умолчанию. `services/backend/intelligence/local_signals.py:L175-L196`.

Поэтому фразы уровня:

> «отправлю 15.08»

требуют более надёжного date parser. Иначе возможна ситуация, когда обещание найдено правильно, а контрольный дедлайн поставлен неправильно.

Для системы, обещающей контролировать сроки, это **P0/P1 correctness issue**, а не косметическая функция.

### Проверка закрытия проблемы сейчас слишком простая

Для категории «клиент ждёт ответа» проблема считается решённой, если после исходного сообщения существует **любое** более позднее outgoing сообщение. `services/backend/intelligence/reconciliation.py:L209-L221`.

То есть технически:

```text
Клиент: Где договор?
Сотрудник: Ок
```

может оказаться достаточным признаком «ответ был».

Но исходная продуктовая концепция говорит более сильную вещь: система должна **проверить, что underlying conversation действительно изменилась и проблема исправлена**. `docs/product-and-architecture.md:L5-L21`.

Поэтому reconciliation сейчас есть, но **semantic remediation verification пока не закончена**.

Здесь очень полезен отдельный verifier:

```text
problem evidence
+
expected remediation
+
new messages after problem
↓
small deterministic/AI verifier
↓
fixed / not fixed / uncertain
```

### Уведомления администратору уже работают на уровне backend-логики

Notification planner умеет рассчитывать destinations:

- сотрудник;
- manager/owner;
- рабочая группа.  
  `services/backend/intelligence/notifications.py:L38-L74`, `services/backend/intelligence/notifications.py:L151-L213`.

Для отправки используется активный tenant bot: его token извлекается и расшифровывается, после чего NotificationDispatcher отправляет сообщение через Telegram Bot API. `services/backend/intelligence/notifications.py:L94-L134`, `services/backend/intelligence/notifications.py:L282-L324`.

Уведомления сами являются durable jobs, а ошибки сохраняются и могут быть повторены. Это правильное решение: AI-анализ не должен считаться проваленным только потому, что Telegram временно не принял notification. `services/backend/intelligence/notifications.py:L151-L267`.

### Порог сотрудника сейчас работает не совсем так, как можно ожидать

По текущей логике есть:

```text
employee.criticality_threshold
```

но непосредственно отправка сотруднику дополнительно привязана к понятию `immediate`.

В результате при дефолтной конфигурации manager получает проблемы примерно с problem threshold, а employee/group в основном включаются для immediate-событий. `services/backend/intelligence/notifications.py:L38-L74`.

Следовательно, настройка сотруднику, например, порога 70 не обязательно означает, что он получит каждую ситуацию criticality=70.

Я бы разделил это на прозрачную матрицу:

```text
manager_threshold
employee_threshold
group_threshold
immediate_threshold
```

без дополнительного скрытого пересечения условий.

### Cooldown может подавить не только дубликаты

Есть notification cooldown, по умолчанию порядка 120 минут. `services/backend/config.py:L32-L63`.

При планировании проверяется недавняя отправка destination. `services/backend/intelligence/notifications.py:L215-L267`.

Это защищает от спама, но при большом объёме может скрывать **два разных серьёзных события одному и тому же администратору**.

Лучше делать suppression key чем-то вроде:

```text
destination
+
dialog
+
problem_type
+
severity bucket
```

а P0 critical позволять bypass cooldown.

### Регулярный анализ работает

Scheduler хранит расписание и запускает tenant analysis по timezone, report time, enabled days и advance time. `services/backend/scheduler/service.py:L142-L195`.

Дополнительно существуют:

```text
incremental analysis → примерно каждые 30 секунд
hourly reconciliation → примерно каждый час
scheduled/deep report → по расписанию tenant
```

`services/backend/scheduler/service.py:L197-L256`, `services/backend/config.py:L32-L63`.

Таким образом, задуманные «два режима» уже присутствуют:

**операционный быстрый контур**

```text
новое сообщение → проблема → notification
```

и **управленческий контур**

```text
накопленная история → deep analysis → отчёт
```

Это хорошее архитектурное разделение.

### Но scheduled analysis имеет потенциальную проблему с несколькими Telegram accounts

`analysis.pipeline` fan-out-ится по Telegram connections и создаёт отдельный AnalysisRun на connection. `services/backend/analysis/service.py:L86-L141`, `services/backend/analysis/service.py:L265-L301`.

При этом построение отчёта использует tenant-wide problems/signals/commitments/employees/dialogs. `services/backend/analysis/service.py:L404-L603`.

Из этого следует важный риск текущей схемы:

> для tenant с несколькими Telegram connections один scheduled cycle может породить несколько очень похожих tenant-wide отчётов.

Это стоит исправить до активного multi-account использования.

Правильнее:

```text
Tenant Analysis Run
   ├─ Account A batches
   ├─ Account B batches
   ├─ Account C batches
   ↓
единственная aggregation barrier
   ↓
единственный tenant report
```

### В длинных чатах scheduled analysis может анализировать не ту часть истории

Preprocessing загружает сообщения по времени и затем ограничивает compact context размером. `services/backend/analysis/preprocessing.py:L101-L198`.

По текущей последовательности есть риск, что при достижении лимита символов будут сохранены ранние сообщения окна, а самый новый хвост очень активного диалога в prompt не попадёт. Это вывод из порядка выборки и early break в compaction. `services/backend/analysis/preprocessing.py:L59-L86`, `services/backend/analysis/preprocessing.py:L151-L189`.

Для Ventrix это особенно опасно: управленчески важнее обычно **последнее развитие ситуации**, чем начало 30-дневного окна.

Я бы перестроил context:

```text
последние N сообщений — всегда
+
ключевые historical evidence
+
summary старой части
```

## Mini App, сотрудники, группы и граница фронта с бэкендом

### Backend API значительно опережает интерфейс

В backend уже существуют endpoints не только для просмотра, но и для управления:

- problems;
- problem details и изменение status;
- reports и report detail;
- signals;
- commitments;
- employees;
- group integrations;
- Telegram connections;
- AI usage;
- запуск/отмена анализа;
- settings.  
  См. набор routes в `services/backend/api/client_router.py`, в частности `L1033-L1078`, `L1231-L1316`.

А frontend API-клиент сейчас реализует существенно меньший набор:

```text
auth
bootstrap
onboarding
employees list
connections list
groups list
reports list
Telegram login
Telegram 2FA
folder catalog
scope selection
cancel login
```

`app/mini-app/api/client.ts:L41-L118`.

То есть backend уже имеет функциональность, которой Mini App пока просто не пользуется.

### Что Mini App реально умеет сейчас

Основные вкладки уже есть:

```text
dashboard
problems
statistics
reports
employees
connections
groups
settings
more
```

`app/mini-app/mini-app-root.tsx:L16-L48`, `app/mini-app/layout/navigation.ts:L3-L17`.

Самая законченная интерактивная часть — **подключение Telegram account**: сотрудник, телефон, OTP, 2FA, folder scope, history depth, personal-dialog consent, запуск initial sync. `app/mini-app/features/connections/connection-manager.tsx:L23-L119`.

А значительная часть остальных экранов пока read-only.

Например Employees просто показывает сотрудников. `app/mini-app/features/sections/section-views.tsx:L16-L20`.

Groups просто показывает интеграции. `app/mini-app/features/sections/section-views.tsx:L22-L26`.

Reports показывает список summary, но не полноценный drill-down. `app/mini-app/features/sections/section-views.tsx:L10-L14`.

Settings вообще пока статический интерфейс без вызовов settings API. `app/mini-app/features/sections/section-views.tsx:L28-L30`.

Problems — в основном просмотр и клиентский фильтр, а не полноценный workflow управления problem lifecycle. `app/mini-app/features/problems/problems-view.tsx:L8-L16`.

### Поэтому текущий Mini App — скорее monitor, чем control center

Сейчас:

```text
посмотреть состояние       ✅
посмотреть проблемы        ✅
посмотреть сотрудников     ✅
посмотреть группы          ✅
посмотреть отчёты          ✅
подключить Telegram        ✅

создать сотрудника         ❌ UI
настроить его alerts       ❌ UI
назначить permissions      ❌ UI
изменить settings          ❌ UI
открыть problem detail     🟡
назначить ответственного   ❌
ввести deadline            ❌
подтвердить проблему       ❌/🟡
false positive workflow    ❌ UI
открыть evidence chain     ❌
commitment management      ❌ UI
report drill-down          ❌
ручной deep analysis       ❌ UI
```

Backend при этом уже поддерживает часть этих действий, поэтому значительная часть следующего прогресса может быть получена **без переписывания аналитического ядра**. `services/backend/api/client_router.py:L1033-L1078`, `services/backend/api/client_router.py:L1231-L1316`, `app/mini-app/api/client.ts:L41-L118`.

### Добавить Employee и дать Employee доступ в Mini App — сейчас две разные вещи

Это одна из самых важных найденных дыр.

В БД есть сущность `Employee`. У неё есть Telegram user ID, username, role, notification settings и criticality threshold. `services/backend/models.py:L1021-L1048`.

Есть отдельно `TenantMembership`, роли и permissions. `services/backend/models.py:L1152-L1183`.

Mini App auth требует действующую tenant membership пользователя. `services/backend/api/client_router.py:L239-L291`.

Но endpoint:

```text
POST /employees
```

создаёт `Employee` и **не создаёт автоматически `TenantMembership`**. `services/backend/api/client_router.py:L1060-L1078`.

При создании tenant membership явно создаётся для OWNER. `services/backend/services/foundation.py:L127-L134`.

Следовательно:

> Админ уже может завести сотрудника для аналитики и уведомлений, но это ещё не означает, что сотрудник автоматически получил доступ к своему Mini App.

Это важный незаконченный end-to-end сценарий.

Нужно связать:

```text
Employee
↓
Telegram user
↓
TenantMembership
↓
role
↓
permissions
↓
Mini App access
```

### Сотруднику уведомление отправляться может

Это другой сценарий и он уже ближе к завершению.

SignalService пытается связать Telegram sender/outgoing account с Employee. `services/backend/intelligence/signals.py:L171-L194`.

Если Employee имеет Telegram user ID и notifications enabled, NotificationPlanner может выбрать его в качестве direct destination. `services/backend/intelligence/notifications.py:L38-L74`, `services/backend/intelligence/notifications.py:L151-L213`.

Поэтому:

> **«Админ добавил сотрудника → система знает, к кому относится проблема → уведомила сотрудника»** — backend-основа есть.

А:

> **«Админ добавил сотрудника → сотрудник вошёл в Mini App → видит только свои проблемы → управляет ими»**

— пока не закончено.

### Сам клиентский бот сейчас преимущественно owner-only

Middleware клиентского бота проверяет пользователя против owner Telegram user ID tenant. `services/backend/client_bots/handlers.py:L55-L113`.

Следовательно, сотрудники сейчас не имеют полноценного interactive tenant-bot интерфейса, аналогичного владельцу. Они могут выступать destination для уведомлений, но это не то же самое, что роль в клиентском интерфейсе.

### Рабочая группа и источник анализа — сейчас не одно и то же

Это критически важно понимать.

Существуют `GroupIntegration`, логика проверки участия бота в группе и обработка `my_chat_member`. `services/backend/client_bots/handlers.py:L1027-L1074`.

Но основным источником анализируемых сообщений являются **Telethon session + выбранная рабочая папка**, а не сообщения, которые tenant bot получает как generic group bot. `services/backend/telegram_sessions/gateway.py:L252-L299`, `services/backend/telegram_sessions/incremental.py:L41-L100`.

То есть модель:

```text
добавили Ventrix Bot в любую группу
↓
бот прочитал всю группу
↓
начал анализировать
```

в текущем backend **не является главным реализованным ingestion path**.

Сейчас скорее:

```text
подключили рабочий Telegram account
↓
выбрали папку, содержащую группу
↓
Telethon account читает её
↓
анализирует
```

а GroupIntegration может выступать местом для reminders/notifications.

Если ваша продуктовая задумка именно **«добавил бота в рабочий чат и всё сразу заработало»**, эта часть ещё требует отдельной реализации.

### Frontend и backend технически разделены хорошо

Next.js frontend делает REST-запросы к FastAPI через configurable `baseUrl`. `app/mini-app/api/client.ts:L19-L38`.

Поэтому их можно разворачивать и развивать отдельно.

Но runtime-authentication Mini App тесно связан с Telegram: каждый запрос несёт:

```text
Authorization: tma <initData>
```

`app/mini-app/api/client.ts:L25-L33`.

Без Telegram launch state Mini App показывает экран «откройте через Telegram». `app/mini-app/mini-app-root.tsx:L20-L24`.

И backend для валидации auth проверяет доступные bot instances, после чего ищет membership. `services/backend/api/client_router.py:L239-L291`.

То есть:

**На уровне кода/deployment:** frontend от backend отделён хорошо.

**На уровне продукта/auth:** frontend намеренно зависит от Telegram.

### В auth есть будущий bottleneck

Текущая auth-логика перебирает активные client bot instances и пытается подобрать bot token, которым подписан `initData`. `services/backend/api/client_router.py:L239-L291`.

При нескольких bot instances это нормально.

При тысячах tenant bots это становится потенциально линейной операцией на API request:

```text
request
↓
bot 1?
bot 2?
bot 3?
...
bot N?
```

Для будущего масштаба лучше:

```text
Telegram initData
↓  один раз
backend verifies
↓
короткоживущая signed session / JWT
↓
последующие API requests O(1)
```

с tenant/user/permissions внутри server-signed claims.

## Зрелость, бутылочные горлышки и риски

### Критические архитектурные приоритеты

Я бы оценил проблемы по важности так:

| Приоритет | Проблема | Почему важно |
|---|---|---|
| 🔴 P0 | Один sequential background worker | Critical alerts конкурируют с AI, sync и reports |
| 🔴 P0 | Разные lifecycle для initial/incremental/deep анализа | Одинаковая проблема может вести себя по-разному |
| 🔴 P0 | Problem FSM из domain слоя не подключён полностью к persistent/API workflow | Нарушается ключевое обещание продукта |
| 🔴 P0 | Employee ≠ Membership | Нет законченного employee Mini App access |
| 🔴 P0 | Архив содержит `.env` и backup data | Риск утечки секретов при передаче сборки |
| 🟠 P1 | Telethon connect/disconnect на dialogs | Масштабирование Telegram ingestion |
| 🟠 P1 | Не настоящий live-update ingestion | Critical alert latency зависит от polling |
| 🟠 P1 | Deadline parser | Возможны неправильные сроки обещаний |
| 🟠 P1 | Простая verification «любое исходящее = ответ» | False resolution |
| 🟠 P1 | Notification cooldown слишком общий | Возможна потеря второго важного alert |
| 🟠 P1 | Несколько reports при нескольких connections | Дублирование отчётов |
| 🟠 P1 | Scheduled context может терять свежий хвост | Ошибка анализа активных чатов |
| 🟡 P2 | Mini App read-heavy | Backend возможности недоступны пользователю |
| 🟡 P2 | Полный initData на каждом API request | Будущий auth scaling issue |
| 🟡 P2 | SQLite как общий write store | Естественный предел роста |

Основание для этих выводов: `services/backend/jobs/worker.py:L72-L102`, `services/backend/telegram_sessions/gateway.py:L252-L299`, `services/backend/intelligence/reconciliation.py:L209-L221`, `services/backend/api/client_router.py:L239-L291`, `docs/product-and-architecture.md:L104-L125`.

### Самое важное архитектурное несоответствие — Problem lifecycle

В `packages/ops_core` уже существует более правильная доменная модель проблемы с transition rules. Исходный product document также описывает состояния:

```text
new
→ needs_confirmation
→ acknowledged
→ assigned
→ in_progress
→ waiting
→ resolved / auto_resolved
→ reopened
```

и требует evidence при закрытии. `docs/product-and-architecture.md:L104-L125`, `packages/ops_core/problems.py:L9-L79`.

Но живой `OperationalProblem` и client API работают намного проще: status хранится строкой, а API меняет его напрямую. Полноценные assignment, transition audit и verification пока не соединены с этим workflow. `services/backend/models.py:L689-L724`, `services/backend/api/client_router.py:L54-L55`, `services/backend/api/client_router.py:L722-L737`.

Именно поэтому я не ставлю всему продукту 80%+, несмотря на большой объём backend-кода.

Для Ventrix это центральная функция:

> **Найти проблему — только половина ценности.  
> Вторая половина — добиться её исправления и доказать это.**

### В проекте уже заложена правильная будущая архитектура

Хорошая новость: документация показывает, что разработка изначально понимает переходы масштабирования.

В документе уже выделены будущие extraction boundaries:

```text
telegram-connector
analysis-worker
report-worker
notification-worker
```

`docs/product-and-architecture.md:L49-L63`.

Поэтому я бы не начинал с микросервисов.

Лучший следующий этап:

```text
               SQLite / PostgreSQL
                       |
           durable background jobs
             /       |        \
Telegram worker   AI workers   Notification worker
       \             |             /
             Problem lifecycle
                    |
                 FastAPI
                    |
                 Mini App
```

Сначала всё ещё можно оставить в одном репозитории и даже на одном сервере.

### Архив проекта содержит чувствительные runtime-файлы

В переданном ZIP физически присутствуют:

```text
.env
.env.backup-before-test
data/app.db
backups/*.db
.git/
node_modules/
.venv/
.next/
```

Это не просто вопрос размера архива.

`.env` игнорируется Git и, судя по структуре, не является tracked-файлом, но он попал именно в ZIP. `.gitignore` предназначен для исключения таких runtime-данных; README отдельно предупреждает, что backups могут содержать защищённые секреты и должны считаться чувствительными. `.gitignore:L31-L40`, `README.md:L230-L248`.

Кроме того, текущий secret-scan script пропускает некоторые runtime/env области, поэтому он не является гарантией, что export ZIP безопасен для передачи. `scripts/secret_scan.py:L11-L17`, `scripts/secret_scan.py:L31-L32`.

Это надо исправить перед передачей проекта подрядчикам, инвесторам или внешним разработчикам.

Экспорт проекта должен формироваться отдельной командой:

```text
include:
app/
services/
packages/
tests/
docs/
infra/
migrations/
package files
pyproject

exclude:
.env*
data/
backups/
.git/
node_modules/
.venv/
.next/
*.db
logs/
```

Содержимое секретов я в отчёте намеренно не раскрываю.

### Наблюдаемость есть, но не закончена до production-уровня

Есть `/health` и `/health/details`, а health details агрегирует database, scheduler, queue, sessions, AI config и Mini App состояние. `services/backend/api/app.py:L44-L110`.

Есть structured logging и persisted job state. Документация предусматривает correlation/job/tenant/account/stage context и исключение чувствительных полей из логов. `docs/SCHEDULER_ANALYSIS_ARCHITECTURE.md:L69-L76`.

Но product architecture заявляет также полноценные `/health/live`, `/health/ready` и `/metrics`; текущий API в этом отношении ещё не полностью соответствует целевой observability-модели. `docs/product-and-architecture.md:L184-L193`, `services/backend/api/app.py:L44-L110`.

Для production дальше нужны в первую очередь:

```text
queue depth
queue age p50/p95/p99
job duration
job failure/retry rate
Telegram fetch latency
message → signal latency
signal → notification latency
DeepSeek latency
AI invalid-json rate
FloodWait count
SQLite lock/retry count
reports overdue
notification send failure
```

Без этого нельзя уверенно сказать «система выдерживает X сообщений/сек» только по коду.

### Проверка проекта

В рамках анализа frontend lint и TypeScript type-check проходили. Полный Next production build в текущем изолированном окружении не удалось считать достоверной проверкой из-за попытки получить build-компоненты через сеть.

Python test suite в архиве достаточно объёмный и содержит проверки очереди, SQLite claiming, incremental intelligence, critical notifications, reconciliation и scheduler logic, но полноценно выполнить его именно в текущем Linux-окружении не удалось из-за отсутствующих host-зависимостей и несовместимости вложенного `.venv`, который был создан не под это runtime-окружение.

Поэтому корректная формулировка:

> **архитектура покрыта тестами на уровне репозитория, но этот аудит не является подтверждением, что весь suite сейчас зелёный в clean CI.**

Перед production релизом нужен чистый:

```text
docker build
↓
migration test
↓
pytest
↓
eslint
↓
tsc
↓
next build
↓
integration Telegram sandbox
↓
load test
```

## Чеклист реализации и общий прогресс

Ниже — сводный checklist именно относительно задуманной модели Ventrix.

| Функция | Статус | Комментарий |
|---|---|---|
| Multi-tenant backend | ✅ | Tenant-scoped модели и client context присутствуют |
| FastAPI backend | ✅ | Основной API реализован |
| Separate scheduler | ✅ | Отдельный process и persistent schedule |
| Durable SQLite queue | ✅ | Claim/retry/heartbeat/idempotency |
| Job priorities | ✅ | Есть приоритеты и category limits |
| Tenant fairness | ✅ | Учтена при claim |
| Несколько workers архитектурно | 🟡 | Queue готова, default deployment = 1 worker |
| Параллельное выполнение внутри worker | ❌ | Jobs фактически последовательны |
| PostgreSQL production path | ❌ | Пока SQLite |
| Redis/broker | ❌ | Осознанно не MVP |
| Telegram user account auth | ✅ | Phone/code/2FA |
| Шифрование Telegram session | ✅ | Session storage через encryption service |
| Выбор рабочей folder | ✅ | Реализован |
| Несколько folders через UI | 🟡 | Backend-модель допускает больше, UI выбирает одну |
| History depth | ✅ | 3/7/14/30 UI |
| Initial history sync | ✅ | Батчевая загрузка |
| Cursor/idempotent incremental sync | ✅ | Присутствует |
| Настоящие Telegram live events | ❌ | Сейчас polling |
| Long-lived Telethon workers | ❌ | Fetch создаёт подключения |
| Локальные detectors | ✅ | Несколько бизнес-категорий |
| Жалобы | ✅ | Есть detector |
| Повторные обращения / ожидание ответа | ✅ | Есть |
| Коммерческий интерес | ✅ | Есть |
| Договор/КП/счёт/оплата | ✅ | Есть локальные признаки |
| Обещания сотрудников | ✅ | Commitment создаётся |
| Автоматические deadline | 🟡 | Простые случаи да, сложные даты слабые |
| Overdue commitments | ✅ | Reconciliation создаёт проблемы |
| AI triage | ✅ | Context + strict result schema |
| AI soft budget | ✅ | Реализован |
| AI hard budget | ✅ | Реализован |
| JSON validation/repair | ✅ | Есть контролируемая обработка |
| Automatic deep escalation | 🟡/❌ | Schema есть, полный workflow не доведён |
| Signal model | ✅ | Реализована |
| Commitment model | ✅ | Реализована |
| OperationalProblem | ✅ | Реализована |
| Единый Event→Problem pipeline | 🟡 | Initial/deep/incremental расходятся |
| Deduplication | ✅/🟡 | Механизмы есть, но пути различаются |
| Evidence | ✅/🟡 | Источник/evidence сохраняются, lifecycle не полный |
| Assignment problem → employee | 🟡 | Employee mapping есть, полноценный workflow нет |
| Полный Problem FSM | ❌/🟡 | Domain core есть, live persistence/API не подключены полностью |
| Audit переходов проблемы | ❌/🟡 | Не соответствует полной целевой модели |
| Semantic remediation verifier | ❌ | Сейчас упрощённые эвристики |
| Auto-resolve | ✅/🟡 | Есть, но критерий слишком простой |
| Уведомление владельцу | ✅ | Notification destination есть |
| Уведомление сотруднику | ✅/🟡 | Backend есть при корректной employee mapping |
| Уведомление в группу | ✅/🟡 | Destination есть |
| Per-employee thresholds | ✅/🟡 | Данные есть, policy пересекается с immediate threshold |
| Quiet hours | ✅ | Предусмотрены |
| Notification cooldown | ✅ | Есть |
| Severity-aware cooldown | ❌ | Текущая модель слишком общая |
| Durable notifications | ✅ | Notification jobs/logs |
| Retry отправки | ✅ | Есть |
| Добавить tenant bot в группу | ✅/🟡 | Integration/verification есть |
| Анализ только потому, что bot добавлен в группу | ❌ | Основной ingestion идёт через Telethon account |
| Регулярный hourly reconciliation | ✅ | Реализован |
| Daily/scheduled analysis | ✅ | Реализован |
| Timezone schedule | ✅ | Реализован |
| Enabled weekdays | ✅ | Реализован |
| AI batch analysis | ✅ | Реализован |
| Tenant reports | ✅ | Backend есть |
| Employee report | ✅ | Backend aggregation есть |
| Client report | ✅ | Backend aggregation есть |
| Company report | ✅ | Backend aggregation есть |
| Report Mini App list | ✅ | Есть |
| Report detailed UI | ❌/🟡 | Backend богаче интерфейса |
| Proactive «отчёт готов» delivery | 🟡 | Durable availability есть, полный transport ограничен |
| Multi-account single consolidated report | 🟡 | Нужна переработка aggregation barrier |
| Admin Mini App auth | ✅ | Telegram initData validation |
| Tenant isolation в auth | ✅ | Tenant выводится server-side |
| RBAC модели | ✅ | Membership/permission entities есть |
| Owner membership | ✅ | Создаётся |
| Employee creation API | ✅ | Есть |
| Employee creation UI | ❌ | Нет |
| Employee → Membership automatic link | ❌ | Существенный gap |
| Employee Mini App access | 🟡 | Auth готов к membership, provisioning не доведён |
| Role-specific employee Mini App | ❌/🟡 | Backend foundation есть, UX отсутствует |
| Dashboard | ✅ | Есть |
| Problems list | ✅ | Есть |
| Problem detail workflow | 🟡/❌ | Backend лучше frontend |
| Statistics | ✅/🟡 | Базовые показатели |
| Employees screen | ✅ | Read-only |
| Connections screen | ✅ | Самый законченный management screen |
| Groups screen | ✅ | Read-only |
| Reports screen | ✅ | Read-heavy |
| Settings screen | ❌/🟡 | Визуально есть, API не подключён |
| Settings backend | ✅ | GET/PATCH реализованы |
| Commitments UI | ❌ | Нет отдельного раздела |
| Dialogues UI | ❌ | Нет |
| Clients UI | ❌ | Нет |
| Meetings UI | ❌ | Нет |
| Pipeline UI | ❌ | Нет |
| Finance UI | ❌ | Нет |
| Attention center | ❌/🟡 | Частично через dashboard/problems |
| System problems UI | ❌ | Нет полноценной поверхности |
| Security UI | ❌ | Нет |
| Front/backend deployment separation | ✅ | REST boundary |
| Frontend полностью независим от Telegram | ❌ | Auth намеренно TMA-dependent |
| Health endpoint | ✅ | Есть |
| Detailed health | ✅ | Есть |
| Production metrics endpoint | 🟡/❌ | Целевая модель шире текущей |
| Structured logging | ✅ | Есть инфраструктура |
| Реальные load benchmarks | ❌ | В архиве нет достаточной production telemetry |
| Horizontal multi-host scaling | ❌ | Текущая модель single-host |
| Safe distributable source archive | ❌ | ZIP содержит runtime secrets/data |

Источники для сводки: `docker-compose.yml:L1-L64`, `services/backend/jobs/queue.py:L81-L369`, `services/backend/jobs/worker.py:L53-L242`, `services/backend/scheduler/service.py:L142-L256`, `services/backend/telegram_sessions/incremental.py:L41-L227`, `services/backend/intelligence/signals.py:L68-L222`, `services/backend/intelligence/notifications.py:L38-L324`, `services/backend/api/client_router.py:L239-L291`, `services/backend/api/client_router.py:L1033-L1078`, `services/backend/api/client_router.py:L1231-L1316`, `app/mini-app/api/client.ts:L41-L118`, `app/mini-app/features/sections/section-views.tsx:L10-L34`.

### Как я бы описал состояние проекта одним предложением

**Сейчас Ventrix — это уже рабочее backend-ядро системы операционного контроля Telegram, но ещё не полностью собранный продукт: обнаружение и аналитика продвинулись дальше всего, а lifecycle проблемы, employee access, Mini App management и production-scale processing должны быть следующим основным этапом.**

### Где именно находится проект на пути к production

Упрощённо:

```text
Идея
  ↓
UI prototype
  ↓
Backend prototype
  ↓
Durable MVP
  ↓
[ ВЫ СЕЙЧАС ПРИМЕРНО ЗДЕСЬ ]
  ↓
Closed-loop product
  ↓
Production hardening
  ↓
Scale
```

Я бы разделил дальнейшую готовность на три ступени.

**Чтобы проводить ограниченный пилот с несколькими реальными компаниями**, фундамент уже близок. Перед этим критично исправить secrets/export, deadline correctness, notification reliability и ключевые lifecycle ошибки.

**Чтобы продавать как устойчивый B2B SaaS**, необходимо завершить Employee→Membership→RBAC, Problem FSM, assignment, verification, настройки Mini App, единый analysis lifecycle и разнести разные виды jobs хотя бы на несколько worker pools.

**Чтобы масштабировать на большой поток компаний и сообщений**, потребуется long-lived Telegram ingestion, PostgreSQL, отдельная очередь/broker, несколько классов workers, O(1) Mini App auth sessions, metrics и нагрузочные SLO.

Приоритет разработки я бы ставил не на новые AI-фичи, а так:

```text
Problem lifecycle + verification
            ↓
employee/RBAC end-to-end
            ↓
Mini App control surfaces
            ↓
worker parallelism / priority isolation
            ↓
Telegram live/reused sessions
            ↓
unified analysis pipeline
            ↓
production observability
            ↓
PostgreSQL / broker when load proves need
```

Именно это даст самый большой прирост не в количестве кода, а в **проценте реально законченного продукта**. Исходная собственная спецификация проекта подтверждает такой порядок: сначала secure Telegram и detectors, затем event correlation/problem FSM/evidence/assignment/remediation, после — tenant bot/Mini App, reports/analytics и финальный security/load/pilot этап. `docs/product-and-architecture.md:L214-L223`.

## Ограничения оценки

Это **аудит исходного кода и архитектуры архива**, а не production APM-аудит. В архиве нет репрезентативного набора реальной telemetry, по которому можно было бы честно определить фактические `messages/sec`, p95 AI latency, p99 notification latency, queue backlog во время пика или предел tenant concurrency. Поэтому выводы о бутылочных горлышках основаны на модели выполнения кода, а не на синтетически придуманных цифрах.

Именно поэтому оценка **≈64% готовности** означает «доля законченных продуктовых сценариев относительно собственной заявленной архитектуры», а не «64% до возможности запуска». Ограниченный пилот возможен существенно раньше 100%; наоборот, переход от 80% функциональности к надёжному production SaaS обычно требует непропорционально много работы именно в reliability, lifecycle, observability и scale.

Самая позитивная часть аудита в том, что проект уже имеет правильные базовые строительные блоки — durable queue, tenant isolation, Telegram sessions, deterministic signals, AI triage, commitments, reconciliation, notifications и reports. Самая важная незавершённая часть — **соединить эти блоки в один контролируемый цикл «увидел → назначил → уведомил → исправили → проверил → закрыл → отчитался»**, потому что именно этот цикл, согласно собственной продуктовой спецификации Ventrix, и является главным продуктом. `docs/product-and-architecture.md:L3-L21`.