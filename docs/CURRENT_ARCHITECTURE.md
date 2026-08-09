# Ventrix current architecture

## Runtime boundaries

Ventrix remains a single-host modular monolith backed by SQLite/WAL. The API, owner bot,
client-bot runtime, scheduler, Telegram session runtime and background-worker pools are
separate processes over one durable database.

Each permanent `TelegramConnection` is owned by exactly one `TelegramSessionActor`.
Ownership is protected by `telegram_runtime_leases` with an instance identity, expiring
lease and monotonically increasing fencing generation. A stale actor cannot update runtime
health or counters after another instance takes over.

Telegram RPC jobs use category `telegram_rpc` and an explicit `telegram_account_id`.
The actor for that account is the only worker allowed to claim them. Live events are
persisted through the durable queue and ordered by dialog partition. General, realtime,
notification, AI, report and reconciliation work remains in independent bounded pools.

## Business flow

`Telegram event -> telegram.ingest_event -> signal.scan_batch -> local/AI triage ->
Signal -> Commitment/OperationalProblem -> notification -> remediation verification ->
problem lifecycle -> report`.

Dialog response state and commitment deadlines create durable scheduled jobs. Periodic
reconciliation remains a recovery path, so a process restart does not lose SLA/deadline
checks.

## Storage and trust boundaries

- Tenant authorization and filtering are enforced by backend queries, never query params.
- Telegram StringSession and bot tokens are encrypted at rest and never returned to Mini App.
- Login clients are ephemeral; after login, permanent session access belongs to the actor.
- Owner AI drafts reject likely secrets before provider calls and validate an extra-forbidden schema.
- Runtime state is SQLite-persistent; container restart policy and bind-mounted data preserve it.

## Scaling boundary

SQLite is appropriate for the present single-node pilot when concurrency stays bounded.
The queue categories, account actors and application services are explicit extraction
boundaries for a later PostgreSQL/Redis deployment; no such migration is required for the pilot.
