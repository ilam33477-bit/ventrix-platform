# Ventrix next-stage implementation report

Date: 2026-08-09
Scope: current repository, single-host SQLite MVP, no production deployment in this change set.

## 1. Before the changes

Telegram user sessions were opened for polling work and closed again; `receive_updates=False` meant the 30-second incremental scan was the effective realtime path. A working folder was mandatory, initial history created a job per message, personal dialogs required extra consent, and groups were not represented as explicit monitored sources. The queue was durable, but execution categories were not fully isolated. Owner client creation was a seven-field FSM. Mini App onboarding was a short linear flow. Scheduled reports could rebuild AI context for unchanged dialogs.

The earlier deep-research implementation already provided useful foundations that were retained: durable SQLite jobs with leases/idempotency/recovery, tenant-scoped auth, Signal/Commitment/OperationalProblem models, persistent problem FSM, remediation verification, notification policies, multi-account report aggregation, encrypted secrets and a typed Mini App API boundary.

## 2. What changed

- Added a dedicated `telegram-session-runtime` process with one long-lived Telethon client per ready account.
- Added push handlers for `NewMessage` and `MessageEdited`; handlers only normalize metadata and enqueue durable work.
- Added reconnect supervision, runtime heartbeat/counters and cursor-based catch-up through the same ingestion path.
- Added a reserved realtime queue lane and batch signal scan jobs.
- Changed source policy to all personal dialogs by default; groups/channels are explicit opt-in sources.
- Added group and shared-folder link preview/selection/confirmation jobs.
- Added sender roles and canonical peer IDs for cross-connection group correlation.
- Added attachment-aware invoice detection and configurable metadata-aware fast-lane rules.
- Replaced mandatory folder onboarding with a resumable ten-step business onboarding; optional employee/group steps persist `skipped`.
- Added DeepSeek-backed owner client drafts, versioned correction, field editing, preview and final confirmation.
- Added meaningful-version report filtering so scheduled analysis skips unchanged dialogs.
- Extended runtime metrics and added tests for event ingestion, source opt-in, invoice metadata and AI drafts.

## 3. Architecture decisions

The implementation extends the existing modular monolith rather than introducing a second queue or database. Telethon RPC ownership is isolated in one runtime process, while normalized events cross into the existing durable SQLite queue. Heavy network/AI work never runs in a Telethon callback. The runtime is single-host and assumes one actor per connection; multi-host leader election is deliberately deferred.

SQLite concurrency stays bounded: one Telegram RPC command loop, two lightweight realtime workers by default, one heavy worker, and small independent AI/notification/general pools. Realtime, critical and notification categories bypass the normal per-tenant active-job cap, while heavy work retains tenant limits.

## 4. Main files changed

- Runtime and ingestion: `services/backend/telegram_sessions/runtime.py`, `event_ingestion.py`, `gateway.py`, `service.py`, `sync.py`.
- Queue and execution: `services/backend/jobs/queue.py`, `worker.py`, `scheduler/service.py`, `config.py`.
- Intelligence: `intelligence/local_signals.py`, `signals.py`, `ai_triage.py`, `notifications.py`, `reconciliation.py`.
- Owner drafts: `services/backend/services/client_drafts.py`, `bot/handlers.py`, `bot/keyboards.py`, `bot/states.py`, `services/foundation.py`.
- API/data: `models.py`, `api/client_router.py`, `metrics.py`, `api/app.py`.
- Mini App: `app/mini-app/api/client.ts`, `types.ts`, connection manager, onboarding flow and session hook.
- Deployment/docs/tests: both Compose files, README, architecture document and backend tests.

## 5. Migrations

- `20260808_0010_problem_lifecycle`: problem transitions, verification and lifecycle evidence.
- `20260808_0011_notification_policy`: destination-specific thresholds/policy.
- `20260808_0012_critical_fast_lane`: tenant-configurable deterministic urgent rules.
- `20260809_0013_event_runtime_sources`: runtime health/counters, canonical peers, sender roles, ingestion source, monitored sources, onboarding/report JSON, meaningful report versions and owner client drafts. It also maps legacy onboarding steps.

