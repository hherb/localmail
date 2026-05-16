"""Parse a free-text-plus-operators search query into a typed shape.

Supported operators (all optional, in any order, anywhere in the query):
    from:STR / from:"STR"     to:STR / to:"STR"
    subject:STR               label:STR
    account:NAME              folder:STR / folder:"STR"
    after:YYYY-MM-DD          before:YYYY-MM-DD
    has:attachment

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
    folders: list[str] | None = None
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


_OPERATORS = {"from", "to", "subject", "after", "before", "has", "label", "account", "folder"}


def _tokenize(s: str) -> list[str]:
    """Whitespace-split, but keep quoted strings (single or double) intact."""
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


def parse_query(query: str) -> ParsedQuery:
    """Decompose a query string into free text + structured filters."""
    free_parts: list[str] = []
    f_account_names: list[str] = []
    f_folders: list[str] = []
    f_from = f_to = f_subject = f_label = None
    f_after = f_before = None
    f_has_attachment: bool | None = None

    for tok in _tokenize(query):
        if ":" in tok:
            op, _, value = tok.partition(":")
            op_l = op.lower()
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
                elif op_l == "after":
                    f_after = _parse_date(value, "after")
                elif op_l == "before":
                    f_before = _parse_date(value, "before")
                elif op_l == "has":
                    if value.lower() == "attachment":
                        f_has_attachment = True
                continue
        free_parts.append(tok)

    filters = SearchFilters(
        account_names=f_account_names,
        folders=f_folders or None,
        from_substr=f_from,
        to_substr=f_to,
        subject_substr=f_subject,
        after=f_after,
        before=f_before,
        has_attachment=f_has_attachment,
        label=f_label,
    )
    return ParsedQuery(free_text=" ".join(free_parts), filters=filters)
