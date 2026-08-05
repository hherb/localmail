# Postgres maintenance runbook

Two Postgres-level faults that look like localmail bugs but are not, plus the
routine maintenance that prevents the second one. Both were diagnosed on the
macOS deployment (Postgres.app 18.1, port 5532) on 2026-08-01.

Neither involves data loss, and neither is fixed by anything inside localmail.

## When to read this

- `LISTEN`/`NOTIFY` fails with `could not access status of transaction …`
  (`tests/test_daemon_command_listen.py`, `tests/test_daemon_commands_service.py`
  go red while everything else passes).
- Postgres logs start mentioning wraparound, or `age(datfrozenxid)` approaches
  `autovacuum_freeze_max_age`.
- You need the launchd labels to stop/start the daemon on macOS.

---

## Fault 1 — stale NOTIFY queue entry ("could not access status of transaction")

### Symptom

Exactly three tests fail; the rest of the suite passes:

```
tests/test_daemon_command_listen.py::test_notify_sets_reconcile_wake
tests/test_daemon_command_listen.py::test_run_forever_reconciles_early_on_notify
tests/test_daemon_commands_service.py::test_enqueue_emits_notify

psycopg.errors.UndefinedFile: could not access status of transaction 177804769
DETAIL:  Could not open file "pg_xact/00A9": No such file or directory.
```

The failing statement is a bare `LISTEN daemon_commands` on a fresh connection.

### This is not corruption

That error string reads like clog corruption, and it is *not*. Confirm with
these four checks before touching anything — the combination is diagnostic:

```bash
# 1. Is it database-scoped rather than data-scoped?
#    Run LISTEN in three databases. Only the affected one fails.
psql -p 5532 -d localmail_test -c 'LISTEN daemon_commands'   # fails
psql -p 5532 -d localmail      -c 'LISTEN daemon_commands'   # ok
psql -p 5532 -d postgres       -c 'LISTEN daemon_commands'   # ok

# 2. Is the notify queue non-empty?  Non-zero => entries are pinned.
psql -p 5532 -d postgres -c 'SELECT pg_notification_queue_usage()'

# 3. Is a long-lived listener pinning the queue tail?
psql -p 5532 -d postgres -c "SELECT pid, datname, backend_start, left(query,40)
  FROM pg_stat_activity WHERE backend_type='client backend'
  ORDER BY backend_start LIMIT 5"

# 4. Is clog truncation actually correct?  Compare the oldest segment present
#    against the cluster's oldest datfrozenxid.  They should agree.
ls "$PGDATA/pg_xact" | head -1
psql -p 5532 -d postgres -c 'SELECT min(datfrozenxid) FROM pg_database'
```

If a full scan of the affected database is also clean, the data is fine:

```sql
-- every relation, in the affected database; expect only permission-denied
-- on pg_authid / pg_largeobject / pg_statistic / pg_statistic_ext_data /
-- pg_user_mapping, which are unrelated.
SELECT c.oid::regclass FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE c.relkind IN ('r','m') AND n.nspname IN ('public','pg_catalog');
```

### Why it happens

`LISTEN` does not position the backend at the queue *head*. It sets its pointer
to the queue **tail** and walks forward to catch up, and for every entry whose
database OID matches its own it calls `TransactionIdDidCommit(xid)` — a clog
lookup.

So the failure needs two things to line up:

1. A listener pins the queue tail long enough that an old entry is never
   recycled. On this deployment that was the sync daemon's `LISTEN
   daemon_commands` connection ([daemon.py:308](../../src/localmail/daemon.py#L308)),
   idle for ~34 hours.
2. Routine clog truncation advances past that entry's XID. Segment `00A9`
   covers XIDs 177,209,344–178,257,919; the oldest segment retained was `00EB`
   (~246.4M), which correctly matched the cluster's oldest `datfrozenxid`.

The database-OID check is what makes it look data-specific: backends in other
databases walk the same stale entry but skip the XID lookup, so only the one
database errors.

### Fix

The queue lives in shared memory. It resets when its **last** listener detaches,
and it does not survive a server restart. Either route works; the first is less
disruptive because `serve` only ever NOTIFYs and can stay up.

**Option A — cycle the sync daemon (preferred).** Both agents set
`KeepAlive = true`, so `launchctl stop` respawns within `ThrottleInterval`
(30 s), possibly before you can verify. Use `bootout`/`bootstrap`:

```bash
launchctl bootout gui/$UID/com.localmail.daemon
# confirm it is really down (bootout returns before the process exits)
launchctl print gui/$UID/com.localmail.daemon   # expect: could not find service

# must read 0
psql -p 5532 -d postgres -c 'SELECT pg_notification_queue_usage()'
psql -p 5532 -d localmail_test -c 'LISTEN daemon_commands'   # now ok

launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.localmail.daemon.plist
launchctl print gui/$UID/com.localmail.daemon | grep -E '^\s*(state|pid) '
```

A plain `launchctl kickstart -k` also clears the fault eventually, but skips
the verify-while-down step — and the pin has been observed (2026-08-06) to
outlast that restart by **several minutes**: a re-run ~5 minutes after the
kickstart still failed, and the two probes only read clean ~9 minutes after.
Whichever route you take, **gate the pytest re-run on the probes, not on a
fixed wait**. Note also that the bare `psql -p 5532` invocations above assume
the socket dir resolves; from shells where it doesn't, add `-h localhost` and
`-U localmail`.

Then confirm the daemon is really working, not merely running:

```sql
SELECT worker_kind, account_id, state, last_heartbeat_at
FROM daemon_heartbeats ORDER BY last_heartbeat_at DESC LIMIT 6;
```

Zero rows for a few minutes right after the restart is **normal**, not a
failed daemon: `start_workers` wipes the table at startup and then runs the
blob-temp sweep — cold-cache-slow over a large blob tree — before any worker
spawns (#269). Confirm with `sample <pid>` (expect `os_scandir`) rather than
restarting again.

**Option B — restart Postgres.** Guaranteed, and fine here: the daemon and
`serve` both reconnect on their own 1s→60s backoff, so the cost is a few
seconds of paused sync.

### What not to do

- **Do not `dropdb localmail_test`.** The stale entry is in shared memory, not
  in the database, so it would not help. Worse, on this cluster `pgvector` is
  `trusted = false` while the `localmail` role is not a superuser, so
  `CREATE EXTENSION vector` in migration `0004` fails on the rebuild — you end
  up with no test database at all.
- **Do not reach for `pg_resetwal`** or any other clog surgery. Nothing is
  damaged.

---

## Fault 2 — transaction-ID wraparound pressure

### Check

```sql
SELECT datname, age(datfrozenxid) FROM pg_database ORDER BY 2 DESC LIMIT 5;
SHOW autovacuum_freeze_max_age;   -- default 200000000
```

Anti-wraparound autovacuum fires on its own once a database's age reaches
`autovacuum_freeze_max_age`. That is safe but it picks its own moment, which
may be the middle of a sync. Freezing deliberately puts it on your terms.

### Fix

`vacuumdb --all --freeze` is the usual tool, but **check your client version
first** — Postgres.app ships only v16 binaries here
(`/Applications/Postgres.app/Contents/Versions/latest -> 16`) while the server
on 5532 is 18.1. Issuing the SQL directly sidesteps the mismatch entirely:

```python
# as a superuser (hherb / postgres both qualify and connect passwordless)
import psycopg
BASE = "postgresql://postgres@localhost:5532/"
with psycopg.connect(BASE + "postgres", autocommit=True) as c:
    dbs = [r[0] for r in c.execute(
        "SELECT datname FROM pg_database WHERE datallowconn "
        "ORDER BY pg_database_size(datname)").fetchall()]
for name in dbs:
    with psycopg.connect(BASE + name, autocommit=True) as c:
        c.execute("SET statement_timeout = 0")
        c.execute("VACUUM (FREEZE)")
```

Notes:

- `VACUUM` is per-database, so it must be looped; there is no cluster-wide form.
- `VACUUM` cannot run in a transaction block — `autocommit=True` is required.
- Skip `template0` (`datallowconn = false`); the query above already does.
- Superuser is needed to reach tables owned by other roles. The `localmail`
  role is **not** a superuser (it has `CREATEDB` only).
- Smallest-database-first ordering banks the quick wins before the 21 GB
  `localmail` archive.

Safe to run against a live system: freezing is a maintenance operation with no
semantic effect on data. It does dirty pages and generate WAL, so expect real
IO while it runs.

### What it actually cost (2026-08-02, 16 databases)

Far cheaper than the database sizes suggest — most pages were already marked
all-frozen in the visibility map, so `VACUUM (FREEZE)` skipped them:

| Database | Size | `age(datfrozenxid)` before → after | Time |
|---|---:|---|---:|
| `localmail` | 22.9 GB | 186,573,602 → 0 | 1.3 s |
| `bmlibrarian` | 123.6 GB | 197,446,929 → 2 | 3.5 s |
| 14 others | < 100 MB each | 90–197 M → ≤ 24 | 0.1 s each |
| **total** | | | **6.2 s** |

`template0` keeps a large age (~91 M) and is skipped — it has
`datallowconn = false`, nothing writes to it, and PostgreSQL handles it
separately. That is expected, not a leftover.

### Expected cadence

This cluster consumed ~53.6 M XIDs in ~12 hours (≈1,250 XID/s) across all its
services, so `autovacuum_freeze_max_age` (200 M) is reached roughly every two
days. Anti-wraparound autovacuum handles that automatically; run the manual
freeze only when you want to control *when* it happens — before a long import,
say, or when the ages in the check query above are already high and you are
about to start something latency-sensitive.

---

## Reference — macOS launchd agents

The label is the plist filename minus `.plist`, in `~/Library/LaunchAgents/`.
See also [macos-launchd-install.md](macos-launchd-install.md).

| Label | Role | LISTENs? |
|---|---|---|
| `com.localmail.daemon` | sync daemon (`localmail run`) | **yes** — `daemon_commands` |
| `com.localmail.serve` | HTTPS API + admin UI (`localmail serve`) | no, NOTIFY only |

```bash
launchctl list | grep localmail                    # labels + PIDs + last exit
launchctl print gui/$UID/com.localmail.daemon      # full state, path, pid
```

Both set `KeepAlive = true` and `RunAtLoad = true` with
`ThrottleInterval = 30`. Consequences worth remembering:

- `launchctl stop <label>` is **not** "keep it down" — launchd respawns it.
  Use `bootout` when you need a verified window with the process gone.
- `bootout` returns before the process has exited. Poll `launchctl print`
  rather than assuming.
- After `bootout`, the service stays unloaded until you `bootstrap` it back —
  including across the rest of the session. Always pair them.
