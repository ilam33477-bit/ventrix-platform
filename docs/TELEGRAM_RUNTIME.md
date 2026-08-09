# Telegram runtime

## Ownership

One permanent account equals one `TelegramSessionActor` and one Telethon client. Before
decrypting a StringSession, the runtime must acquire `TelegramRuntimeLease`. The actor
heartbeats the lease independently of Telegram RPC work. Takeover increments `generation`;
all actor-owned database writes verify the current generation.

## Commands and ordering

Commands are durable `BackgroundJob` records with `category=telegram_rpc` and the target
account ID. Each actor has a serial command worker. Dialog ingestion jobs additionally carry
`partition_key` and `partition_sequence`, preventing concurrent or reversed processing for
the same dialog while allowing different accounts/dialogs to progress independently.

Actor-owned operations currently include catch-up, health, catalog refresh, historical chat
sync, source preview and source confirmation. Ordinary background workers do not register
the permanent history/incremental Telethon handlers.

## Recovery and errors

- `NewMessage` and `MessageEdited` are the fast path.
- Catch-up from durable cursors runs after connect/reconnect and from scheduler recovery.
- `FloodWait` records `rate_limited_until` and retries with Telegram's delay.
- revoked/expired/deactivated auth moves the connection to `reauthorization_required`.
- transient network errors use bounded exponential backoff with jitter.
- update counters are batched to reduce SQLite writes.

## Known live-test requirement

Automated tests verify lease exclusion, takeover fencing, queue partition ordering and
storage idempotency. Real Telegram FloodWait, DC migration, revoked-session recovery and
multi-hour reconnect behavior require a controlled live account test and are not claimed
as verified by the local suite.
