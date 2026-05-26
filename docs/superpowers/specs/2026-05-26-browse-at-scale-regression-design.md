# Browse at-scale folder-filter regression coverage (#87)

**Status**: Design approved 2026-05-26. Implementation pending.

**Issue**: [#87 — browse: consider CI-gated at-scale regression coverage for
the folder-filter plan family](https://github.com/hherb/localmail/issues/87)

## Background and risk

PR #86 (closing #85) replaced `JOIN message_labels + SELECT DISTINCT` with
an `EXISTS (SELECT 1 FROM message_labels ...)` semi-join in the
folder-filter branch of `localmail.api.browse.list_messages`. The
DISTINCT-regression signature is a `Unique` node + full-projection `Sort`
on top of a `Nested Loop`, and it only surfaces at scales where the
planner *prefers* the date-ordered walk over the inverted semi-join —
roughly, broad folders (~50% of an account's rows) on archives of at
least a few tens of thousands of messages.

Today this scale is covered only by the operator-run acceptance harness
at `tests/acceptance/run_browse_explain.py`. The unit-scale eligibility
tests in `tests/test_api_browse_plan.py` cover the SQL-shape question
("is `messages_recent_idx` still ELIGIBLE for the messages side under
the semi-join?") but deliberately do not forbid Sort nodes — at fixture
scale the planner correctly inverts the semi-join and Sorts to restore
the ORDER BY, which is legitimate.

**Concrete risk**: a future refactor of `localmail.api.browse.build_where`
— e.g. re-introducing `SELECT DISTINCT`, switching the EXISTS to a
non-correlated `IN (SELECT ...)`, or otherwise breaking the semi-join
short-circuit — would silently regress at-scale folder-filtered browse
pages with no automated signal. The only catches would be a human
running the operator harness or an end user noticing slow pages.

The repo also currently has **no Python CI** at all — only the GUI CI at
`.github/workflows/gui-ci.yml`. So #87 implicitly requires standing up a
Python CI pipeline before the at-scale regression test can be gated.

## Scope

This spec covers, as one bundle:

1. A new Python CI workflow (`.github/workflows/python-ci.yml`) running
   the full pytest suite on every push to `main` and every PR touching
   Python paths, against a Postgres 18 service container.
2. A refactor of `tests/acceptance/run_browse_explain.py` to extract its
   reusable primitives (seed config, seeding functions, probe builders,
   `classify_plan`) into a shared library module
   `tests/acceptance/browse_explain_lib.py`. The CLI stays operator-facing
   and preserves all flags.
3. A new pytest test `tests/test_browse_at_scale.py` that consumes the
   shared library, seeds a calibrated archive (default scale exposed via
   `LOCALMAIL_REGRESSION_ROWS` env var), and asserts the structural
   regression signature: `Index Scan using messages_recent_idx` present,
   no `Unique` node, no full-projection `Sort` on a `Nested Loop` against
   `messages`.

**Out of scope**:

- A buffer-hit ceiling assertion (the issue offered this as an
  alternative; we picked plan-signature only, since it's structurally
  stable across PG version drift and gives a more useful diagnostic
  when it fires).
- A PG version matrix in CI. One version (18) is pinned to match the
  developer's local environment; a docs note explains how a future
  contributor can add a matrix.
- A Python version matrix. One version (3.12) matches
  `pyproject.toml:requires-python`.
- Tightening the existing unit-scale eligibility tests. They correctly
  permit a Sort node for legitimate ordering reasons at fixture scale.

## Design

### Component 1 — `tests/acceptance/browse_explain_lib.py` (new)

Pure-library module extracted from `run_browse_explain.py`. Exports:

- **Dataclasses** (unchanged from the existing harness):
  `SeedConfig`, `ProbeSpec`, `PlanSummary`, `FolderMailboxes`.
- **Seeding functions** (renamed from `_seed_*` to `seed_*` since they're
  now public):
  `seed_accounts(conn, num_accounts) -> list[int]`,
  `seed_messages(conn, account_ids, cfg) -> None`,
  `seed_folder_filter_mailboxes(conn, account_ids) -> FolderMailboxes`.
- **Probe construction**:
  `build_probes(cfg, account_ids, page_size, *, folders=None) -> list[ProbeSpec]`,
  `build_folder_filter_probes(account_ids, cursor_pos, folders) -> list[ProbeSpec]`.
- **Execution + classification**:
  `run_explain(conn, probe, page_size, *, predicate_form='current') -> PlanSummary`,
  `classify_plan(explain_text) -> PlanSummary`. The `PlanSummary`
  dataclass gains a `has_unique_node: bool` field populated by
  `classify_plan`; the existing CLI ignores it, and the new test's
  signature assertion reads it.
- **Truncation contract** (already in the harness):
  `TRUNCATE_SQL` constant, exported so the test can choose its own
  scope rather than re-implement it.

The CLI's argument parsing, table rendering, JSON emission, and verdict
text remain in `run_browse_explain.py`. The pure library never reads
`argv`, never writes to stdout, never exits — only the CLI does.

Module-level constants stay where they are *unless* they're config
parameters (`_NULL_INTERNAL_DATE_FRAC`, `_BOTH_NULL_FRAC`,
`_DATE_SPAN_DAYS`, `_FOLDER_FRACTIONS`, `_COPY_BATCH`, `_DISTRIBUTIONS`,
`_DEFAULT_PAGE_SIZE`, `_VALID_PREDICATE_FORMS`) — those move with the
seeding code to the library. Renamed from leading-underscore form to
canonical (`NULL_INTERNAL_DATE_FRAC`, etc.) since they're now public
API.

### Component 2 — `tests/acceptance/run_browse_explain.py` (refactor)

After the refactor:

- All `_seed_*`, `_build_probes`, `_run_explain`, `classify_plan`,
  dataclasses, module constants → removed (moved to library).
- The remaining file: `main()` entry point, `argparse` configuration,
  `_render_table`, `_verdict`, `_verdict_for_folderless`,
  `_verdict_for_folder_filter`, the `if __name__ == "__main__"` shim.
- Imports from the new library.

Estimated lines: ~150 (down from ~1000).

Operator-facing contract preserved 1:1 — every flag still works, output
format unchanged.

### Component 3 — `tests/test_browse_at_scale.py` (new)

```python
"""At-scale regression coverage for the broad-folder browse plan family (#87).

Pins that the DISTINCT-regression signature — `Unique` node plus
full-projection Sort on top of a Nested Loop — cannot silently come back
through a refactor of `build_where`. Sits between the unit-scale
eligibility tests in test_api_browse_plan.py (which deliberately permit
a Sort because the planner inverts the semi-join at fixture scale) and
the operator-run harness in tests/acceptance/run_browse_explain.py
(which catches this class but is not gated).

Seeds a calibrated archive shape that reliably triggers the date-ordered
walk for the broad-folder probe (i.e. plan family "index-walk (option 1)").
Below that scale the planner inverts the semi-join — legitimate, but
the regression class can't surface, so the test fails its calibration
gate with a diagnostic asking the operator to bump
LOCALMAIL_REGRESSION_ROWS.

Scale tunable via env var. Auto-skips when no DB (existing db_conn
fixture).
"""
```

The test:

1. Resolves `n_rows` from `os.environ.get("LOCALMAIL_REGRESSION_ROWS",
   DEFAULT_REGRESSION_ROWS)`. `DEFAULT_REGRESSION_ROWS` is a module
   constant, value calibrated during implementation.
2. Builds `SeedConfig(total_rows=n_rows, num_accounts=3,
   distribution="balanced")` — balanced because we're testing
   plan family, not ACL distribution.
3. Calls `seed_accounts`, `seed_messages`,
   `seed_folder_filter_mailboxes` against the existing `db_conn`
   fixture; binds `first_account_id = account_ids[0]` and
   `broad_mailbox_id = folders.broad[0]`.
4. Builds one probe via
   `ProbeSpec(name="broad folder initial page",
   account_ids=[first_account_id], cursor=None,
   folder_ids=[broad_mailbox_id])` — the broad-folder initial-page probe.
5. Calls `run_explain(conn, probe, page_size=DEFAULT_PAGE_SIZE)`.
6. **Calibration gate**: `assert summary.plan_family == "index-walk (option 1)"`.
   Failure message includes the raw EXPLAIN text and the hint
   `"bump LOCALMAIL_REGRESSION_ROWS"`. This must succeed before the
   signature assertion runs — otherwise the signature would be vacuously
   green.
7. **Signature assertion**:
   - `assert "Index Scan using messages_recent_idx" in summary.raw` —
     belt-and-suspenders on top of the calibration gate.
   - `assert not summary.has_unique_node` — the canonical DISTINCT
     marker. Postgres only emits `Unique` to enforce `SELECT DISTINCT`;
     a clean EXISTS semi-join never produces one. This is the
     load-bearing assertion for the #87 regression class.
   - `assert not summary.has_full_sort` — secondary heuristic against
     a plan that abandons the date-ordered walk. The legitimate
     semi-join inversion at sub-calibration scale would also fail this,
     but step 6 has already ruled that out. At calibrated scale the
     date-ordered walk does not need a Sort.

   `has_unique_node` is a new field on `PlanSummary` added in the library
   refactor; the heuristic is `any(ln.strip().startswith("Unique") or
   ln.strip().startswith("->  Unique") for ln in lines)`.
8. **Observability**: log `summary.plan_family`, `summary.execution_ms`,
   `summary.shared_hit_blocks`, `summary.shared_read_blocks`, and the
   probe's row count via `caplog` at INFO level. Not gated.

The truncation in `db_conn` already covers the test's tables; no
explicit cleanup needed.

Estimated lines: ~120.

### Component 4 — `.github/workflows/python-ci.yml` (new)

```yaml
name: python-ci

on:
  push:
    branches: [main]
    paths:
      - 'src/**'
      - 'tests/**'
      - 'migrations/**'
      - 'pyproject.toml'
      - 'uv.lock'
      - '.github/workflows/python-ci.yml'
  pull_request:
    paths:
      - 'src/**'
      - 'tests/**'
      - 'migrations/**'
      - 'pyproject.toml'
      - 'uv.lock'
      - '.github/workflows/python-ci.yml'

jobs:
  pytest:
    name: pytest (PG ${{ matrix.postgres }}, Python ${{ matrix.python }})
    runs-on: ubuntu-latest
    strategy:
      fail-fast: false
      matrix:
        postgres: ["18"]
        python: ["3.12"]
    services:
      postgres:
        image: postgres:${{ matrix.postgres }}
        env:
          POSTGRES_USER: localmail
          POSTGRES_PASSWORD: 'local@@mail'
          POSTGRES_DB: localmail_test
        ports:
          - 5432:5432
        options: >-
          --health-cmd pg_isready
          --health-interval 10s
          --health-timeout 5s
          --health-retries 10
    env:
      LOCALMAIL_TEST_DSN: >-
        postgresql://localmail:local%40%40mail@localhost:5432/localmail_test
    steps:
      - uses: actions/checkout@v6
      - name: Install uv
        uses: astral-sh/setup-uv@v6
        with:
          enable-cache: true
      - name: Pin Python via uv
        run: uv python install ${{ matrix.python }}
      - name: Sync dependencies
        run: uv sync --frozen
      - name: Run pytest
        run: uv run pytest -q
```

Key choices and rationale:

- **`postgres:18`** matches the operator's local environment (PG 18.1).
  No version matrix yet — adding `postgres: ["17", "18"]` is one line
  when warranted.
- **Port 5432 in CI**, not 5532 like local dev. The DSN env var is set
  on the job, so the test conftest reads from `LOCALMAIL_TEST_DSN` and
  picks the right port.
- **`uv.lock` is the cache key surface** via `setup-uv`'s `enable-cache`.
  No manual `actions/cache@v4` step needed.
- **`uv sync --frozen`** to enforce the lockfile, not float dependencies
  on CI.
- **`uv run pytest -q`** — quiet output by default. Tests still print
  via `caplog` when interesting, and pytest's failure output stays
  verbose on failure.
- **No `pgvector`** assumed by this workflow. The migrations include
  pgvector — if any test depends on pgvector being present, this
  workflow will surface that as a failure; the fix is to either add
  pgvector to the image (`pgvector/pgvector:pg18`) or skip the relevant
  tests when the extension is absent. We resolve this empirically during
  the implementation's first CI run.

### Data flow (test execution)

```
GitHub Action runner
  └─ postgres:18 service container (port 5432)
       └─ migrations applied by conftest.db_dsn (apply_migrations)
            └─ pytest run
                 └─ tests/test_browse_at_scale.py
                      ├─ db_conn fixture (TRUNCATE)
                      ├─ browse_explain_lib.seed_* (~N rows COPY'd)
                      ├─ classify_plan(EXPLAIN ANALYZE output)
                      ├─ calibration gate (plan_family check)
                      ├─ signature assertions
                      └─ caplog observability
```

### Error handling

| Scenario | Behaviour |
|---|---|
| No DB reachable locally | `db_conn` fixture skips (existing behavior) |
| CI service container down | Job fails on `pg_isready` health check |
| `pgvector` extension missing | Migration runner surfaces a clean error; fix is image swap or test skip |
| PG version emits new plan family | `classify_plan` returns `"other"`; calibration gate fails with raw EXPLAIN text and a hint to update the classifier |
| Scale below calibration threshold | Calibration gate fails with the chosen `plan_family` and the hint `"bump LOCALMAIL_REGRESSION_ROWS"` |
| DISTINCT regresses back | Signature assertion fails with the raw EXPLAIN text pointing at the `Unique` node or the full-projection `Sort` |

### Testing the new code

- **`browse_explain_lib.py`** — covered by (a) the new pytest test, which
  exercises the seeding + classifier + probe builder; and (b) the
  existing operator-facing CLI in `run_browse_explain.py`, which becomes
  a thin consumer of the library. If either consumer breaks, CI catches it.
- **The new pytest test** — its own correctness is exercised by the
  calibration gate. If the assertions are vacuously green because the
  planner inverted the semi-join, the gate fails. If the gate is
  satisfied, the assertions are non-trivial.
- **The CI workflow** — first validated by its own PR run.

### Calibration step (implementation-time)

During implementation:

1. Run the test locally at `LOCALMAIL_REGRESSION_ROWS=5000` upward in
   1.5× increments (5000, 7500, 11000, 16500, 25000, ...) until
   the calibration gate passes reliably (5+ consecutive runs).
2. The smallest reliable `N` becomes `DEFAULT_REGRESSION_ROWS` with
   a 1.5× headroom multiplier to absorb PG planner cost-model jitter.
3. Document the chosen `N` and the reasoning in the test docstring.

Expected order of magnitude: tens of thousands of rows. The issue
suggested 20k×3; we calibrate empirically rather than ship that number
on faith.

## File map (post-implementation)

```
docs/superpowers/specs/
  2026-05-26-browse-at-scale-regression-design.md     # this spec

.github/workflows/
  python-ci.yml                                       # NEW

tests/
  test_browse_at_scale.py                             # NEW
  acceptance/
    browse_explain_lib.py                             # NEW (extracted)
    run_browse_explain.py                             # REFACTORED (thin CLI)
```

No other files touched. No migrations. No `src/localmail/` changes.

## Open questions / explicitly accepted risks

1. **PG version drift breaking `classify_plan`.** Mitigation: the
   calibration gate, classifier returning `"other"`, and raw-EXPLAIN
   diagnostics give a fail-loud signal rather than silent green.
2. **`pgvector` requirement in the migration chain.** Verified
   empirically by the first CI run; the fix is either `pgvector/pgvector:pg18`
   or a skip on the affected tests.
3. **Test runtime on GH Actions hosted runners.** A 20-30k row COPY +
   ANALYZE + EXPLAIN ANALYZE budget should be well under 30 seconds.
   If it overshoots, the fix is to mark the test
   `@pytest.mark.scaled_regression` and run it as a separate CI job
   in the same workflow.
4. **`uv.lock` mismatch with declared deps.** `uv sync --frozen` is
   intentional; if a contributor commits a change that desyncs the lock
   from `pyproject.toml`, CI fails clearly.
