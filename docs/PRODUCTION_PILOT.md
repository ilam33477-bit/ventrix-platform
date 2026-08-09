# Production pilot runbook

## Before pilot

1. Back up the SQLite database and verify restore into a separate file.
2. Apply Alembic migrations through `20260809_0014`.
3. Run secret scan, backend tests, frontend lint/typecheck/build and Docker build.
4. Confirm only the Ventrix compose project is restarted.
5. Confirm `/health/live`, `/health/ready`, `/health/details` and metrics.

## Pilot checks

- Connect one non-critical work account and confirm exactly one runtime lease/client.
- Send and edit messages in a personal dialog and an opted-in group.
- Restart Telegram runtime during traffic; verify catch-up has no gaps or duplicates.
- Leave a customer message unanswered past tenant SLA, then reply and verify the timer/problem flow.
- Create a dated commitment, verify deadline job, notification and remediation lifecycle.
- Confirm manager/employee tenant isolation in Mini App.
- Generate a consolidated report for a tenant with two accounts.

## Operational limits

Start with a small cohort and bounded worker concurrency. Monitor queue age, SQLite lock
retries, FloodWait, notification failures, AI latency/invalid JSON and overdue reports.
Do not add arbitrary workers on SQLite. Increase concurrency only from measured queue and
lock data.

## Rollback

Stop only the Ventrix compose project, preserve its data directory, restore the verified
backup if a data migration must be reverted, and deploy the previous image. Never delete
the active database or Telegram sessions as part of rollback.
