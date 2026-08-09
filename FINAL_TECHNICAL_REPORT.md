# Ventrix final technical hardening report

Date: 2026-08-09

## VERIFIED

- SQLite durable queue: idempotency, retry, heartbeat, stale recovery, tenant fairness,
  bounded category pools and dialog partition ordering.
- Database-backed Telegram runtime lease with takeover generation and stale-owner fencing.
- Live/catch-up ingestion converges on the common Signal/Commitment/Problem lifecycle.
- Historical Telegram sync and catalog refresh are dispatched to the account actor in production.
- Durable per-dialog SLA timers and per-commitment deadline jobs, with periodic reconciliation fallback.
- Persistent problem FSM, transition audit/evidence, assignment, resolution and reopen APIs.
- Tenant/RBAC checks in Mini App APIs and employee membership flow.
- Strict owner AI draft schema, pre-provider secret rejection, version/diff/latency audit fields.
- Migration chain upgrades, downgrades and upgrades again through revision `20260809_0014`.
- Source export/ignore/secret-scan policy excludes runtime secrets and generated data.

Verification is based on the commands recorded below; “verified” does not imply a real
Telegram or DeepSeek production call unless explicitly stated.

## IMPLEMENTED BUT REQUIRES LIVE TEST

- Real Telethon push delivery, edit delivery, reconnect, FloodWait and revoked-session behavior.
- Actor takeover between two simultaneously running runtime containers under real network delay.
- End-to-end Telegram account login, catalog, initial history, Mini App and notifications.
- DeepSeek draft/triage timeout and malformed-response behavior against the production provider.
- Production load envelope for 10–50 companies and multi-hour SQLite contention.

## DEFERRED

- PostgreSQL/Redis migration and distributed workers; unnecessary before measured pilot pressure.
- Telegram webhook/live update replacement; Telethon push plus cursor reconciliation is retained.
- Destructive retention of private message history. Policy must be approved before enabling deletion.
- Large-scale benchmark and chaos campaign with live Telegram accounts.

## Main changes

Revision `20260809_0014` adds Telegram runtime leases, connection runtime telemetry, dialog
response timers, problem next-check time, queue partition ordering and owner-draft audit fields.
The runtime owns permanent session RPC; general workers no longer register permanent history
or incremental Telegram clients. SLA and commitment checks are scheduled durably at the event,
while reconciliation provides recovery. AI draft input is schema-confined and secret-screened.

## Automated verification

- Full backend suite: **93 passed** (`pytest -q`).
- Python lint: **passed** (`ruff check services tests/backend alembic scripts`).
- Migration upgrade/downgrade/re-upgrade through 0014: **passed**.
- Frontend ESLint: **passed**.
- Frontend TypeScript (`tsc --noEmit`): **passed**.
- Next.js production build: **passed**.
- All six Python Docker images, including `telegram-session-runtime`: **built successfully**.
- Container import smoke for API and Telegram runtime: **passed**.
- Python mypy baseline: **not passed (471 errors in 26 files)**. Most findings are existing
  SQLAlchemy optional/result typing and missing Telethon stubs; this remains explicit debt and
  is not represented as a runtime-test failure.

## Readiness estimate

Engineering readiness for a controlled pilot: **84%**. Core persistence, isolation and
recovery paths are implemented. The remaining uncertainty is predominantly live-provider
and sustained-load evidence plus the unresolved Python static-typing baseline.
