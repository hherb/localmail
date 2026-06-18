# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Couple the MCP ``search`` filter *prose* to the SQL ``_filter_sql`` emits.

PR #167 encoded the actual filter operators into the agent-facing
``Field(description=…)`` of the MCP ``search`` tool:

  * ``date_from`` → inclusive  (``m.date_sent >= %s``)
  * ``date_to``   → exclusive  (``m.date_sent <  %s``)
  * ``from_addr`` / ``to`` / ``subject`` → case-insensitive substring
    (``ILIKE`` with a ``%value%`` parameter)

The presence tests in ``test_mcp_tool_descriptions.py`` only guard *that* each
parameter is documented, not *what* it claims. Nothing coupled the prose to the
SQL, so an operator change in ``search/arms.py`` (e.g. ``date_to`` becoming
inclusive, or substring becoming exact match) would silently leave the docstring
asserting a false contract to agents (#168).

These tests pin both halves to one shared spec table (``FILTER_SEMANTICS``):
the SQL side asserts ``_filter_sql`` emits the documented operator and *not* its
contrary; the prose side asserts the matching MCP parameter description still
carries the keyword. Drift in either half turns this red.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date

import pytest
from psycopg_pool import ConnectionPool

from localmail.config import McpConfig
from localmail.search.arms import _filter_sql
from localmail.search.query import SearchFilters

pytest.importorskip("mcp")  # the [mcp] extra (mcp SDK) gates the prose half

from localmail.mcp import build_mcp_server  # noqa: E402

_SAMPLE_DATE = date(2026, 1, 1)


@dataclass(frozen=True)
class FilterSemantic:
    """One filter's documented contract, shared by the SQL and prose checks.

    mcp_param:      parameter name on the MCP ``search`` tool.
    filters_kwarg:  the ``SearchFilters`` attribute the param maps to.
    sample:         a value that activates the filter in ``_filter_sql``.
    required_sql:   fragments that MUST appear in the emitted SQL.
    forbidden_sql:  fragments that MUST NOT appear (the contrary operator).
    required_params:fragments that MUST appear in the emitted parameter list
                    (proves substring wrapping for ILIKE filters).
    prose_keywords: keywords the MCP param description MUST contain.
    """

    mcp_param: str
    filters_kwarg: str
    sample: object
    required_sql: tuple[str, ...]
    forbidden_sql: tuple[str, ...]
    required_params: tuple[object, ...]
    prose_keywords: tuple[str, ...]


# The single source of truth tying agent-facing prose to SQL operators.
# The two date entries deliberately forbid the *other* comparison so flipping
# inclusivity (``<`` ↔ ``<=``, ``>=`` ↔ ``>``) is caught even though the
# operators share a prefix.
FILTER_SEMANTICS: tuple[FilterSemantic, ...] = (
    FilterSemantic(
        mcp_param="date_from",
        filters_kwarg="after",
        sample=_SAMPLE_DATE,
        required_sql=("m.date_sent >= %s",),
        forbidden_sql=("m.date_sent > %s",),
        required_params=(),
        prose_keywords=("inclusive",),
    ),
    FilterSemantic(
        mcp_param="date_to",
        filters_kwarg="before",
        sample=_SAMPLE_DATE,
        required_sql=("m.date_sent < %s",),
        forbidden_sql=("m.date_sent <= %s",),
        required_params=(),
        prose_keywords=("exclusive",),
    ),
    FilterSemantic(
        mcp_param="from_addr",
        filters_kwarg="from_substr",
        sample="alice",
        required_sql=("m.from_addr ILIKE %s", "m.from_name ILIKE %s"),
        forbidden_sql=(),
        required_params=("%alice%",),
        prose_keywords=("case-insensitive", "substring"),
    ),
    FilterSemantic(
        mcp_param="to",
        filters_kwarg="to_substr",
        sample="bob",
        required_sql=("unnest(m.to_addrs)", "ILIKE"),
        forbidden_sql=(),
        required_params=("%bob%",),
        prose_keywords=("case-insensitive", "substring"),
    ),
    FilterSemantic(
        mcp_param="subject",
        filters_kwarg="subject_substr",
        sample="invoice",
        required_sql=("m.subject ILIKE %s",),
        forbidden_sql=(),
        required_params=("%invoice%",),
        prose_keywords=("case-insensitive", "substring"),
    ),
)


@pytest.fixture
def search_param_descriptions() -> dict[str, str]:
    """Lower-cased agent-facing descriptions of every MCP ``search`` param.

    Pure schema introspection — ``list_tools()`` never acquires a connection,
    so the pool is built unopened with a placeholder DSN. This keeps the prose
    half independent of a reachable Postgres (it would otherwise skip via the
    ``db_dsn`` fixture for a reason wholly unrelated to what it asserts).
    """
    pool = ConnectionPool(
        "postgresql://placeholder/localmail", min_size=1, max_size=2, open=False)
    try:
        server = build_mcp_server(
            pool, searcher=None, config=McpConfig(enabled=True))
        tools = {t.name: t for t in asyncio.run(server.list_tools())}
    finally:
        pool.close()
    props = (tools["search"].inputSchema or {}).get("properties", {})
    return {
        name: (schema.get("description") or "").lower()
        for name, schema in props.items()
    }


@pytest.mark.parametrize("spec", FILTER_SEMANTICS, ids=lambda s: s.mcp_param)
def test_filter_sql_emits_documented_operator(spec: FilterSemantic) -> None:
    """``_filter_sql`` uses the operator the MCP prose promises — and not its
    contrary."""
    sql, params = _filter_sql(SearchFilters(**{spec.filters_kwarg: spec.sample}))
    for fragment in spec.required_sql:
        assert fragment in sql, (
            f"{spec.mcp_param}: expected {fragment!r} in emitted SQL: {sql!r}")
    for fragment in spec.forbidden_sql:
        assert fragment not in sql, (
            f"{spec.mcp_param}: contrary operator {fragment!r} leaked into "
            f"SQL — MCP prose would be stale: {sql!r}")
    for value in spec.required_params:
        assert value in params, (
            f"{spec.mcp_param}: expected param {value!r} (substring wrapping) "
            f"in {params!r}")


@pytest.mark.parametrize("spec", FILTER_SEMANTICS, ids=lambda s: s.mcp_param)
def test_mcp_description_states_documented_semantic(
    spec: FilterSemantic, search_param_descriptions: dict[str, str]
) -> None:
    """The MCP ``search`` param description still carries the keyword the SQL
    operator is coupled to."""
    desc = search_param_descriptions[spec.mcp_param]
    for keyword in spec.prose_keywords:
        assert keyword in desc, (
            f"search.{spec.mcp_param} description dropped {keyword!r}; "
            f"prose↔SQL coupling broken: {desc!r}")
