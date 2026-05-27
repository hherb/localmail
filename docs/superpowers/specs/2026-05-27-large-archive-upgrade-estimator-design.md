# Large-archive upgrade estimator + operator runbook

**Status**: design approved 2026-05-27. Closes [#2](https://github.com/hherb/localmail/issues/2).

## Goal

Give operators a pre-flight diagnostic + documented procedure for
lock-heavy migrations against a populated `messages` table. The named
case is migration 0006 (`fts_v2` generated column + two GIN indexes),
but the framework supports adding estimators for other migrations
(0015, 0018, future) without rework.

## Non-goals

- **Do not** make migration 0006's `ADD COLUMN ... GENERATED ALWAYS AS
  ... STORED` an online operation. The shadow-column / dual-write /
  swap procedure is too invasive for an issue that has been deferred
  long enough to know it isn't actively blocking anyone.
- **Do not** edit existing migration files. Per [CLAUDE.md](../../../CLAUDE.md)
  ("never edit a migration that has been applied anywhere"), 0006 is
  immutable.
- **Do not** ship a new migration that drops & rebuilds the GIN indexes
  CONCURRENTLY. That would degrade search reads on every existing
  install (every operator pays for an edge case they may never hit).
- **Do not** attempt sample-based duration timing. Rough
  throughput-based heuristics with caveats are honest; per-environment
  sampling adds significant code complexity for marginal accuracy gain.
- Only migration 0006 ships with an estimator implementation in this
  scope. 0015 (`body_lang` column + partial btree index) and 0018
  (`internal_date` column + `messages_recent_idx` btree expression
  index) are noted in the runbook but get estimators only when an
  operator reports needing them.

## Background

Migration 0006 (`migrations/0006_search_indexes.sql`) has two sources of
lock pain against a populated `messages` table:

1. `ALTER TABLE messages ADD COLUMN fts_v2 tsvector GENERATED ALWAYS
   AS (...) STORED` — Postgres must compute the expression for every
   existing row, which triggers a full table rewrite under
   `ACCESS EXCLUSIVE`. This blocks reads as well as writes.
2. `CREATE INDEX ... USING GIN (fts_v2)` and the same on
   `message_chunks` — without `CONCURRENTLY`, holds a `ShareLock` for
   the duration of the build, blocking writes.

The HNSW index in the same migration *is* built `CONCURRENTLY`, which
makes the missing `CONCURRENTLY` on the GIN indexes a visible
inconsistency.

Fresh installs and small archives are unaffected — the migration
completes in seconds. The pain only surfaces when adopting localmail
against a pre-existing populated `messages` table (operator imports
from another source, then runs `localmail init-db`).

## Components

### `src/localmail/upgrade_estimate.py` (new)

Pure module — no IO except via an injected `psycopg.Connection`.
Reusable from the CLI today, from a future HTTP route or MCP tool
tomorrow without refactor.

```python
@dataclass(frozen=True)
class EstimateResult:
    revision: str
    status: Literal["pending", "applied", "not_applicable"]
    current_bytes: dict[str, int]       # actual sizes when status == "applied"
    projected_bytes: dict[str, int]     # projected sizes when status == "pending"
    projected_duration_s: float         # projected lock-holding seconds; 0.0 when applied
    warnings: list[str]


EstimatorFn = Callable[[Connection, UpgradeEstimateConfig, bool], EstimateResult]

ESTIMATORS: dict[str, EstimatorFn] = {
    "0006_search_indexes": estimate_0006,
}


def estimate_0006(
    conn: Connection,
    cfg: UpgradeEstimateConfig,
    applied: bool,
) -> EstimateResult:
    ...
```

The third positional argument `applied` is set by the caller from the
`schema_migrations` lookup. It selects which branch the estimator
takes: `applied=True` → read actual sizes, `applied=False` → run
projection math. The estimator does not consult `schema_migrations`
itself — keeps the function pure-with-respect-to-DB-state-it-doesn't-own
and lets unit tests exercise both branches without dropping rows from
`schema_migrations`.

`estimate_0006` reads:

- Row count of `messages` (cheap — `SELECT COUNT(*)`).
- `pg_total_relation_size('messages')` (cheap — catalog lookup).
- `SELECT avg(length(coalesce(subject, '')) + length(coalesce(body_text, ''))
   + length(coalesce(body_html, ''))) FROM messages` — single heap scan,
  read-only, `AccessShareLock`. Acceptable cost for a pre-flight check
  even at millions of rows.

It projects:

- `fts_v2` column footprint: `rows × avg_text_len × cfg.fts_v2_blowup_factor`
- Per-GIN-index footprint: `projected_fts_v2_bytes × cfg.gin_size_factor`
- Table-rewrite duration:
  `(current_table_size + projected_fts_v2_bytes) / (cfg.table_rewrite_mb_per_sec × 1_048_576)`
- GIN-build duration (two indexes):
  `projected_gin_bytes / (cfg.gin_build_mb_per_sec × 1_048_576) × 2`

When `status == "applied"`, `current_bytes` is filled from
`pg_total_relation_size('messages_fts_v2_idx')` + the chunks GIN. If a
named index is missing despite the migration being recorded as applied,
`warnings` gets `"index <name> missing despite migration applied"` and
the size key is omitted — the call does not fail.

### `src/localmail/config.py` (modified)

New sub-model `UpgradeEstimateConfig` attached to `LocalmailConfig.upgrade`:

```python
class UpgradeEstimateConfig(BaseModel):
    fts_v2_blowup_factor: float = 1.5     # tsvector stores tokens + positions
    gin_size_factor: float = 0.4          # typical GIN-on-tsvector ratio
    table_rewrite_mb_per_sec: float = 80.0  # SSD baseline
    gin_build_mb_per_sec: float = 30.0      # SSD baseline
```

No magic numbers in `upgrade_estimate.py` — every constant comes from
config. An operator on slow storage can tune the throughput rates in
`config.toml`.

Add the new section to `config.example.toml` with comments explaining
when to tune each value.

### `src/localmail/cli.py` (modified)

New `estimate-upgrade` command (~25 lines):

```
localmail estimate-upgrade [--format text|json]
```

- Default (`--format text`): human-readable table — one row per
  registered estimator:
  `revision | status | current_size | projected_size | projected_duration | warnings`.
- `--format json`: machine-readable JSON list, one object per estimator,
  every field of `EstimateResult` present. Matches the convention of
  the existing `search-status`, `list-failed`, and
  `list-failed-embeddings` commands.

Connects via existing `_dsn` + `_config` helpers (same pattern as
other CLI commands).

### `docs/operations/upgrade-runbook.md` (new)

Operator-facing markdown runbook. New top-level subdirectory under
`docs/` for operations-class documentation (future runbooks — backup,
monitoring, capacity planning — would live alongside).

Sections:

1. **When to read this** — checklist of scenarios: importing from a
   prior IMAP archive, restoring from backup, mid-upgrade with the
   daemon stopped.
2. **The lock-heavy migrations** — table listing 0006, 0015, 0018
   with a one-paragraph "what it does" each.
3. **`localmail estimate-upgrade`** — example invocation, example
   output, how to interpret each column, when to re-tune the config
   rates.
4. **Manual online procedure for 0006** — for operators with strong
   Postgres ops skills who want to avoid the lock entirely. Includes
   the trade-off discussion (extra storage during the swap, brief
   `AccessExclusiveLock` on rename), and an explicit "if you don't
   know why each step is needed, schedule downtime and run `init-db`
   instead" caveat.
5. **Disk-space planning** — rule of thumb for sizing reserved space
   before running 0006.

### Cross-references

- **README.md** — add a short paragraph near the existing "Upgrading
  to migration 0016" callout (around line 96), pointing operators to
  the runbook before they run `init-db` on a large pre-existing
  archive.
- **CLAUDE.md** — add `localmail estimate-upgrade` to the Commands
  block, with a one-line description referencing the runbook for the
  full procedure.

## Data flow

```
localmail estimate-upgrade [--format text|json]
   ↓
cli._cmd_estimate_upgrade(ctx)
   ↓
load_config()  →  LocalmailConfig.upgrade   (throughput rates)
_dsn(ctx)      →  DSN string
   ↓
psycopg.connect(dsn)
   ↓
SELECT revision FROM schema_migrations      →  applied set
   ↓
for revision, fn in ESTIMATORS.items():
    result = fn(conn, cfg.upgrade, applied=(revision in applied))
    print(format_row(result, json=json))
   ↓
exit 0
```

All DB queries are read-only — no writes, no transaction needed beyond
the implicit one psycopg opens around the SELECTs.

## Failure modes

| Trigger | Behaviour |
|---|---|
| DB unreachable | `psycopg.OperationalError` bubbles up. CLI prints `Error: <reason>` on stderr via `click.ClickException` and exits **1** (Click idiom, matches every other `localmail` subcommand). The JSON output channel is the structured one for scripts; the exit code is intentionally not differentiated from other CLI errors. |
| `messages` table doesn't exist | Estimator catches `UndefinedTable`, returns `status="not_applicable"` with row `"messages table not present — run `localmail init-db` first"`. Exit 0. |
| `schema_migrations` doesn't exist | Treated as "everything pending". No exception escapes. |
| Empty `messages` table on pending 0006 | Projections all 0 bytes / 0 seconds. Row says `"messages table empty — migration will be cheap"`. Exit 0. |
| Applied 0006 with index manually dropped | `status="applied"`, `warnings=["index messages_fts_v2_idx missing despite migration applied"]`. Exit 0. |

No retry. No backoff. No DB writes. The command is idempotent by
construction.

## Testing strategy (TDD)

### `tests/test_upgrade_estimate.py` (new)

Unit tests against the pure module via the existing `db_conn` fixture
(TRUNCATE-per-test, real Postgres at `LOCALMAIL_TEST_DSN`).

All tests pass `applied=...` explicitly so the projection vs.
read-actual branch is selected by the test, not derived from fixture
state.

1. `test_estimate_0006_pending_empty_messages` — fresh DB, no rows,
   `applied=False`. Returns `status="pending"`, all projected bytes
   = 0, durations = 0. Confirms no divide-by-zero.
2. `test_estimate_0006_pending_with_seeded_rows` — seed 100 messages
   via `tests/_eml.py` builders with known text length, call with
   `applied=False`. Assert `projected_bytes["fts_v2"] ≈ rows ×
   text_len × fts_v2_blowup_factor` within ±10% tolerance (absorbs
   `avg()` rounding).
3. `test_estimate_0006_applied_reports_actual_sizes` — seed rows,
   call with `applied=True`. Assert `status="applied"`,
   `current_bytes` non-zero for both indexes, projections absent.
4. `test_estimate_0006_applied_with_index_missing` — `DROP INDEX
   messages_fts_v2_idx` inside a savepoint, call with `applied=True`,
   assert warning string present, no exception. Savepoint rollback
   restores the fixture index for subsequent tests.
5. `test_duration_uses_config_throughput_rates` — override
   `UpgradeEstimateConfig.table_rewrite_mb_per_sec` to `1.0`, seed
   rows, call with `applied=False`, assert duration matches
   arithmetic. Proves no magic numbers leaked through to the
   estimator.
6. `test_unknown_revision_raises` — `ESTIMATORS["0099_nonsense"]`
   raises `KeyError`. Trivial but documents the registry contract.

### `tests/test_cli_estimate_upgrade.py` (new)

Integration tests using `cli_config` (the fixture from PR #101) +
`db_conn`.

1. `test_cli_estimate_upgrade_human_output` — run CLI, capture stdout,
   assert it contains revision name, status, and a duration string.
   Don't assert exact bytes (too brittle); assert structure.
2. `test_cli_estimate_upgrade_json_output` — `--format json`, parse
   JSON, assert every estimator row has keys: `revision`, `status`,
   `current_bytes`, `projected_bytes`, `projected_duration_s`,
   `warnings`.
3. `test_cli_estimate_upgrade_db_unreachable` — DSN at unreachable
   host, exits **1** (Click idiom) with connection error on stderr.

### Test ordering

TDD red → green per commit:

1. `UpgradeEstimateConfig` test (round-trip via pydantic) — red, then green.
2. Module tests 1, 2, 3, 5, 6 — red, then implement `upgrade_estimate.py` → green.
3. Module test 4 (index-missing) — red, then add warnings handling → green.
4. CLI tests 1, 2, 3 — red, then implement `_cmd_estimate_upgrade` → green.
5. Runbook + cross-references.

## Acceptance criteria for closing #2

- `localmail estimate-upgrade` runs against a populated DB and returns
  size + rough duration projections for migration 0006.
- `--format json` output is parseable and structurally stable.
- The runbook at `docs/operations/upgrade-runbook.md` covers:
  - What 0006 does to a populated `messages` table.
  - How to interpret estimator output.
  - A manual online procedure (with the "requires Postgres ops chops"
    caveat).
  - Disk-space planning rule of thumb.
- README and CLAUDE.md cross-reference the runbook.
- All new code is config-driven; the four throughput-rate constants
  live in `UpgradeEstimateConfig` and nowhere else.
- Full test suite green; new tests cover happy path, empty DB,
  applied-status, applied-with-missing-index, and the
  config-overrides-affect-output invariant.
- Issue #2 closes via `Closes #2` in the merge commit body.

## Open decisions

- **Exact runbook prose** — drafted during implementation, not in this
  design.
- **Exact CLI table format** — default proposal:
  `revision | status | current_size | projected_size | projected_duration | warnings`,
  human-readable column widths chosen at format time. Tweakable
  without affecting `EstimateResult` or the JSON shape.

## Risks

- **Throughput defaults may be wrong for users' hardware.** Mitigation:
  the four constants are config-overridable, and the runbook
  explicitly tells operators to halve the rates on HDD or low-memory
  hosts. The runbook will include a "calibrate on a sample table"
  procedure for operators who care about accuracy.
- **`avg(length(...))` on a multi-GB `messages` table takes seconds.**
  Mitigation: this is a one-shot diagnostic, not a hot path; the cost
  is acceptable. Documented in the runbook so operators don't think
  the CLI is hung.
- **The framework grows estimators we don't need.** Mitigation: the
  registry is `dict[str, Callable]`; adding an entry is one line.
  YAGNI-shaped — only 0006 ships now.