A clean SQLite database upgraded from the first revision to `20260809_0013 (head)` successfully.

## 6. Telegram lifecycle

```text
ready TelegramConnection
  → one TelegramSessionActor / long-lived Telethon client
  → NewMessage | MessageEdited
  → lightweight normalized telegram.ingest_event job
  → source-policy check + idempotent TelegramMessage/cursor write
  → signal.scan_batch
  → local relevance/invoice/commitment rules
  → optional AI triage
  → Signal / Commitment / OperationalProblem
  → notification / remediation / report lifecycle

restart or disconnect
  → reconnect
  → telegram.catch_up from saved cursors
  → the same telegram.ingest_event path
```

Event idempotency uses connection, canonical peer, remote message ID, event kind and edit version. Edited messages supersede prior source signals and are rescanned. The callback does not download files, call AI or write business entities directly.

## 7. Queue/lane model

- `telegram_rpc`: serialized session RPC commands owned by the session runtime.
- `realtime` / `critical`: reserved lightweight capacity for ingestion and fast response.
- `notification`: isolated delivery pool.
- `historical` / `telegram` / `sync`: bounded history processing.
- `ai` / `ai_fast`: bounded triage.
- `ai_heavy` / `analysis` / `report`: one small heavy pool and per-tenant heavy limit.
- `general` / `reconciliation`: maintenance and deterministic lifecycle work.

Initial sync stores messages in batches and creates `signal.scan_batch` jobs for chunks of 100 IDs instead of a job per message.

## 8. Source policy

All personal chats are selected automatically. Relevance is evaluated per message; a personal-looking chat is not silently removed from the source set. Groups and channels require an enabled `MonitoredSource`. Legacy folder endpoints remain for compatibility but are no longer required by the normal login flow or exposed as the primary Mini App model.

## 9. Invoice pipeline

The local engine evaluates attachment filename, MIME type, document extension, payment language and explicit monetary amount. A random PDF does not become `invoice_received`; an invoice-like PDF does. Tenant fast-lane rules can further constrain attachment names, MIME types, extensions, direction, source type, sender role and amount requirement. Alerts remain provisional until AI/context confirmation when configured.

## 10. Group monitoring

The API exposes async preview and confirm operations. Telegram invite links and `addlist` folder links are resolved inside the owning session actor. Folder peers are shown for selection. Joining occurs only when the confirmation request explicitly sets it. Confirmed peers become `MonitoredSource` and `TelegramDialog` rows, followed by history catch-up.

Canonical Telethon peer IDs are used in live events, catalog rows and previews. Group Signal fingerprints use tenant + canonical peer + remote message + type, preventing the same group message observed through two company accounts from creating two logical signals. Sender roles distinguish account owner, mapped employee and external participant so an employee message is not interpreted as a customer message.

## 11. Mini App onboarding

Backend state now covers: welcome, mini guide, Telegram connection, monitoring started, employees, notifications, reports, groups, final review and completion. Each advance persists tenant-level status; optional steps may be `skipped`. Closing and reopening resumes the current step, while completed tenants open the dashboard immediately.

The UI explains business value, enables all personal dialogs after login, avoids legacy folder terminology, shows security wording, and keeps employees/groups optional. The Sources section supports group/folder preview and confirmation.

## 12. Owner-bot AI client creation

“Create client” now offers AI description mode or the existing manual mode. One free-form description is sent through the existing DeepSeek provider into a validated draft. The bot shows a preview, supports common-field editing and free-form correction, and versions every correction. AI output never creates a tenant. Only the final inline confirmation calls Foundation creation.

Tenant creation and marking the draft confirmed now share one database transaction (`FoundationService.create_tenant(commit=False)`), closing the crash window that could otherwise duplicate a tenant. A repeated confirmation reads `created_tenant_id` instead of creating again.

## 13. DeepSeek draft schema

