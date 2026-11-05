"""The sort vocabulary has one authority, and both published schemas carry it (#330).

``SortMode`` and ``SortOrder`` live in ``search/sort_axes.py``. The HTTP
route and both MCP layers used to restate them as fresh ``Literal``s, so a
member added to either axis reached no transport: the wire would keep
refusing a value the Searcher accepts, with nothing failing.

Two pins, because each alone has a hole:

* a structural one — no ``Literal`` in ``src/`` outside ``sort_axes.py``
  shares two or more members with either vocabulary — which catches an
  identical restatement *and* one left stale after the authority grows, in
  any module, published or not (``mcp/tools.py::tool_search`` publishes no
  schema, so this is its only pin);
* a behavioural one — the published HTTP and MCP schemas carry exactly the
  authority's members — which catches a copy the structural rule cannot
  see (a split ``Literal["rank"] | Literal["date"]``, a plain ``str``), but
  cannot tell an import from an identical restatement.

Neither covers the GUI's TypeScript copies (``gui/src``), which cannot
import Python, nor the prose in ``mcp/server.py``'s field descriptions.
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
# Two shared members flags a near-copy (a stale one after the authority
# grows), while one alone stays legal: `Literal["date"]` names a single mode.
NEAR_COPY_OVERLAP = 2
_TYPING_MODULES = {"typing", "typing_extensions"}


def _literal_names(tree: ast.AST) -> set[str]:
    """``Literal`` plus every local alias of it (``from typing import Literal as L``)."""
    names = {"Literal"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in _TYPING_MODULES:
            names.update(a.asname for a in node.names
                         if a.name == "Literal" and a.asname)
    return names


def _literal_strings(node: ast.Subscript,
                     literal_names: set[str]) -> frozenset[str] | None:
    """The string members of a ``Literal[...]`` subscript, else ``None``."""
    name = node.value
    spelled = (name.id if isinstance(name, ast.Name)
               else name.attr if isinstance(name, ast.Attribute) else None)
    if spelled not in literal_names:
        return None
    elts = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
    return frozenset(e.value for e in elts
                     if isinstance(e, ast.Constant) and isinstance(e.value, str))


def restated_vocabularies(source: str) -> list[tuple[int, str]]:
    """(line, vocabulary) for each ``Literal`` spelling (most of) a sort vocabulary."""
    tree = ast.parse(source)
    literal_names = _literal_names(tree)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript):
            members = _literal_strings(node, literal_names)
            if not members:
                continue
            for vocab, expected in VOCABULARIES.items():
                if len(members & expected) >= min(NEAR_COPY_OVERLAP,
                                                  len(expected)):
                    found.append((node.lineno, vocab))
    return sorted(found)


def _spelled(vocab: str) -> str:
    return ", ".join(repr(m) for m in sorted(VOCABULARIES[vocab]))


def test_the_rule_sees_a_restatement():
    """Negative control: without it an AST walk that matches nothing passes.

    Built from the authority, so growing a vocabulary does not fail it.
    """
    src = (
        "from typing import Literal\n"
        "import typing\n"
        "from typing import Literal as L\n"
        f"a: Literal[{_spelled('SortMode')}] | None = None\n"
        f"b: typing.Literal[{_spelled('SortOrder')}]\n"
        f"c: L[{_spelled('SortMode')}]\n"
        "d: Literal['text', 'metadata']\n"
        "e: Literal['date']\n"
    )
    assert restated_vocabularies(src) == [
        (4, "SortMode"), (5, "SortOrder"), (6, "SortMode")]


def test_the_rule_sees_a_copy_left_stale_by_a_grown_authority(monkeypatch):
    """A grown authority must still flag the old two-member spelling."""
    monkeypatch.setitem(VOCABULARIES, "SortMode",
                        VOCABULARIES["SortMode"] | {"sender"})
    assert restated_vocabularies(
        "from typing import Literal\nx: Literal['rank', 'date']\n"
    ) == [(2, "SortMode")]


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
    """The non-null members of an ``X | None`` schema property.

    Reads ``const`` as well as ``enum``: a split ``Literal["a"] |
    Literal["b"]`` publishes one ``const`` branch per member.
    """
    members: set[str] = set()
    for b in prop.get("anyOf", [prop]):
        if "$ref" in b:
            b = defs[b["$ref"].rsplit("/", 1)[-1]]
        members.update(b.get("enum", []))
        if "const" in b:
            members.add(b["const"])
    return frozenset(members)


@pytest.mark.parametrize("field,vocab", [("sort", "SortMode"),
                                         ("sort_order", "SortOrder")])
def test_the_http_request_publishes_the_authority(field, vocab):
    schema = SearchRequest.model_json_schema()
    assert _enum_of(schema["properties"][field],
                    schema.get("$defs", {})) == VOCABULARIES[vocab]


@pytest.mark.parametrize("field,vocab", [("sort", "SortMode"),
                                         ("sort_order", "SortOrder")])
def test_the_mcp_tool_publishes_the_authority(field, vocab):
    pytest.importorskip("mcp")
    from localmail.config import McpConfig
    from localmail.mcp import build_mcp_server

    # Never opened: listing tools reads the schema, not the database.
    pool = ConnectionPool("dbname=unused", open=False)
    server = build_mcp_server(pool, searcher=None,
                              config=McpConfig(enabled=True))
    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    schema = tools["search"].inputSchema or {}
    assert _enum_of(schema["properties"][field],
                    schema.get("$defs", {})) == VOCABULARIES[vocab]
