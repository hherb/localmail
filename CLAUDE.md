# CLAUDE.md

Guidance for Claude Code sessions working in this repo. See [README.md](README.md)
for the end-user view.

## What this is

`localmail` mirrors one or more IMAP accounts (password or Gmail OAuth2) into a
PostgreSQL database. **Read-only with respect to upstream**: localmail never
deletes, modifies, or sends mail. Downstream agents consume the DB and the
attachment tree without touching IMAP.

## Stack (locked in)

- Python ≥ 3.12, managed by `uv` (`uv sync`, `uv run …`).
- Postgres access: `psycopg` v3 + raw SQL + numbered `.sql` files in
  `migrations/`. **No ORM.** Migrations are tracked in `schema_migrations`.
- IMAP: `imapclient` (sync, blocking). Gmail OAuth2 uses XOAUTH2 via
  `google-auth` + `google-auth-oauthlib`.
- Secrets: `keyring` (service `"localmail"`, username = `<account.name>` for
  passwords, `<account.name>:refresh` for OAuth refresh tokens). Cross-platform:
  macOS Keychain on darwin, Secret Service on Linux.
- Config: TOML, validated by `pydantic` v2.
- CLI: `click`.
- Tests: `pytest` (in-memory `keyring` backend; real Postgres at
  `LOCALMAIL_TEST_DSN`, defaults to a separate `localmail_test` database so
  tests can't clobber the live archive).

## Commands

```bash
uv sync                          # install deps
uv run pytest                    # full test suite (skips DB tests if PG unreachable)
uv run localmail init-db         # apply pending migrations
uv run localmail list-accounts   # show config'd accounts and whether a secret is stored
uv run localmail add-account N   # store password for account N (must exist in config.toml)
uv run localmail remove-account N  # drop stored secrets for account N
uv run localmail oauth-login N   # Gmail desktop OAuth flow → refresh token in keyring
uv run localmail sync [--account N] [--limit-per-folder K]   # one-shot incremental sync
uv run localmail run             # foreground daemon (IDLE on INBOX + periodic poll)
uv run localmail list-failed [--account N] [--limit K]   # show messages sync skipped
uv run localmail retry-failed [--account N]    # re-attempt every failed message
```

Common gotcha when running ad-hoc commands: shells often have `VIRTUAL_ENV`
set to some other pyenv venv, which makes `uv run` warn and (with `--active`)
pick the wrong interpreter. Prefix with `unset VIRTUAL_ENV && …` to be safe.

## Layout

```
src/localmail/
  cli.py            # click entry point
  config.py         # pydantic models + TOML loader
  db.py             # connection pool + migration runner
  secrets.py        # keyring wrapper
  oauth_gmail.py    # OAuth2 desktop flow + token refresh
  imap_client.py    # open_connection() context manager (password / XOAUTH2)
  parser.py         # bytes -> ParsedMessage (pure; no IO; NUL-strip + empty->None)
  attachments.py    # write_attachments(conn, parsed, root) -> JSONB rows (content-addressable)
  sync.py           # upsert_*, process_one_message, sync_mailbox, sync_account,
                    #   record_failed_message, retry_failed_messages, folders_to_sync
  worker.py         # WorkerContext shared by daemon threads
  idle.py           # run_inbox_idle_loop, _one_inbox_session, _idle_step
  poller.py         # run_poll_loop, _one_poll_pass
  daemon.py         # Daemon class: signal handling, per-account thread spawn
migrations/         # 0001_init.sql, 0002_attachment_blobs.sql, 0003_failed_messages.sql
tests/
  conftest.py       # memory_keyring fixture, db_dsn/db_conn fixtures
  _eml.py           # MIME fixture builders (no .eml files on disk)
  _fake_imap.py     # in-memory IMAP fake with IDLE support
  test_*.py
config.example.toml
```

User-facing config lives at `~/.config/localmail/config.toml` (override with
`$LOCALMAIL_CONFIG` or `localmail --config PATH …`).

## Schema essentials

Tables: `accounts`, `mailboxes`, `messages`, `message_labels`,
`attachment_blobs`, `failed_messages`, `schema_migrations`. Dedup model:

- **Messages — per-account, by `Message-Id`**: same Message-Id in INBOX + 3
  Gmail labels produces one `messages` row + four `message_labels` rows. The
  same Message-Id on a different account is a separate `messages` row
  (provenance preserved).
- **Messages — fallback when no Message-Id**: dedup by SHA-256 of the raw
  RFC822 bytes (`messages.raw_sha256`, partial unique index when
  `message_id IS NULL`).
- **Attachments — content-addressable, global**: identical bytes appear on
  disk and in `attachment_blobs` exactly once across the whole archive
  regardless of account/message. `messages.attachments` JSONB stores
  `[{"filename": "<original-name-from-this-email>", "sha256": "<hex>"}, …]` —
  the original filename is preserved per-message so files can be restored with
  the names they had when received; the bytes, mime type, size, and on-disk
  path live on the `attachment_blobs` row.

On-disk path: `<attachments.root>/blobs/<aa>/<bb>/<full-sha256-hex>` (two-level
hex fan-out). The path is opaque — never derive filenames from it; always go
through the JSONB.

**Nullability**: only `raw_bytes`, `size_bytes`, `headers`, and `attachments`
are `NOT NULL` on `messages`. `subject`, `body_text`, `body_html`, `from_addr`,
`to_addrs`, etc. are all nullable — real mail occasionally lacks any of them.
The parser normalizes empty strings to NULL so `WHERE body_text IS NULL` is
the canonical "no body" query.

Folder filtering supports `folder_allow`, `folder_deny`, and **`folder_deny_flags`**
(by RFC 6154 IMAP special-use flag, e.g. `\Trash`, `\Junk`, `\All`). Prefer
flag-based denial — it survives provider locales (`[Gmail]/Bin` vs `Trash`).

## Sync model

- One-shot via `localmail sync`: useful for cron and smoke testing.
  `--limit-per-folder K` caps how many UIDs are processed per mailbox per run;
  the next run resumes from `mailboxes.uidnext`.
- Daemon via `localmail run`: per account, **two threads** — one IDLE on INBOX,
  one periodic poll on every other folder. They share a `psycopg_pool`
  ConnectionPool, coordinate via a `threading.Event` stop signal, and reconnect
  with exponential backoff (1s → 60s cap) on failure. SIGTERM/SIGINT cleanly
  stop IDLE and join threads.

`sync_mailbox` checkpoints `mailboxes.uidnext` after each 50-message batch, so
a crash mid-run loses at most one batch of progress. Re-running is safe — the
existing-id check + `ON CONFLICT DO NOTHING` make inserts idempotent.

### Failure handling (poison-pill messages)

Per-message work runs inside a Postgres `SAVEPOINT msg` so a single bad row
(unexpected encoding, NUL byte the parser missed, etc.) only loses itself,
not the surrounding 49 messages in the batch. On exception:

1. `ROLLBACK TO SAVEPOINT msg` — discards just this message's writes.
2. `record_failed_message` inserts the full RFC822 bytes + error class +
   message + traceback into `failed_messages` (its own nested SAVEPOINT so a
   logging failure can't kill the outer transaction). Re-failures upsert and
   bump `retry_count`.
3. `mailboxes.uidnext` still advances past the failed UID — we don't get
   stuck retrying the same poison pill on every run.

Recovery flow: fix the parser bug, run `localmail retry-failed`. The retry
path calls the same `process_one_message` as live sync, so any fix that works
for new messages also works for the backlog.

The parser itself does two pre-emptive sanitizations to keep poison pills
rare: NUL bytes are stripped from every text field (Postgres `TEXT` rejects
them), and attachment-only messages get synthesized
`subject = "{attachments only}"` / `body_text = "{attachments: name1, name2}"`
so they remain searchable and visible (original bytes/filenames are intact
in `messages.attachments` + the blobs tree).

## Conventions

- **No comments unless the WHY is non-obvious.** Don't restate the SQL or the
  Python.
- **Don't write `.eml` fixtures to disk** — `tests/_eml.py` builds messages
  programmatically with `email.message.EmailMessage`. Same goes for any future
  test fixture: generate, don't check in.
- **DB tests** TRUNCATE before each test (see the `db_conn` / `pool` fixtures).
  Tests must work against the live test DB; never `DROP TABLE`.
- **No `cur.fetchone()[0]` without `assert row is not None` first** — mypy is
  enabled (`[tool.mypy]` in `pyproject.toml`) and will flag it.
- New SQL goes in a new numbered migration file. **Never edit a migration
  that has been applied anywhere** — add the next-numbered file instead.
  Latest is `0003_failed_messages.sql`; next would be `0004_*.sql`.

## Testing notes

- `LOCALMAIL_TEST_DSN` defaults to the **`localmail_test`** database, not the
  live `localmail` one. This is intentional and important — running pytest
  must not touch live archives.
- The `memory_keyring` fixture (autouse) intercepts every `keyring` call so
  real Keychain entries aren't written/read during tests.
- `tests/_fake_imap.py::FakeIMAPClient` is the only place to extend when sync
  or daemon code needs new IMAP verbs.

## Known gaps / non-goals (deliberate)

- No write path to IMAP (no sending, no flag changes, no deletion).
- No web UI / API — downstream agents read directly.
- No multi-host clustering — single-host daemon.
- Gmail "In production" OAuth verification is not pursued; the project stays in
  "Testing" mode with the user as an explicit Test User.
