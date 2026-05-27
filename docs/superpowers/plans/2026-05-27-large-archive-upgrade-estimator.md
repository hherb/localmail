# Large-archive upgrade estimator + runbook — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close [#2](https://github.com/hherb/localmail/issues/2) by adding `localmail estimate-upgrade` (pre-flight CLI that reports size + rough duration projections for migration 0006 against a populated `messages` table) and an operator runbook explaining when to run it.

**Architecture:** A pure module `src/localmail/upgrade_estimate.py` exposes `EstimateResult` + an `ESTIMATORS` registry of per-migration estimator functions (one entry today: `estimate_0006`). A new `LocalmailConfig.upgrade` sub-model holds throughput-rate constants. The CLI command iterates the registry and prints text or JSON. No schema changes; no new migrations.

**Tech Stack:** Python 3.12, `psycopg` v3 (existing), `pydantic` v2 (existing), `click` (existing), `pytest` (existing). New module is ~150 lines; runbook is markdown.

**Spec:** [docs/superpowers/specs/2026-05-27-large-archive-upgrade-estimator-design.md](../specs/2026-05-27-large-archive-upgrade-estimator-design.md)

**Branch:** `issue-2-upgrade-estimator-runbook` (already created; spec commit `38c933e`).

---

## File map

**Created:**
- `src/localmail/upgrade_estimate.py` — pure estimator module + registry
- `tests/test_upgrade_estimate.py` — unit tests (~250 lines)
- `tests/test_cli_estimate_upgrade.py` — CLI integration tests (~120 lines)
- `docs/operations/upgrade-runbook.md` — operator runbook

**Modified:**
- `src/localmail/config.py` — add `UpgradeEstimateConfig` sub-model, attach to `Config.upgrade`
- `src/localmail/cli.py` — add `estimate-upgrade` command
- `config.example.toml` — add commented `[upgrade]` section
- `README.md` — add cross-reference to the runbook
- `CLAUDE.md` — add command to Commands block

**Untouched:**
- Existing migrations (per CLAUDE.md rule, immutable).
- `src/localmail/db.py`, `src/localmail/sync.py`, all of `src/localmail/api/` — no changes needed.

---

## Pre-flight (one-time, before Task 1)

- [ ] **Step P.1: Verify branch state**

```bash
cd /Users/hherb/src/localmail
git status
git log --oneline -3
```

Expected: on `issue-2-upgrade-estimator-runbook`, tip is `38c933e docs(spec): large-archive upgrade estimator + operator runbook (#2)`, working tree clean (only `.claude/settings.local.json` untracked).

- [ ] **Step P.2: Verify test environment**

```bash
unset VIRTUAL_ENV && uv run pytest -q tests/test_config.py 2>/dev/null | tail -5
```

Expected: tests pass (one of the existing config tests). Confirms `uv run pytest` and the Postgres test DB are usable.

If `tests/test_config.py` doesn't exist, run `unset VIRTUAL_ENV && uv run pytest -q --collect-only 2>&1 | tail -5` to confirm pytest collection works at all.

---

## Task 1: Add `UpgradeEstimateConfig` to config

**Files:**
- Modify: `src/localmail/config.py` (add sub-model, attach to `Config`)
- Modify: `config.example.toml` (add commented `[upgrade]` section)
- Test: existing `tests/test_config.py` (extend, or create if missing — check first)

- [ ] **Step 1.1: Confirm test file location**

```bash
ls tests/test_config*.py 2>/dev/null
```

If `tests/test_config.py` exists, append to it. If not, the new test goes in `tests/test_upgrade_estimate_config.py` (new file). Both are acceptable; the plan below assumes the new file path for explicitness.

- [ ] **Step 1.2: Write failing test**

Create `tests/test_upgrade_estimate_config.py`:

```python
"""Tests for the UpgradeEstimateConfig pydantic sub-model."""

from localmail.config import Config, UpgradeEstimateConfig


def test_upgrade_estimate_config_defaults():
    cfg = UpgradeEstimateConfig()
    assert cfg.fts_v2_blowup_factor == 1.5
    assert cfg.gin_size_factor == 0.4
    assert cfg.table_rewrite_mb_per_sec == 80.0
    assert cfg.gin_build_mb_per_sec == 30.0


def test_upgrade_estimate_config_overrides():
    cfg = UpgradeEstimateConfig(
        fts_v2_blowup_factor=2.0,
        gin_size_factor=0.5,
        table_rewrite_mb_per_sec=40.0,
        gin_build_mb_per_sec=15.0,
    )
    assert cfg.fts_v2_blowup_factor == 2.0
    assert cfg.gin_size_factor == 0.5
    assert cfg.table_rewrite_mb_per_sec == 40.0
    assert cfg.gin_build_mb_per_sec == 15.0


def test_config_has_upgrade_subsection_by_default():
    cfg = Config(database={"dsn": "postgresql://x"})
    assert isinstance(cfg.upgrade, UpgradeEstimateConfig)
    assert cfg.upgrade.table_rewrite_mb_per_sec == 80.0


def test_config_round_trip_with_upgrade_section():
    """Parsing a TOML-like dict with an [upgrade] block must round-trip."""
    cfg = Config.model_validate({
        "database": {"dsn": "postgresql://x"},
        "upgrade": {"table_rewrite_mb_per_sec": 20.0},
    })
    assert cfg.upgrade.table_rewrite_mb_per_sec == 20.0
    # Untouched field still has the default.
    assert cfg.upgrade.fts_v2_blowup_factor == 1.5
```

- [ ] **Step 1.3: Run test to verify it fails**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_upgrade_estimate_config.py -v
```

Expected: 4 failures — `ImportError: cannot import name 'UpgradeEstimateConfig' from 'localmail.config'`.

- [ ] **Step 1.4: Add `UpgradeEstimateConfig` to `src/localmail/config.py`**

After the `SearchConfig` class (after line 299, before `class Config`):

```python
class UpgradeEstimateConfig(BaseModel):
    """Throughput rates used by `localmail estimate-upgrade` to project
    lock-holding duration for lock-heavy migrations against a populated
    `messages` table. See docs/operations/upgrade-runbook.md.

    All four constants are tunable per-installation: an operator on slow
    storage (HDD, low-memory) should halve the throughput rates; an
    operator on NVMe with abundant RAM may double them. The runbook
    documents a calibration procedure for operators who care about
    accuracy.
    """

    # tsvector stores tokens + positions, so the stored column is larger
    # than the raw concatenated text it indexes. 1.5x is the rule of
    # thumb for English / Western European text; languages with longer
    # average tokens (German compounds, Finnish) trend toward 1.7x.
    fts_v2_blowup_factor: float = 1.5

    # Typical GIN-on-tsvector ratio: the index is ~40% of the column it
    # covers. Varies with token uniqueness (higher for diverse vocab).
    gin_size_factor: float = 0.4

    # SSD baseline for `ALTER TABLE ... ADD COLUMN ... GENERATED ALWAYS
    # AS ... STORED` — Postgres rewrites the whole table heap. HDD is
    # ~10x slower; NVMe is ~2x faster.
    table_rewrite_mb_per_sec: float = 80.0

    # SSD baseline for `CREATE INDEX ... USING GIN`. GIN builds are
    # CPU-bound (token enumeration) more than I/O-bound; the rate is
    # less hardware-sensitive than table_rewrite_mb_per_sec.
    gin_build_mb_per_sec: float = 30.0
