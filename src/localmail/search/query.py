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
preserved in encounter order). The free text keeps web-search syntax for the
lexical arms (#419): ``"quoted phrase"``, ``OR`` (any case), ``-excluded``.
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

    @property
    def semantic_text(self) -> str:
        return semantic_text(self.free_text)

    @property
    def excluded_text(self) -> str:
        return excluded_text(self.free_text)


def _is_or(raw: str) -> bool:
    # websearch_to_tsquery reads `or` in any case as the operator.
    return raw.lower() == "or"


def _is_exclusion(raw: str) -> bool:
    # `--foo` is a double negation to websearch_to_tsquery, i.e. requires foo.
    return raw.startswith("-") and not raw.startswith("--")


def semantic_text(free_text: str) -> str:
    """``free_text`` without its web-search syntax, for the embedding,
    reranker, snippet and rankability: quotes dropped, ``OR`` (any case) and
    ``-excluded`` words removed — embedding an excluded word would pull
    toward it (#419). Empty means there is no positive term to rank against.
    """
    return " ".join(
        text for text, raw in _tokenize(free_text)
        if text and not _is_or(raw) and not _is_exclusion(raw)
    )


def excluded_text(free_text: str) -> str:
    """The ``-word`` / ``-"phrase"`` tokens of ``free_text``, as websearch
    syntax, for the message-level exclusion every retrieval path applies —
    the vector arms match no words, so a lexical ``!word`` alone excludes
    nothing from them (#419).

    An exclusion next to an ``OR`` is left out: ``a -b OR c`` is
    ``(a & !b) | c`` to Postgres, and excluding ``b`` outright would drop
    the ``c`` matches that contain it. So is one with no word in it (``-"``,
    ``-!``): its tsquery is empty, and ``@@`` an empty tsquery matches
    nothing, which would exclude every message.
    """
    tokens = _tokenize(free_text)
    raws = [raw for _, raw in tokens]
    return " ".join(
        raw for i, (text, raw) in enumerate(tokens)
        if _is_exclusion(raw) and any(c.isalnum() for c in text)
        and not (i > 0 and _is_or(raws[i - 1]))
        and not (i + 1 < len(raws) and _is_or(raws[i + 1]))
    )


_OPERATORS = {
    "from", "to", "subject", "after", "before", "has", "label",
    "account", "folder", "account_id", "folder_id", "lang",
}


def _tokenize(s: str) -> list[tuple[str, str]]:
    """Whitespace-split, but keep quoted strings (single or double) intact.

    Each token comes back twice: with its quote characters stripped (what an
    operator value is read from), and with its ``"`` delimiters kept (what
    free text is built from, so a phrase reaches ``websearch_to_tsquery``
    as one, #419). Single quotes are stripped from both and never turned
    into ``"``: Postgres ignores them, and converting would make ``it's
    late`` a phrase.

    An unclosed quote runs to the end of the string: anything after an open
    quote is swallowed into its last token. A composer appending tokens after
    untrusted text must account for that (#367, and #374 for the CLI's).
    """
    out: list[tuple[str, str]] = []
    buf: list[str] = []
    raw: list[str] = []
    quote: str | None = None
    for ch in s:
        if quote:
            if ch == quote:
                quote = None
                if ch == '"':
                    raw.append(ch)
            else:
                buf.append(ch)
                raw.append(ch)
        elif ch in ('"', "'"):
            quote = ch
            if ch == '"':
                raw.append(ch)
        elif ch.isspace():
            if buf:
                out.append(("".join(buf), "".join(raw)))
            buf, raw = [], []
        else:
            buf.append(ch)
            raw.append(ch)
    if buf:
        out.append(("".join(buf), "".join(raw)))
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


def _reject_unstorable(tok: str) -> None:
    """Refuse a token Postgres cannot take as text, naming its operator (#377).

    Two shapes, one outcome. Postgres ``TEXT`` cannot hold a NUL, so psycopg
    raises ``DataError``; and a lone surrogate (JSON ``"\\ud800"`` decodes to
    one) is not encodable as UTF-8, so psycopg raises ``UnicodeEncodeError``.
    Neither is an ``APIError``, so each was an unhandled 500 on HTTP and, on
    MCP, FastMCP's generic ``ToolError`` carrying psycopg's raw text.

    Refused, not stripped with ``pgtext.strip_nuls``: a stripped query is a
    different question answered as if it were the one asked (#364). Every
    character lands in some token, since the tokenizer drops only whitespace
    and quote characters.
    """
    if "\x00" in tok:
        problem = "a NUL character (U+0000)"
    else:
        try:
            tok.encode("utf-8")
            return
        except UnicodeEncodeError as exc:
            problem = f"a lone surrogate (U+{ord(tok[exc.start]):04X})"
    op = tok.partition(":")[0].lower() if ":" in tok else ""
    field_name = op if op in _OPERATORS else "query"
    raise QueryParseError(f"{field_name}: contains {problem}, which cannot be searched")


def parse_query(query: str) -> ParsedQuery:
    """Decompose a query string into free text + structured filters.

    Raises ``QueryParseError`` for a malformed date, an empty ``lang:``, an
    unknown or contradictory ``has:``, and a NUL or lone surrogate anywhere in
    the query.
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

    for tok, raw in _tokenize(query):
        _reject_unstorable(tok)
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
                        free_parts.append(raw)
                    continue
                elif op_l == "folder_id":
                    try:
                        f_folder_ids.append(int(value))
                    except ValueError:
                        free_parts.append(raw)
                    continue
                elif op_l == "after":
                    f_after = _parse_date(value, "after")
                elif op_l == "before":
                    f_before = _parse_date(value, "before")
                elif op_l == "has":
                    f_has_attachment = _parse_has(value, f_has_attachment)
                continue
        # A spaced dash is a separator ("Order confirmation - Qantas"), but
        # websearch_to_tsquery reads it as negating the next word (#419).
        if raw.strip("-"):
            free_parts.append(raw)

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