`ClientDraftData` validates company/owner identity, Telegram user ID, niche, description, products, audience, working hours, IANA timezone, SLA, critical criteria, report time, plan and AI instructions. Raw owner input is encrypted. Tokens/passwords are explicitly excluded by the system prompt and are not draft fields. Invalid JSON or invalid fields fail closed and leave the user in the correction flow.

## 14. Report optimisation

Every meaningful dialog update increments `DialogState.meaningful_version`. Scheduled batch building skips a dialog when its meaningful version is not newer than `last_report_version`. The state version used for an AI batch is stored in the batch payload and is acknowledged only after the consolidated report succeeds, avoiding advancement on a failed report. Multi-account aggregation still produces one tenant report.

## 15. Metrics

`/metrics` includes queue depth/category/oldest age, job latency/retry/failure, message→signal and signal→notification latency, AI latency/errors/invalid JSON and job-type totals, notification failures, overdue reports, SQLite lock failures, FloodWait failures, Telegram runtime status, heartbeat lag, received updates, duplicate events and catch-up events.

## 16. Security changes

- Tenant scope is checked before source operations and job result access.
- Preview confirmation may select only peers returned by the completed tenant-owned preview job.
- Telegram codes and 2FA remain ephemeral; sessions and owner draft prompts are encrypted.
- Runtime/logging paths do not include message text, tokens or session strings in structured operational logs.
- Source export and secret scan exclude `.env*`, DB/data/backups, sessions, `.git`, virtualenv, `.next`, node modules and logs.

## 17. Verification performed

- `pytest -q`: **89 passed**.
- Ruff over services/tests/migrations: **passed**.
- TypeScript `tsc --noEmit`: **passed**.
- ESLint: **passed**.
- `npm run build`: **passed**, Next.js production build generated successfully.
- Clean Alembic upgrade/current: **passed**, head `20260809_0013`.
- `docker compose config --quiet`: **passed**.
- `docker compose build`: **passed** for backend, background-worker, Telegram session runtime, client-bots, owner-bot and scheduler.
- One-shot container import smoke for runtime, worker and owner handlers: **passed** (`container-imports-ok`).
- Secret scan/source export tests: included in the 89 passing tests.

No live Telegram account or real DeepSeek request was executed during automated verification; network-dependent preview/join and AI quality must be exercised in a controlled pilot account before production rollout.

## 18. Load/simulation results

Automated SQLite simulations passed: 20 simultaneous enqueues, 24 jobs drained exactly once by four workers, stale-lease recovery, retry completion, category-specific claiming, tenant fairness and one-heavy-job-per-tenant enforcement. Multi-account analysis aggregation and cursor restart/idempotency tests passed. This is a correctness/concurrency simulation, not a production capacity benchmark; no messages-per-second claim is made.

## 19. Remaining work and known limitations

- Add a live Telegram integration test account for FloodWait, reconnect, invite-link and shared-folder behavior across Telegram entity variants.
- Add multi-process leader/lease ownership before running more than one `telegram-session-runtime` replica.
- Add sustained pilot telemetry and a reproducible throughput benchmark with representative message distributions.
- Report configuration currently reuses existing time/days/content APIs and defaults; a richer weekly/monthly schedule editor can be added without changing the report pipeline.
- The Mini App onboarding configuration screens intentionally use safe defaults; employee/group creation and advanced policy editing remain in their dedicated control-center sections.
- SQLite remains single-host. Move to PostgreSQL/Redis only after measured lock latency or multi-host requirements justify it.

## 20. Recommended next stage

Run a controlled pilot with 2–3 test tenants and real accounts, capture runtime heartbeat/update/duplicate/catch-up metrics, validate invoice/group false positives, and calibrate tenant rules. Add a session-actor lease before horizontal runtime replication. Then add a reproducible 1k/10k event benchmark and operational alerts for heartbeat lag, oldest realtime job, FloodWait and notification failure. Keep API/domain boundaries unchanged so PostgreSQL or an external queue can be introduced later from measured evidence rather than pre-emptively.
