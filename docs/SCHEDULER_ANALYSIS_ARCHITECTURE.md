# Tenant scheduler, Telegram ingestion and analysis pipeline

## Runtime topology

The SQLite MVP uses five long-running roles on one host and one local volume:

1. `backend` — FastAPI and tenant-scoped Mini App API.
2. `owner-bot` — platform owner inline interface.
3. `client-bots` — one runtime manager for all client bots, not one OS process per tenant.
4. `scheduler` — the single source of scheduled analysis jobs.
5. `background-worker` — shared durable jobs; more workers may be used conservatively on the same host.

The scheduler persists `next_analysis_at` and never relies on an in-memory timer. It calculates the report time in the tenant timezone, subtracts the configured 5/10/15/30-minute lead, verifies access, and enqueues an idempotent `analysis.pipeline` job. On restart it resumes from SQLite.

## Fair queue contract

`background_jobs` stores the tenant/account, correlation and idempotency identifiers, payload, progress, result, lifecycle timestamps, heartbeat and retry state. Claim is an atomic conditional update. Eligible jobs are ordered by priority and then by the tenant's persisted `last_claimed_at`. A running heavy job excludes another heavy job for the same tenant. Global category limits cap AI, Telegram, sync and report work.

Supported public job types:

- incremental path: `telegram.fetch_updates`, `telegram.history_sync`, `signal.local_scan`, `signal.ai_triage`;
- reconciliation: `commitment.reconcile`, `problem.evaluate`, `analysis.hourly`;
- notifications: `notification.employee`, `notification.manager`, `notification.group`;
- deep/report path: `analysis.deep`, `report.employee`, `report.client`, `report.company`;
- maintenance: `maintenance.session_health`;

- `telegram_initial_sync`, `telegram_incremental_sync`, `dialog_classification`;
- `message_preprocessing`, `ai_batch_analysis`, `problem_deduplication`;
- `report_generation`, `report_delivery`, `statistics_refresh`;
- `session_health_check`, `cleanup`, `retry_failed_job`;
- orchestration job `analysis.pipeline` and internal `telegram.sync_chat`.

Retries use exponential backoff, stale leases are recovered from `locked_at`, and cancellation is tenant-scoped. SQLite remains a single-host design; it is not a distributed queue.

## Telegram login and ingestion

The client-bot flow is inline-first:

`consent → phone → code → optional 2FA → encrypted StringSession → get_me → folders → personal-dialog consent → history window → resumable sync`.

Phone/code/2FA messages are deleted when Telegram permits. Codes and 2FA are never persisted. The phone is masked; the Telethon session is stored only through the encrypted-secret service. Folder selection supports multiple folder IDs at the service/model level. Personal dialogs require explicit consent and low-confidence classifications remain unselected until confirmation.

Initial ingestion stores small batches, per-dialog cursors and progress. FloodWait becomes a scheduled retry. Attachments retain metadata only. A failed dialog does not fail the entire run.

## Analysis and reporting

Local preprocessing removes system events and duplicates, normalizes text, preserves message IDs/timestamps, and extracts authors, response delay, dates, amounts, links, call mentions, repeated messages and attachment counts. Batches never mix unrelated dialogs and include the tenant business profile and local signals.

The router selects configured DeepSeek fast/deep models. Every provider response must validate against schema `1.0`. The pipeline performs one controlled JSON repair/retry; an invalid batch cannot create problems. Raw provider output is encrypted. Problem fingerprints are unique. Input/output tokens and request counts are aggregated per tenant/model/day.

Reports are produced only after every required batch completes. `reports`, `report_sections`, `report_metrics`, `report_problems` and `report_generation_runs` store structured output. A late report keeps `forming` state until completion, then records `completed_after_due_time`. Delivery makes the durable report available to the client bot and Mini App; proactive Telegram push is a separate transport concern.

## Mini App API

All routes derive tenant from verified Telegram `initData`, the matched client bot and active membership. A tenant ID supplied in JSON is never trusted.

- `GET /api/v1/client/bootstrap`, `/dashboard`, `/menu`, `/access`, `/settings`;
- `GET /api/v1/client/problems`, `/problems/{id}` and `PATCH /problems/{id}`;
- `GET /api/v1/client/reports`, `/reports/{id}`;
- `GET /api/v1/client/connections`;
- `GET /api/v1/client/sync/current`, `/sync/{job_id}`;
- `POST /api/v1/client/sync/start`, `/sync/cancel`;
- `PATCH /api/v1/client/settings`.

Owner membership grants all tenant permissions; other memberships use explicit permission rows. Repository methods keep an immutable tenant scope.

## Health and secrets

`/health/details` exposes only aggregate state for database, scheduler, queue, workers/bots, Telethon sessions, AI configuration and Mini App API. Structured logs allow correlation/job/tenant/bot/account/stage/duration/retry fields and reject arbitrary context. Tokens, API keys, sessions, login codes, 2FA, full phone numbers and private message bodies are not logging fields.

## Known MVP boundaries

- All SQLite writers and all bot runtimes must share one host and local filesystem.
- A report becomes available durably; proactive client notification is not yet a guaranteed delivery channel.
- Employee/department/permission tables are ready, while management UI and department-level policies are intentionally deferred.
- Local batching is per dialog and character-bounded; semantic multi-window splitting can be refined with real-volume telemetry.
- Provider model availability and real Telegram authorization require live credentials and cannot be proven by offline tests.
