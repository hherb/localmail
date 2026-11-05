"""The sort vocabulary has one authority, and every transport publishes it (#330).

``SortMode`` and ``SortOrder`` live in ``search/sort_axes.py``. The HTTP
route and both MCP layers used to restate them as fresh ``Literal``s, so a
member added to either axis reached no transport: the wire would keep
refusing a value the Searcher accepts, with nothing failing.

Two pins, because each alone has a hole:

* a structural one — no ``Literal`` in ``src/`` outside ``sort_axes.py``
  spells either vocabulary — which catches a restatement *today*, but not a
  stale one after the authority grows (the sets then differ);
* a behavioural one — the published HTTP and MCP schemas carry exactly the
  authority's members — which catches that stale copy, but cannot tell an
  import from an identical restatement.
"""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from typing import Any, get_args

import pytest
from psycopg_pool import ConnectionPool

from localmail.search.sort_axes import SortMode, SortOrder
from localmail.serve.routes.search import SearchRequest

SRC = Path(__file__).resolve().parents[1] / "src" / "localmail"
AUTHORITY = SRC / "search" / "sort_axes.py"
VOCABULARIES = {
    "SortMode": frozenset(get_args(SortMode)),
    "SortOrder": frozenset(get_args(SortOrder)),
}


def _literal_strings(node: ast.Subscript) -> frozenset[str] | None:
    """The string members of a ``Literal[...]`` subscript, else ``None``."""
    name = node.value
    spelled = (name.id if isinstance(name, ast.Name)
               else name.attr if isinstance(name, ast.Attribute) else None)
    if spelled != "Literal":
        return None
    elts = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
    if not all(isinstance(e, ast.Constant) and isinstance(e.value, str)
               for e in elts):
        return None
    return frozenset(e.value for e in elts)  # type: ignore[attr-defined]


def restated_vocabularies(source: str) -> list[tuple[int, str]]:
    """(line, vocabulary) for each ``Literal`` spelling a sort vocabulary."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Subscript):
            members = _literal_strings(node)
            for vocab, expected in VOCABULARIES.items():
                if members == expected:
                    found.append((node.lineno, vocab))
    return sorted(found)


def test_the_rule_sees_a_restatement():
    """Negative control: without it an AST walk that matches nothing passes."""
    src = (
        "from typing import Literal\n"
        "import typing\n"
        "a: Literal['date', 'rank'] | None = None\n"
        "b: typing.Literal['asc', 'desc']\n"
        "c: Literal['text', 'metadata']\n"
    )
    assert restated_vocabularies(src) == [(3, "SortMode"), (4, "SortOrder")]


def test_no_module_restates_the_sort_vocabulary():
    offenders = [
        f"{path.relative_to(SRC.parent)}:{line}: restates {vocab}; "
        f"import it from localmail.search.sort_axes"
        for path in sorted(SRC.rglob("*.py")) if path != AUTHORITY
        for line, vocab in restated_vocabularies(path.read_text())
    ]
    assert offenders == []


def test_the_authority_is_still_where_the_rule_looks():
    """A moved authority would exempt nothing and flag its own new home."""
    assert {v for _, v in restated_vocabularies(AUTHORITY.read_text())} == {
        "SortMode", "SortOrder"}


def _enum_of(prop: dict[str, Any], defs: dict[str, Any]) -> frozenset[str]:
    """The non-null enum members of an ``X | None`` schema property."""
    branches = prop.get("anyOf", [prop])
    members: set[str] = set()
    for b in branches:
        if "$ref" in b:
            b = defs[b["$ref"].rsplit("/", 1)[-1]]
        members.update(b.get("enum", []))
    return frozenset(members)


@pytest.mark.parametrize("field,vocab", [("sort", "SortMode"),
                                         ("sort_order", "SortOrder")])
def test_the_http_request_publishes_the_authority(field, vocab):
    schema = SearchRequest.model_json_schema()
    assert _enum_of(schema["properties"][field],
                    schema.get("$defs", {})) == VOCABULARIES[vocab]


@pytest.mark.parametrize("field,vocab", [("sort", "SortMode"),
                                         ("sort_order", "SortOrder")])
def test_the_mcp_tool_publishes_the_authority(db_dsn, field, vocab):
    pytest.importorskip("mcp")
    from localmail.config import McpConfig
    from localmail.mcp import build_mcp_server

    pool = ConnectionPool(db_dsn, min_size=1, max_size=2, open=True)
    try:
        server = build_mcp_server(pool, searcher=None,
                                  config=McpConfig(enabled=True))
        tools = {t.name: t for t in asyncio.run(server.list_tools())}
    finally:
        pool.close()
    schema = tools["search"].inputSchema or {}
    assert _enum_of(schema["properties"][field],
                    schema.get("$defs", {})) == VOCABULARIES[vocab]
