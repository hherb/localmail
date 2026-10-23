# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Parse a free-text-plus-operators search query into a typed shape.

Supported operators (all optional, in any order, anywhere in the query):
    from:STR / from:"STR"     to:STR / to:"STR"
    subject:STR               label:STR
    account:NAME              folder:STR / folder:"STR"
    after:YYYY-MM-DD          before:YYYY-MM-DD
    has:attachment / has:no-attachment

Anything not matched by an operator becomes free-text (joined with spaces,
preserved in encounter order).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime


class QueryParseError(ValueError):
    """Raised when an operator value can't be parsed (e.g. malformed date)."""


@dataclass(frozen=True)
class SearchFilters:
    account_names: list[str] = field(default_factory=list)
    accounts: list[int] | None = None  # resolved by Searcher from account_names
    account_ids: list[int] | None = None  # set directly from the API layer; bypasses name resolution
    folders: list[str] | None = None
    folder_ids: list[int] | None = None  # mailbox PKs from the API layer
    from_substr: str | None = None
    to_substr: str | None = None
    subject_substr: str | None = None
    after: date | None = None
    before: date | None = None
    has_attachment: bool | None = None
    label: str | None = None
    languages: list[str] | None = None


@dataclass(frozen=True)
class ParsedQuery:
    free_text: str
    rewritten_text: str | None = None
    expansion_terms: list[str] = field(default_factory=list)
    filters: SearchFilters = field(default_factory=SearchFilters)


_OPERATORS = {
    "from", "to", "subject", "after", "before", "has", "label",
    "account", "folder", "account_id", "folder_id", "lang",
}


def _tokenize(s: str) -> list[str]:
    """Whitespace-split, but keep quoted strings (single or double) intact.

    An unclosed quote runs to the end of the string: anything after an open
    quote is swallowed into its last token. A composer appending tokens after
    untrusted text must account for that (#367, and #374 for the CLI's).
    """
    out: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    for ch in s:
        if quote:
            if ch == quote:
                quote = None
            else:
                buf.append(ch)
        elif ch in ('"', "'"):
            quote = ch
        elif ch.isspace():
            if buf:
                out.append("".join(buf))
                buf = []
        else:
            buf.append(ch)
    if buf:
        out.append("".join(buf))
    return out


def _parse_date(value: str, field_name: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise QueryParseError(f"{field_name}: expected YYYY-MM-DD, got {value!r}") from exc


#: ``has:`` values and what each sets ``SearchFilters.has_attachment`` to.
_HAS_VALUES: dict[str, bool] = {"attachment": True, "no-attachment": False}


def _parse_has(value: str, current: bool | None) -> bool:
    """Resolve one ``has:`` token against any earlier one in the same query.

    An unrecognised value used to vanish from the query entirely — not a
    filter, and not free text either — so ``has:attachments`` searched the
    whole archive with a 200 (#364). A contradiction is refused rather than
    letting the last token win, which would be the same silence one step
    removed.
    """
    key = value.lower()
    if key not in _HAS_VALUES:
        expected = " or ".join(repr(v) for v in _HAS_VALUES)
        raise QueryParseError(f"has: expected {expected}, got {value!r}")
    wanted = _HAS_VALUES[key]
    if current is not None and current != wanted:
        raise QueryParseError(
            "has: 'attachment' and 'no-attachment' contradict each other"
        )
    return wanted


def _reject_nul(tok: str) -> None:
    """Refuse a token carrying U+0000, naming the operator it belongs to (#377).

    Postgres ``TEXT`` cannot hold a NUL, so one reaching a query parameter
    makes psycopg raise ``DataError`` — an unhandled 500 on HTTP, and an
    unmapped exception on MCP. Refused, not stripped with
    ``pgtext.strip_nuls``: a stripped query is a different question answered
    as if it were the one asked (#364). Every NUL lands in some token, since
    the tokenizer drops only whitespace and quote characters.
    """
    if "\x00" not in tok:
        return
    op = tok.partition(":")[0].lower() if ":" in tok else ""
    field_name = op if op in _OPERATORS else "query"
    raise QueryParseError(
        f"{field_name}: contains a NUL character (U+0000), which cannot be searched"
    )


def parse_query(query: str) -> ParsedQuery:
    """Decompose a query string into free text + structured filters.

    Raises ``QueryParseError`` for a malformed date, an empty ``lang:``, an
    unknown or contradictory ``has:``, and a NUL anywhere in the query.
    """
    free_parts: list[str] = []
    f_account_names: list[str] = []
    f_folders: list[str] = []
    f_account_ids: list[int] = []
    f_folder_ids: list[int] = []
    f_from = f_to = f_subject = f_label = None
    f_after = f_before = None
    f_has_attachment: bool | None = None
    f_languages: list[str] = []

    for tok in _tokenize(query):
        _reject_nul(tok)
        if ":" in tok:
            op, _, value = tok.partition(":")
            op_l = op.lower()
            if op_l == "lang":
                normalized = value.strip().lower()
                if not normalized:
                    raise QueryParseError("lang: empty value not allowed")
                f_languages.append(normalized)
                continue
            if op_l in _OPERATORS and value:
                if op_l == "from":
                    f_from = value
                elif op_l == "to":
                    f_to = value
                elif op_l == "subject":
                    f_subject = value
                elif op_l == "label":
                    f_label = value
                elif op_l == "account":
                    f_account_names.append(value)
                elif op_l == "folder":
                    f_folders.append(value)
                elif op_l == "account_id":
                    try:
                        f_account_ids.append(int(value))
                    except ValueError:
                        free_parts.append(tok)
                    continue
                elif op_l == "folder_id":
                    try:
                        f_folder_ids.append(int(value))
                    except ValueError:
                        free_parts.append(tok)
                    continue
                elif op_l == "after":
                    f_after = _parse_date(value, "after")
                elif op_l == "before":
                    f_before = _parse_date(value, "before")
                elif op_l == "has":
                    f_has_attachment = _parse_has(value, f_has_attachment)
                continue
        free_parts.append(tok)

    filters = SearchFilters(
        account_names=f_account_names,
        folders=f_folders or None,
        account_ids=f_account_ids or None,
        folder_ids=f_folder_ids or None,
        from_substr=f_from,
        to_substr=f_to,
        subject_substr=f_subject,
        after=f_after,
        before=f_before,
        has_attachment=f_has_attachment,
        label=f_label,
        languages=f_languages or None,
    )
    return ParsedQuery(free_text=" ".join(free_parts), filters=filters)