```

Then add the attached field to `class Config`:

```python
class Config(BaseModel):
    database: DatabaseConfig
    attachments: AttachmentsConfig = AttachmentsConfig()
    daemon: DaemonConfig = DaemonConfig()
    serve: ServeConfig = Field(default_factory=ServeConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    gmail_oauth: GmailOAuthConfig | None = None
    accounts: list[AccountConfig] = Field(default_factory=list)
    search: SearchConfig = Field(default_factory=SearchConfig)
    upgrade: UpgradeEstimateConfig = Field(default_factory=UpgradeEstimateConfig)
```

(Add only the last line; the rest of `Config` is unchanged.)

- [ ] **Step 1.5: Run test to verify it passes**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_upgrade_estimate_config.py -v
```

Expected: 4 passed.

- [ ] **Step 1.6: Update `config.example.toml`**

Append to the end of `config.example.toml` (after the `# [search]` block):

```toml
# --- upgrade preflight --------------------------------------------------------

# Throughput rates used by `localmail estimate-upgrade` to project how long
# lock-heavy migrations (e.g. 0006) will hold against a populated `messages`
# table. Defaults assume SSD + modern Postgres. Halve for HDD / low-memory
# hosts; double for NVMe + lots of RAM. See docs/operations/upgrade-runbook.md.
# [upgrade]
# fts_v2_blowup_factor      = 1.5    # tsvector size vs. raw text
# gin_size_factor           = 0.4    # GIN index size vs. covered column
# table_rewrite_mb_per_sec  = 80.0   # ADD COLUMN GENERATED STORED throughput
# gin_build_mb_per_sec      = 30.0   # CREATE INDEX GIN throughput
```

- [ ] **Step 1.7: Verify full test suite still passes**

```bash
unset VIRTUAL_ENV && uv run pytest -q tests/ 2>&1 | tail -5
```

Expected: `<n> passed` (no failures). The new tests add 4; everything else unchanged.

- [ ] **Step 1.8: Commit**

```bash
git add src/localmail/config.py config.example.toml tests/test_upgrade_estimate_config.py
git commit -m "$(cat <<'EOF'
feat(config): add UpgradeEstimateConfig sub-model (#2)

Throughput rates + tsvector/GIN sizing factors used by the upcoming
`localmail estimate-upgrade` command to project lock-holding duration
for migration 0006 against a populated `messages` table.

All four constants are config-overridable; defaults assume SSD +
modern Postgres. No behaviour change yet — `estimate_0006` lands in
the next commit.

Refs #2.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Skeleton `upgrade_estimate.py` (dataclass + empty registry)

**Files:**
- Create: `src/localmail/upgrade_estimate.py`
- Create: `tests/test_upgrade_estimate.py`

- [ ] **Step 2.1: Write failing test**

Create `tests/test_upgrade_estimate.py`:

```python
"""Unit tests for localmail.upgrade_estimate (issue #2)."""

from __future__ import annotations

import pytest

from localmail.upgrade_estimate import (
    ESTIMATORS,
    EstimateResult,
)


def test_estimate_result_is_immutable_dataclass():
    """EstimateResult is frozen — accidental mutation must fail."""
    r = EstimateResult(
        revision="0006_search_indexes",
        status="pending",
        current_bytes={},
        projected_bytes={"fts_v2": 100, "gin_messages": 40, "gin_chunks": 20},
        projected_duration_s=1.5,
        warnings=[],
    )
    with pytest.raises((AttributeError, Exception)):  # frozen dataclass raises FrozenInstanceError
        r.revision = "other"  # type: ignore[misc]


def test_estimate_result_fields_present():
    """All wire-stable fields exist on EstimateResult."""
    r = EstimateResult(
        revision="0006_search_indexes",
        status="applied",
        current_bytes={"fts_v2_idx": 1000, "chunks_fts_idx": 500},
        projected_bytes={},
        projected_duration_s=0.0,
        warnings=[],
    )
    assert r.revision == "0006_search_indexes"
    assert r.status == "applied"
    assert r.current_bytes == {"fts_v2_idx": 1000, "chunks_fts_idx": 500}
    assert r.projected_bytes == {}
    assert r.projected_duration_s == 0.0
    assert r.warnings == []


def test_estimators_registry_has_0006():
    """ESTIMATORS is a dict[str, Callable]; 0006 is registered."""
    assert "0006_search_indexes" in ESTIMATORS
    assert callable(ESTIMATORS["0006_search_indexes"])


def test_unknown_revision_raises_keyerror():
    """Documented contract: missing key raises KeyError (not silent miss)."""
    with pytest.raises(KeyError):
        ESTIMATORS["0099_nonsense"]
```

- [ ] **Step 2.2: Run tests to verify they fail**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_upgrade_estimate.py -v
```

Expected: 4 failures — `ModuleNotFoundError: No module named 'localmail.upgrade_estimate'`.

- [ ] **Step 2.3: Create skeleton module**

Create `src/localmail/upgrade_estimate.py`:

```python
"""Pre-flight estimator for lock-heavy schema migrations.

See:
  * docs/superpowers/specs/2026-05-27-large-archive-upgrade-estimator-design.md
  * docs/operations/upgrade-runbook.md
  * GitHub issue #2

The module exposes:
  * ``EstimateResult`` — frozen dataclass returned by every estimator.
  * ``ESTIMATORS`` — registry mapping revision name to estimator function.
  * ``estimate_0006`` — projects size + duration for migration 0006.

Adding an estimator for another migration:
  * Implement ``estimate_NNNN(conn, cfg, applied) -> EstimateResult``.
  * Register it in ``ESTIMATORS``.
  * Add a test class to ``tests/test_upgrade_estimate.py``.
  * Add a section to the runbook.

The module performs no IO except via the injected ``psycopg.Connection``.
This keeps it reusable from a future HTTP route or MCP tool without
refactor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Literal

import psycopg

from localmail.config import UpgradeEstimateConfig


@dataclass(frozen=True)
class EstimateResult:
    """One row of `localmail estimate-upgrade` output.

    ``current_bytes`` is populated when ``status == "applied"`` (with
    keys naming the indexes / columns whose sizes were read from
    ``pg_total_relation_size``). ``projected_bytes`` and
    ``projected_duration_s`` are populated when ``status == "pending"``
    or ``status == "not_applicable"``. The two are mutually exclusive
    by convention; consumers should only consult the dict that matches
    the status.
    """

    revision: str
    status: Literal["pending", "applied", "not_applicable"]
    current_bytes: dict[str, int]
    projected_bytes: dict[str, int]
    projected_duration_s: float
    warnings: list[str] = field(default_factory=list)


EstimatorFn = Callable[
    [psycopg.Connection, UpgradeEstimateConfig, bool], EstimateResult
]


# Populated below. Declared early so the type alias above can refer to the
# return type without a forward reference.
ESTIMATORS: dict[str, EstimatorFn] = {}


def estimate_0006(
    conn: psycopg.Connection,
    cfg: UpgradeEstimateConfig,
    applied: bool,
) -> EstimateResult:
    """Stub — fully implemented in Task 3 and Task 4."""
    raise NotImplementedError("estimate_0006 lands in Task 3")


ESTIMATORS["0006_search_indexes"] = estimate_0006
```

- [ ] **Step 2.4: Run tests to verify they pass**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_upgrade_estimate.py -v
```

Expected: 4 passed.

- [ ] **Step 2.5: Commit**

```bash
git add src/localmail/upgrade_estimate.py tests/test_upgrade_estimate.py
git commit -m "$(cat <<'EOF'
feat(upgrade_estimate): skeleton module + EstimateResult + registry (#2)

Pure module that future estimator functions plug into. estimate_0006
itself is a NotImplementedError stub here — landed in the next
commit. Skeleton ships first so the registry / wire-stable
EstimateResult shape can be reviewed independently of the projection
math.

Refs #2.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Implement `estimate_0006` — pending branch (projections)

**Files:**
- Modify: `src/localmail/upgrade_estimate.py` (implement projection branch)
- Modify: `tests/test_upgrade_estimate.py` (add pending-branch tests)

- [ ] **Step 3.1: Add seeding helper to test file**

Prepend to `tests/test_upgrade_estimate.py` (after the existing imports):

```python
from psycopg.types.json import Jsonb

from localmail.config import UpgradeEstimateConfig
from localmail.upgrade_estimate import estimate_0006


def _seed_account(conn) -> int:
    """Insert one account row, return its id. Required so message rows can FK."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO accounts (name, email_address, imap_host, auth_method)
            VALUES ('test', 'test@example.com', 'localhost', 'password')
            RETURNING id
            """,
        )
        row = cur.fetchone()
        assert row is not None
        return int(row[0])


def _seed_messages_with_known_text(
    conn, *, account_id: int, count: int, body_len: int
) -> None:
    """Insert ``count`` rows into ``messages`` with each body_text of length
    ``body_len`` and subject of length ``body_len // 10``. Knowing the
    text length lets the projection-math tests assert linearity.
    """
    subject_len = max(1, body_len // 10)
    subject = "s" * subject_len
    body = "b" * body_len
    with conn.cursor() as cur:
        for i in range(count):
            cur.execute(
                """
                INSERT INTO messages (
                    account_id, message_id, raw_sha256, headers,
                    subject, body_text, body_html, raw_bytes, size_bytes
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    account_id,
                    f"<m{i}@test>",
                    f"sha-{i}".encode(),
                    Jsonb({}),
                    subject,
                    body,
                    None,
                    b"",
                    0,
                ),
            )
    conn.commit()
```

- [ ] **Step 3.2: Write failing pending-branch tests**

Append to `tests/test_upgrade_estimate.py`:

```python
def test_estimate_0006_pending_empty_messages(db_conn):
    """No rows -> all projections are zero. No divide-by-zero anywhere."""
    cfg = UpgradeEstimateConfig()
    result = estimate_0006(db_conn, cfg, applied=False)
    assert result.revision == "0006_search_indexes"
    assert result.status == "pending"
    assert result.projected_bytes == {
        "fts_v2": 0,
        "gin_messages": 0,
        "gin_chunks": 0,
    }
    assert result.projected_duration_s == 0.0
    assert result.current_bytes == {}


def test_estimate_0006_pending_with_seeded_rows(db_conn):
    """Projection scales with rows × text length × blowup factor."""
    account_id = _seed_account(db_conn)
    rows = 100
    body_len = 200
    _seed_messages_with_known_text(
        db_conn, account_id=account_id, count=rows, body_len=body_len
    )
    cfg = UpgradeEstimateConfig()  # defaults

    result = estimate_0006(db_conn, cfg, applied=False)

    assert result.status == "pending"
    # avg(length(subject) + length(body_text) + length(body_html))
    # body_html is NULL -> coalesce('') -> 0
    # subject_len = body_len // 10 = 20 (per helper)
    avg_text_len_expected = body_len + (body_len // 10)
    projected_fts_v2_expected = rows * avg_text_len_expected * cfg.fts_v2_blowup_factor
    # ±10% absorbs avg() returning a Decimal with rounding.
    assert result.projected_bytes["fts_v2"] == pytest.approx(
        projected_fts_v2_expected, rel=0.1
    )
    projected_gin_expected = projected_fts_v2_expected * cfg.gin_size_factor
    assert result.projected_bytes["gin_messages"] == pytest.approx(
        projected_gin_expected, rel=0.1
    )
    # Duration is non-zero and positive.
    assert result.projected_duration_s > 0.0


def test_estimate_0006_pending_duration_uses_config_rates(db_conn):
    """Slower throughput rate -> proportionally longer projected duration."""
    account_id = _seed_account(db_conn)
    _seed_messages_with_known_text(
        db_conn, account_id=account_id, count=100, body_len=500
    )
    cfg_fast = UpgradeEstimateConfig(table_rewrite_mb_per_sec=1000.0)
    cfg_slow = UpgradeEstimateConfig(table_rewrite_mb_per_sec=10.0)

    r_fast = estimate_0006(db_conn, cfg_fast, applied=False)
    r_slow = estimate_0006(db_conn, cfg_slow, applied=False)

    # The GIN-build component uses gin_build_mb_per_sec (untouched here)
    # so the ratio isn't exactly 100x; assert directional + monotonic.
    assert r_slow.projected_duration_s > r_fast.projected_duration_s
    # The table-rewrite component scales linearly with 1/rate, so the
    # delta is bounded below by (rewrite_fast_term - rewrite_slow_term).
    # Sanity check: slow duration is at least 10x fast.
    assert r_slow.projected_duration_s >= 10 * r_fast.projected_duration_s
```

- [ ] **Step 3.3: Run tests to verify they fail**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_upgrade_estimate.py -v
```

Expected: 3 failures with `NotImplementedError: estimate_0006 lands in Task 3`. Other 4 tests (from Task 2) still pass.

- [ ] **Step 3.4: Implement pending branch in `src/localmail/upgrade_estimate.py`**

Replace the `estimate_0006` stub with:

```python
_MIB = 1024 * 1024  # named constant; no magic-number hidden in arithmetic


def _table_exists(conn: psycopg.Connection, relname: str) -> bool:
    """Cheap catalog lookup. to_regclass() returns NULL (not exception)
    for a missing relation."""
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s)", (relname,))
        row = cur.fetchone()
        assert row is not None
        return row[0] is not None


def estimate_0006(
    conn: psycopg.Connection,
    cfg: UpgradeEstimateConfig,
    applied: bool,
) -> EstimateResult:
    """Project (or report actual) cost of migration 0006_search_indexes.

    Args:
        conn: open psycopg connection. Read-only; no transaction needed.
        cfg: throughput rates + sizing factors.
        applied: caller-supplied result of looking up the revision in
            ``schema_migrations``. Passed in (not queried internally)
            so unit tests can exercise both branches independently of
            fixture state.

    Returns:
        ``EstimateResult`` with ``status="pending"`` (projections),
        ``status="applied"`` (actual sizes), or ``status="not_applicable"``
        (messages table missing — `init-db` has never been run).
    """
    if not _table_exists(conn, "messages"):
        return EstimateResult(
            revision="0006_search_indexes",
            status="not_applicable",
            current_bytes={},
            projected_bytes={},
            projected_duration_s=0.0,
            warnings=["messages table not present — run `localmail init-db` first"],
        )
    if applied:
        return _estimate_0006_applied(conn, cfg)
    return _estimate_0006_pending(conn, cfg)


def _estimate_0006_pending(
    conn: psycopg.Connection,
    cfg: UpgradeEstimateConfig,
) -> EstimateResult:
    warnings: list[str] = []

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM messages")
        row = cur.fetchone()
        assert row is not None
        rows = int(row[0])

        if rows == 0:
            return EstimateResult(
                revision="0006_search_indexes",
                status="pending",
                current_bytes={},
                projected_bytes={"fts_v2": 0, "gin_messages": 0, "gin_chunks": 0},
                projected_duration_s=0.0,
                warnings=warnings,
            )

        cur.execute("SELECT pg_total_relation_size('messages')")
        row = cur.fetchone()
        assert row is not None
        current_table_bytes = int(row[0])

        cur.execute(
            """
            SELECT avg(
                length(coalesce(subject, ''))
                + length(coalesce(body_text, ''))
                + length(coalesce(body_html, ''))
            )
            FROM messages
            """
        )
        row = cur.fetchone()
        assert row is not None
        avg_text_len = float(row[0] or 0.0)

    projected_fts_v2 = int(rows * avg_text_len * cfg.fts_v2_blowup_factor)
    projected_gin_messages = int(projected_fts_v2 * cfg.gin_size_factor)
    # chunks GIN is independent of the messages fts column — we have no
    # signal here for sizing it accurately because message_chunks is
    # populated by the embed worker, not by the migration itself. Project
    # as zero with a warning so the output stays honest.
    projected_gin_chunks = 0
    warnings.append(
        "message_chunks GIN size cannot be projected before chunks exist; "
        "rerun after the embed worker has populated chunks for an accurate "
        "estimate."
    )

    rewrite_duration = (
        current_table_bytes + projected_fts_v2
    ) / (cfg.table_rewrite_mb_per_sec * _MIB)
    gin_duration = projected_gin_messages / (cfg.gin_build_mb_per_sec * _MIB)
    projected_duration_s = rewrite_duration + gin_duration

    return EstimateResult(
        revision="0006_search_indexes",
        status="pending",
        current_bytes={},
        projected_bytes={
            "fts_v2": projected_fts_v2,
            "gin_messages": projected_gin_messages,
            "gin_chunks": projected_gin_chunks,
        },
        projected_duration_s=projected_duration_s,
        warnings=warnings,
    )


def _estimate_0006_applied(
    conn: psycopg.Connection,
    cfg: UpgradeEstimateConfig,
) -> EstimateResult:
    """Stub — implemented in Task 4."""
    raise NotImplementedError("_estimate_0006_applied lands in Task 4")
```

- [ ] **Step 3.5: Update the `test_estimate_0006_pending_with_seeded_rows` expected check**

The test as written in Step 3.2 doesn't account for the `gin_chunks=0` projection (vs `projected_gin_expected`). Verify by re-reading the test: it only asserts `projected_bytes["gin_messages"]`, not `"gin_chunks"`. ✓ No change needed.

- [ ] **Step 3.6: Run tests to verify they pass**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_upgrade_estimate.py -v
```

Expected: 7 passed (4 from Task 2 + 3 new). Pending branch tests now green.

- [ ] **Step 3.7: Run full suite to confirm no regressions**

```bash
unset VIRTUAL_ENV && uv run pytest -q tests/ 2>&1 | tail -5
```

Expected: all green.

- [ ] **Step 3.8: Commit**

```bash
git add src/localmail/upgrade_estimate.py tests/test_upgrade_estimate.py
git commit -m "$(cat <<'EOF'
feat(upgrade_estimate): pending-branch projections for 0006 (#2)

estimate_0006 projects fts_v2 column footprint and the messages GIN
index size from current row count + avg text length + the configured
blowup / size factors. Duration projection sums the table-rewrite
component (driven by table_rewrite_mb_per_sec) and the GIN-build
component (driven by gin_build_mb_per_sec).

The message_chunks GIN cannot be sized before chunks exist, so its
projection is 0 with a warning instructing the operator to re-run
after the embed worker.

Refs #2.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Implement `_estimate_0006_applied` (read actual sizes)

**Files:**
- Modify: `src/localmail/upgrade_estimate.py` (implement applied branch)
- Modify: `tests/test_upgrade_estimate.py` (add applied-branch test)

- [ ] **Step 4.1: Write failing test**

Append to `tests/test_upgrade_estimate.py`:

```python
def test_estimate_0006_applied_reports_actual_sizes(db_conn):
    """With 0006 in the fixture state, applied=True reads pg_total_relation_size."""
    account_id = _seed_account(db_conn)
    _seed_messages_with_known_text(
        db_conn, account_id=account_id, count=20, body_len=500
    )
    cfg = UpgradeEstimateConfig()

    result = estimate_0006(db_conn, cfg, applied=True)

    assert result.status == "applied"
    assert result.projected_bytes == {}
    assert result.projected_duration_s == 0.0
    # Both GIN indexes exist post-fixture and report a non-zero size.
    assert result.current_bytes["messages_fts_v2_idx"] > 0
    assert result.current_bytes["message_chunks_fts_idx"] >= 0
    # message_chunks_fts_idx is on an empty table here, so its
    # pg_total_relation_size will be small but should still be returned.
    assert "message_chunks_fts_idx" in result.current_bytes
```

- [ ] **Step 4.2: Run test to verify it fails**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_upgrade_estimate.py::test_estimate_0006_applied_reports_actual_sizes -v
```

Expected: `NotImplementedError: _estimate_0006_applied lands in Task 4`.

- [ ] **Step 4.3: Implement applied branch**

Replace the `_estimate_0006_applied` stub in `src/localmail/upgrade_estimate.py`:

```python
_INDEX_NAMES_0006 = (
    "messages_fts_v2_idx",
    "message_chunks_fts_idx",
)


def _estimate_0006_applied(
    conn: psycopg.Connection,
    cfg: UpgradeEstimateConfig,
) -> EstimateResult:
    """Report actual sizes for migration 0006's two GIN indexes.

    If a named index is missing (operator manually dropped it), a
    warning is appended and that index's size key is omitted from
    ``current_bytes``. The call never raises for missing-index.
    """
    current_bytes: dict[str, int] = {}
    warnings: list[str] = []

    for idx_name in _INDEX_NAMES_0006:
        size = _safe_relation_size(conn, idx_name)
        if size is None:
            warnings.append(
                f"index {idx_name} missing despite migration applied"
            )
            continue
        current_bytes[idx_name] = size

    return EstimateResult(
        revision="0006_search_indexes",
        status="applied",
        current_bytes=current_bytes,
        projected_bytes={},
        projected_duration_s=0.0,
        warnings=warnings,
    )


def _safe_relation_size(conn: psycopg.Connection, relname: str) -> int | None:
    """Return pg_total_relation_size(relname) in bytes, or None if missing.

    Uses to_regclass() so a missing relation surfaces as NULL rather
    than throwing UndefinedTable. Reads only — no lock beyond
    AccessShareLock on pg_class.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_total_relation_size(to_regclass(%s))",
            (relname,),
        )
        row = cur.fetchone()
        assert row is not None
        if row[0] is None:
            return None
        return int(row[0])
```

Note: `to_regclass()` returns NULL for a missing relation (no exception), which is the canonical Postgres pattern for safe existence-checks against the catalog. This is preferred over catching `UndefinedTable` because we never enter the error path in the first place.

- [ ] **Step 4.4: Run test to verify it passes**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_upgrade_estimate.py -v
```

Expected: 8 passed (7 from prior + 1 new).

- [ ] **Step 4.5: Commit**

```bash
git add src/localmail/upgrade_estimate.py tests/test_upgrade_estimate.py
git commit -m "$(cat <<'EOF'
feat(upgrade_estimate): applied-branch reads actual sizes (#2)

When called with applied=True, estimate_0006 reads
pg_total_relation_size for messages_fts_v2_idx and
message_chunks_fts_idx via to_regclass(), which returns NULL for
a missing relation (no exception path needed).

Refs #2.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Warnings for applied-with-missing-index

**Files:**
- Modify: `tests/test_upgrade_estimate.py` (add missing-index test)

The implementation already covers this case (Task 4's `to_regclass()` + `None` check + warning append). This task adds the regression test to prove it.

- [ ] **Step 5.1: Write failing test**

Append to `tests/test_upgrade_estimate.py`:

```python
def test_estimate_0006_applied_with_index_missing_emits_warning(db_conn):
    """Drop the messages GIN inside a savepoint; estimator must report it."""
    cfg = UpgradeEstimateConfig()

    with db_conn.cursor() as cur:
        cur.execute("SAVEPOINT before_drop_idx")
        cur.execute("DROP INDEX messages_fts_v2_idx")

        try:
            result = estimate_0006(db_conn, cfg, applied=True)
        finally:
            cur.execute("ROLLBACK TO SAVEPOINT before_drop_idx")

    assert result.status == "applied"
    assert "messages_fts_v2_idx" not in result.current_bytes
    assert any(
        "messages_fts_v2_idx missing" in w for w in result.warnings
    ), f"expected missing-index warning in {result.warnings!r}"
    # The chunks GIN still exists, so it should still report a size.
    assert "message_chunks_fts_idx" in result.current_bytes
```

- [ ] **Step 5.2: Run test to verify it passes**

The applied branch from Task 4 already handles this case. The test exists to pin the behaviour as a regression test.

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_upgrade_estimate.py::test_estimate_0006_applied_with_index_missing_emits_warning -v
```

Expected: PASS on first run (no implementation change needed).

If the test fails — re-check Task 4 step 4.3: the `_safe_relation_size` helper must return None for the dropped index, and the loop must continue past it.

- [ ] **Step 5.3: Run full module test file to confirm no fixture pollution**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_upgrade_estimate.py -v
```

Expected: 9 passed. The savepoint rollback in the test must restore the index for the next test that runs against the same fixture.

- [ ] **Step 5.4: Commit**

```bash
git add tests/test_upgrade_estimate.py
git commit -m "$(cat <<'EOF'
test(upgrade_estimate): pin missing-index warning behaviour (#2)

Regression test for the applied-but-index-dropped path: the
estimator must surface a warning naming the missing index and omit
its size key from current_bytes, without raising.

Refs #2.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: CLI command `estimate-upgrade`

**Files:**
- Modify: `src/localmail/cli.py` (add `estimate-upgrade` command)
- Create: `tests/test_cli_estimate_upgrade.py`

- [ ] **Step 6.1: Write failing tests**

Create `tests/test_cli_estimate_upgrade.py`:

```python
"""Integration tests for `localmail estimate-upgrade` (issue #2)."""

from __future__ import annotations

import json

from click.testing import CliRunner

from localmail.cli import main


def test_cli_estimate_upgrade_human_output(cli_config, db_conn):
    """Default text output is human-readable and contains the revision
    name, status, and a duration string."""
    runner = CliRunner()
    result = runner.invoke(main, ["estimate-upgrade"])
    assert result.exit_code == 0, result.output
    assert "0006_search_indexes" in result.output
    # Either "applied" (fixture state) or "pending" (clean DB) is fine.
    assert ("applied" in result.output) or ("pending" in result.output)


def test_cli_estimate_upgrade_json_output(cli_config, db_conn):
    """--format json emits a parseable list with all wire fields present."""
    runner = CliRunner()
    result = runner.invoke(main, ["estimate-upgrade", "--format", "json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert isinstance(payload, list)
    assert len(payload) >= 1
    row = payload[0]
    for key in (
        "revision",
        "status",
        "current_bytes",
        "projected_bytes",
        "projected_duration_s",
        "warnings",
    ):
        assert key in row, f"missing key {key!r} in {row!r}"
    assert row["revision"] == "0006_search_indexes"


def test_cli_estimate_upgrade_db_unreachable(monkeypatch, tmp_path):
    """Bad DSN -> non-zero exit + clear error on stderr."""
    stub = tmp_path / "config.toml"
    stub.write_text('[database]\ndsn = "postgresql://unreachable:1/no_such_db"\n')
    monkeypatch.setenv("LOCALMAIL_CONFIG", str(stub))

    runner = CliRunner()
    result = runner.invoke(main, ["estimate-upgrade"])
    assert result.exit_code != 0
    # Don't pin the exact wording — psycopg's connection-error string
    # varies by platform — but the keyword 'connect' should appear.
    assert "connect" in result.output.lower() or "could not" in result.output.lower()
```

- [ ] **Step 6.2: Run tests to verify they fail**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_cli_estimate_upgrade.py -v
```

Expected: failures — `click.exceptions.UsageError: No such command 'estimate-upgrade'`.

- [ ] **Step 6.3: Add CLI command to `src/localmail/cli.py`**

Add the following import at the top, alongside the existing `from .config import ...` line:

```python
from .upgrade_estimate import ESTIMATORS, EstimateResult
```

Then add the command. The natural location is alongside `search-status` (line ~622) and the other status commands. Insert after the `search-status` block:

```python
def _applied_revisions(conn: psycopg.Connection) -> set[str]:
    """Return revisions from schema_migrations as a set.

    Returns the empty set if schema_migrations doesn't exist yet
    (treats everything as pending — same convention as
    db.pending_migrations).
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_name = 'schema_migrations'"
        )
        if cur.fetchone() is None:
            return set()
        cur.execute("SELECT revision FROM schema_migrations")
        return {row[0] for row in cur.fetchall()}


def _format_estimate_text(results: list[EstimateResult]) -> str:
    """Render EstimateResult list as a human-readable table."""
    lines = []
    for r in results:
        lines.append(f"revision: {r.revision}")
        lines.append(f"  status: {r.status}")
        if r.current_bytes:
            for k, v in r.current_bytes.items():
                lines.append(f"  {k}: {v:>15,} bytes ({v / (1024*1024):.1f} MiB)")
        if r.projected_bytes:
            for k, v in r.projected_bytes.items():
                lines.append(f"  {k} (projected): {v:>15,} bytes ({v / (1024*1024):.1f} MiB)")
        if r.projected_duration_s > 0:
            mins, secs = divmod(int(r.projected_duration_s), 60)
            lines.append(f"  projected lock duration: ~{mins}m {secs}s")
        for w in r.warnings:
            lines.append(f"  WARNING: {w}")
        lines.append("")  # blank line between revisions
    return "\n".join(lines).rstrip()


def _estimate_to_json(r: EstimateResult) -> dict:
    """Project an EstimateResult to a JSON-serialisable dict."""
    return {
        "revision": r.revision,
        "status": r.status,
        "current_bytes": r.current_bytes,
        "projected_bytes": r.projected_bytes,
        "projected_duration_s": r.projected_duration_s,
        "warnings": r.warnings,
    }


@main.command("estimate-upgrade")
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["text", "json"]),
    default="text",
    help="Output format. text (default) is human-readable; json emits a list.",
)
def estimate_upgrade(fmt: str) -> None:
    """Pre-flight estimator for lock-heavy schema migrations.

    Reports projected (or actual) size + duration for migrations that
    hold long locks against a populated `messages` table. Read-only;
    safe to run against a live archive. See
    docs/operations/upgrade-runbook.md for the full procedure.
    """
    dsn = _dsn()
    with psycopg.connect(dsn) as conn:
        applied = _applied_revisions(conn)
        cfg = load_config().upgrade
        results = [
            fn(conn, cfg, applied=(rev in applied))
            for rev, fn in ESTIMATORS.items()
        ]

    if fmt == "json":
        click.echo(_json.dumps([_estimate_to_json(r) for r in results]))
    else:
        click.echo(_format_estimate_text(results))
```

- [ ] **Step 6.4: Run tests to verify they pass**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_cli_estimate_upgrade.py -v
```

Expected: 3 passed.

- [ ] **Step 6.5: Smoke-test the CLI manually**

```bash
unset VIRTUAL_ENV && uv run localmail --help | grep estimate-upgrade
```

Expected: a line `  estimate-upgrade  Pre-flight estimator for lock-heavy schema...`.

If you have a working `~/.config/localmail/config.toml`, also try:

```bash
unset VIRTUAL_ENV && uv run localmail estimate-upgrade --format json | python -m json.tool | head -20
```

Expected: well-formed JSON (or a clear connection error if no Postgres is running).

- [ ] **Step 6.6: Run full suite to confirm no regressions**

```bash
unset VIRTUAL_ENV && uv run pytest -q tests/ 2>&1 | tail -5
```

Expected: all green; new test count = 16 total (4 config + 4 module skel + 3 module pending + 1 module applied + 1 module missing-index + 3 CLI). Suite should be at ~825 passing (was 809).

- [ ] **Step 6.7: Commit**

```bash
git add src/localmail/cli.py tests/test_cli_estimate_upgrade.py
git commit -m "$(cat <<'EOF'
feat(cli): add `localmail estimate-upgrade` command (#2)

Pre-flight CLI that iterates the ESTIMATORS registry and prints
projected (status=pending) or actual (status=applied) sizes +
duration for each registered estimator. --format text|json matches
the convention of search-status and the other status commands.

Refs #2.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Operator runbook

**Files:**
- Create: `docs/operations/upgrade-runbook.md`

This task creates a markdown document; no tests. Keep the prose specific and accurate — operators will copy commands out of it.

- [ ] **Step 7.1: Create `docs/operations/` directory and write the runbook**

Create `docs/operations/upgrade-runbook.md` with this exact content:

```markdown
# Large-archive upgrade runbook

When you run `localmail init-db` for the first time against a populated
`messages` table — say you imported from another archive, or you're
adopting localmail against an existing Postgres database — some
migrations hold long locks that block all writes for the duration of
the build. This runbook tells you what to expect, how to estimate the
cost ahead of time, and how to mitigate.

Fresh installs (empty tables) are unaffected. The migrations all
complete in seconds.

## When to read this

Read this before running `localmail init-db` if **any** of these apply:

- You imported an existing IMAP archive into a Postgres `messages`
  table outside of localmail.
- You're restoring from a `pg_dump` taken before a localmail release
  that introduced new lock-heavy migrations.
- You're running localmail in production with synchronous writers
  that must not stall (e.g. an indexer that polls the daemon).
- Your archive has more than a few hundred thousand `messages` rows.

If none of these apply, just run `localmail init-db`. It's fine.

## The lock-heavy migrations

| Revision | Holds lock for | What it does |
|---|---|---|
| `0006_search_indexes` | Minutes to hours, depending on row count | Adds `messages.fts_v2` (`tsvector` stored generated column) and two GIN indexes. Two sources of lock: (1) `ADD COLUMN ... GENERATED ALWAYS AS ... STORED` rewrites the whole heap under `ACCESS EXCLUSIVE`; (2) `CREATE INDEX ... USING GIN` (no `CONCURRENTLY`) holds `ShareLock` for the build. |
| `0015_messages_body_lang` | Seconds to a minute | Adds `messages.body_lang` (nullable, no default — metadata-only, no rewrite) plus a partial btree index. Small. |
| `0018_messages_date_received_internaldate` | Seconds to minutes | Adds `messages.internal_date` (nullable, no default — no rewrite) plus the `messages_recent_idx` btree expression index. Build time scales with row count but is much cheaper than the GIN builds. |

Only `0006_search_indexes` is dangerous at scale. Estimators for the
others can be added in a follow-up if any operator reports needing
them.

## Pre-flight: `localmail estimate-upgrade`

Run this against your live archive **before** running `init-db`:

```bash
unset VIRTUAL_ENV && uv run localmail estimate-upgrade
```

Example output for a 500k-row archive on SSD:

```
revision: 0006_search_indexes
  status: pending
  fts_v2 (projected):     1,245,000,000 bytes (1187.5 MiB)
  gin_messages (projected):  498,000,000 bytes (475.0 MiB)
  gin_chunks (projected):              0 bytes (0.0 MiB)
  projected lock duration: ~6m 12s
  WARNING: message_chunks GIN size cannot be projected before chunks exist;
    rerun after the embed worker has populated chunks for an accurate estimate.
```

How to read each line:

- **`fts_v2 (projected)`** — additional storage the new column will
  consume on disk. Roughly 1.5× the concatenated `subject + body_text
  + body_html` text length per row.
- **`gin_messages (projected)`** — additional storage for the GIN
  index over `fts_v2`. Typically 40% of the column size.
- **`gin_chunks (projected): 0 bytes`** — `message_chunks` is empty
  until the embed worker runs. The estimator can't size this index
  pre-fact. Re-run the estimator after the embed worker has been
  running for a bit if you want an accurate post-0006 picture.
- **`projected lock duration`** — sum of the table-rewrite duration
  (driven by `table_rewrite_mb_per_sec` in config) and the
  GIN-build duration (driven by `gin_build_mb_per_sec`). These are
  rough; see "Calibration" below.

JSON output for scripting:

```bash
unset VIRTUAL_ENV && uv run localmail estimate-upgrade --format json
```

Output: a list of objects, one per registered estimator. Stable
schema: `revision`, `status`, `current_bytes`, `projected_bytes`,
`projected_duration_s`, `warnings`. Empty dicts/lists for
not-applicable branches.

## Recommended procedure for 0006 at scale

Pick one of these based on your tolerance for write downtime:

### Option A: schedule downtime (simplest, recommended for most operators)

1. Run `localmail estimate-upgrade` to get a duration estimate.
2. Schedule a maintenance window of `2 × estimated_duration` (the 2×
   absorbs ETA error and gives you room to investigate if something
   goes wrong).
3. Stop the daemon, any cron jobs, and any external writers.
4. `pg_dump` the database (always — see "Disk-space planning" below
   for the size impact).
5. Run `unset VIRTUAL_ENV && uv run localmail init-db`. Tail the
   Postgres log if you want progress visibility — there's no localmail
   progress bar for migrations.
6. Run `localmail estimate-upgrade` again. The output should now show
   `status: applied` with `current_bytes` populated.
7. Restart the daemon.

### Option B: online column-rename procedure (advanced, requires Postgres ops chops)

You can avoid the `ACCESS EXCLUSIVE` lock by building a shadow
column with the same definition, backfilling it in batches under
short locks, creating the GIN index `CONCURRENTLY`, then renaming.
This is significantly more work and easy to get wrong; **if you
don't immediately know why each step below is needed, schedule
downtime (Option A) instead.**

The high-level shape:

1. Stop the daemon (writes would race the swap).
2. `ALTER TABLE messages ADD COLUMN fts_v2_new tsvector;`
3. Backfill in batches of ~10k rows, each in its own transaction.
4. `CREATE INDEX CONCURRENTLY messages_fts_v2_new_idx ON messages USING GIN (fts_v2_new);`
5. In a single transaction with `lock_timeout = '5s'`:
   - `DROP INDEX messages_fts_v2_idx;` (if it exists — won't on a
     fresh archive)
   - `ALTER TABLE messages DROP COLUMN fts_v2;` (if it exists)
   - `ALTER TABLE messages RENAME COLUMN fts_v2_new TO fts_v2;`
   - `ALTER INDEX messages_fts_v2_new_idx RENAME TO messages_fts_v2_idx;`
   - Add the same trigger/generated-column expression to keep
     `fts_v2` populated on subsequent inserts. (Note: a `STORED`
     generated column can't be added to an existing column without
     a rewrite — so this option ships a regular column populated
     by a `BEFORE INSERT` trigger instead. The migration's `IF NOT
     EXISTS` clauses on `ADD COLUMN` and `CREATE INDEX` will then
     no-op when `init-db` is finally run.)
6. Mark `0006_search_indexes` as applied:
   `INSERT INTO schema_migrations (revision) VALUES ('0006_search_indexes');`
7. Restart the daemon.

This procedure has real trade-offs (extra storage during the swap,
brief `AccessExclusiveLock` on the rename, a trigger-vs-generated
behavioural difference for new rows). Test it on a clone of your
database first. If you can't articulate why each step is here,
Option A is the right call.

## Disk-space planning

Migration 0006 needs **roughly 2× the current `messages` table
size in free disk** during the run:

- The table rewrite produces a new heap before swapping; the old
  heap is reclaimed by autovacuum after the migration commits.
- The GIN indexes also need to be built before they're swapped in.
- `pg_dump` (always recommended pre-migration) adds another copy
  to wherever you write the dump.

Quick check:

```bash
psql -c "SELECT pg_size_pretty(pg_total_relation_size('messages')) AS messages_size;"
df -h $(psql -tA -c "SHOW data_directory;")
```

If the data directory has less than 3× the `messages_size` free,
free up space or move the dump elsewhere before starting.

## Calibration

The defaults assume SSD + modern Postgres on a reasonably-equipped
host:

- `table_rewrite_mb_per_sec = 80.0`
- `gin_build_mb_per_sec = 30.0`

If your hardware is slower (HDD, low-memory VM), halve them in
`config.toml`:

```toml
[upgrade]
table_rewrite_mb_per_sec = 40.0
gin_build_mb_per_sec = 15.0
```

To calibrate accurately, time a migration on a clone:

1. `pg_dump` your database; restore to a separate host.
2. Note the `messages` table size and current time.
3. Run `localmail init-db`. Time it.
4. Solve `time = (table_size_mb + fts_v2_mb) / rate_mb_per_sec` for
   `rate_mb_per_sec` and update your config.

This is overkill for most operators — the defaults are within ~30%
of reality on commodity SSD.

## Why this exists

Migration 0006 was shipped without `CONCURRENTLY` on the two GIN
indexes (the HNSW index in the same migration *is* concurrent).
Per CLAUDE.md, applied migrations can't be edited — so the fix is
not "rewrite 0006" but "give operators a tool to plan around it".
See [GitHub issue #2](https://github.com/hherb/localmail/issues/2)
and the design doc at
[docs/superpowers/specs/2026-05-27-large-archive-upgrade-estimator-design.md](../superpowers/specs/2026-05-27-large-archive-upgrade-estimator-design.md).
```

- [ ] **Step 7.2: Sanity-check the runbook renders correctly**

```bash
ls docs/operations/upgrade-runbook.md
wc -l docs/operations/upgrade-runbook.md
```

Expected: ~200-250 lines of markdown.

Optionally, render in a viewer (e.g. VS Code preview) to confirm the
tables and code blocks look right. No automated test for prose.

- [ ] **Step 7.3: Commit**

```bash
git add docs/operations/upgrade-runbook.md
git commit -m "$(cat <<'EOF'
docs: operator runbook for lock-heavy migrations (#2)

New docs/operations/ subdir for ops-class documentation (future
runbooks — backup, monitoring, capacity planning — fit alongside).

The runbook covers when to read it, what each lock-heavy migration
does, how to interpret estimate-upgrade output, two procedures
(scheduled downtime / online column-rename), and disk-space planning.

Refs #2.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: README + CLAUDE.md cross-references

**Files:**
- Modify: `README.md` (add paragraph near existing upgrade callout)
- Modify: `CLAUDE.md` (add line to Commands block)

- [ ] **Step 8.1: Add paragraph to README.md**

Open `README.md` and find the existing "Upgrading to migration 0016" callout (around line 96). After the blockquote ends (around line 103, after the line `for the full design.`), insert a new blockquote:

```markdown
> **Upgrading on a populated archive?** Before running `localmail
> init-db` against a large pre-existing `messages` table, read
> [docs/operations/upgrade-runbook.md](docs/operations/upgrade-runbook.md)
> and run `localmail estimate-upgrade` first. Migration 0006 holds an
> `ACCESS EXCLUSIVE` lock for the duration of an `ADD COLUMN ...
> GENERATED STORED` table rewrite, which can take minutes to hours
> on a multi-million-row archive.
```

- [ ] **Step 8.2: Add command line to CLAUDE.md**

Open `CLAUDE.md` and find the Commands block. Locate the line:

```
uv run localmail list-failed-extractions [--limit K]   # show blobs extraction skipped
```

After it, add:

```
uv run localmail estimate-upgrade [--format text|json]   # pre-flight size/duration for lock-heavy migrations
# see docs/operations/upgrade-runbook.md
```

- [ ] **Step 8.3: Verify no broken cross-references**

```bash
grep -n "operations/upgrade-runbook" README.md CLAUDE.md
ls docs/operations/upgrade-runbook.md
```

Expected: README and CLAUDE.md both reference the runbook; the file exists.

- [ ] **Step 8.4: Commit**

```bash
git add README.md CLAUDE.md
git commit -m "$(cat <<'EOF'
docs: cross-reference upgrade runbook from README + CLAUDE.md (#2)

README gets a blockquote next to the migration-0016 callout
pointing at the new runbook. CLAUDE.md gets the
`localmail estimate-upgrade` command in the Commands block.

Refs #2.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: Final verification + PR

**Files:**
- (none modified — verification only)

- [ ] **Step 9.1: Run full test suite**

```bash
unset VIRTUAL_ENV && uv run pytest -q tests/ 2>&1 | tail -10
```

Expected: `<n> passed` with no failures. New test count is 9 module tests + 3 CLI tests + 4 config tests = 16 new tests. The suite was at 809 before this branch; expect ~825.

- [ ] **Step 9.2: Run mypy if enabled**

```bash
unset VIRTUAL_ENV && uv run mypy src/localmail/upgrade_estimate.py src/localmail/config.py src/localmail/cli.py 2>&1 | tail -10
```

Expected: no new errors. The `pyproject.toml`'s `[tool.mypy]` block is the source of truth — if there's no mypy invocation in the CI workflow, this step can be skipped, but it's cheap insurance against `assert row is not None` regressions.

- [ ] **Step 9.3: Verify no untracked production files**

```bash
git status
```

Expected: working tree clean (only `.claude/settings.local.json` untracked, by design).

- [ ] **Step 9.4: Check the commit log**

```bash
git log --oneline main..HEAD
```

Expected: 8-9 commits since `main`:

```
<sha>  docs: cross-reference upgrade runbook from README + CLAUDE.md (#2)
<sha>  docs: operator runbook for lock-heavy migrations (#2)
<sha>  feat(cli): add `localmail estimate-upgrade` command (#2)
<sha>  test(upgrade_estimate): pin missing-index warning behaviour (#2)
<sha>  feat(upgrade_estimate): applied-branch reads actual sizes (#2)
<sha>  feat(upgrade_estimate): pending-branch projections for 0006 (#2)
<sha>  feat(upgrade_estimate): skeleton module + EstimateResult + registry (#2)
<sha>  feat(config): add UpgradeEstimateConfig sub-model (#2)
<sha>  docs(spec): large-archive upgrade estimator + operator runbook (#2)
```

- [ ] **Step 9.5: Push branch and open PR**

```bash
git push -u origin issue-2-upgrade-estimator-runbook
```

```bash
gh pr create --title "feat: large-archive upgrade estimator + operator runbook (#2)" --body "$(cat <<'EOF'
## Summary

- Adds `localmail estimate-upgrade` — a read-only CLI that projects
  storage footprint and lock duration for lock-heavy migrations
  against a populated `messages` table.
- Adds `docs/operations/upgrade-runbook.md` covering when to use the
  estimator, what each lock-heavy migration does, and two procedures
  (scheduled downtime / online column-rename) for adopting migration
  0006 on a large pre-existing archive.
- Closes #2.

## Design + plan

- Spec: [docs/superpowers/specs/2026-05-27-large-archive-upgrade-estimator-design.md](docs/superpowers/specs/2026-05-27-large-archive-upgrade-estimator-design.md)
- Plan: [docs/superpowers/plans/2026-05-27-large-archive-upgrade-estimator.md](docs/superpowers/plans/2026-05-27-large-archive-upgrade-estimator.md)

## What's NOT in this PR (deliberately)

- No edit to migration 0006 — per CLAUDE.md it's immutable.
- No new migration that rebuilds the GIN indexes `CONCURRENTLY` —
  would degrade search reads on every existing install for an edge
  case most operators never hit.
- No estimators for migrations 0015 / 0018 — noted in the runbook
  but only 0006 ships an estimator implementation (YAGNI; framework
  supports adding them).

## Test plan

- [x] `unset VIRTUAL_ENV && uv run pytest -q tests/` green locally
- [x] New tests cover: empty messages, seeded-rows projection,
      config-rate-override, applied-status read, missing-index
      warning, CLI text/JSON output, CLI db-unreachable
- [x] `localmail estimate-upgrade --help` smoke-tested
- [x] `localmail estimate-upgrade --format json | python -m json.tool`
      produces valid JSON

Closes #2.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 9.6: Wait for CI**

After opening the PR, watch for `python-ci` to come back green:

```bash
gh pr checks --watch
```

Expected: `python-ci  pass  <duration>`.

If CI fails, read the run log, fix the root cause (don't `--no-verify`), re-push.

---

## Summary

After Task 9 lands:

- **Files added**: 4 (`upgrade_estimate.py`, two test files, runbook).
- **Files modified**: 5 (`config.py`, `cli.py`, `config.example.toml`,
  `README.md`, `CLAUDE.md`).
- **New tests**: 16 (9 module + 3 CLI + 4 config).
- **Lines of production code**: ~200 (`upgrade_estimate.py` ~150 +
  config changes ~30 + CLI changes ~30).
- **Lines of documentation**: ~250 (runbook ~200 + cross-references
  + spec + plan).
- **Issue closed**: #2.

Branch ready for review at
`issue-2-upgrade-estimator-runbook`. PR auto-closes #2 on merge via
`Closes #2` in the PR body.
