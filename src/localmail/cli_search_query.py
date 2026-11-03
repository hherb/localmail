"""Pure: compose ``localmail search``'s flags and free text into one query (#374).

The flag tokens come first, for the reason ``api.search.build_query_string``
gives (#367): an unclosed ``"`` in the free text runs to the end of the
string, so free text placed first swallowed every flag token into free text
and the command exited 0 with unfiltered results.

First is only safe because every flag token is self-contained. Each string
value goes through ``quoted_filter_value``, the rule the HTTP composer uses,
so a ``"`` in a flag value cannot break out and swallow the free text or
inject an operator; dates are click-parsed ``datetime`` objects rendered as
one ASCII token; ``has:attachment`` is a constant. Hence
``parse_query(composed).free_text == parse_query(free_text).free_text``.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from localmail.search.query import quoted_filter_value


def compose_cli_query(
    free_text: str,
    *,
    accounts: Sequence[str],
    folders: Sequence[str],
    after: datetime | None,
    before: datetime | None,
    from_substr: str | None,
    to_substr: str | None,
    subject_substr: str | None,
    has_attachment: bool | None,
    label: str | None,
) -> str:
    """Return the query string ``Searcher.search`` parses.

    Raises ``ValueError`` naming the flag when a value is empty once its
    quotes are stripped: ``key:""`` parses as free text, so the filter would
    silently vanish.
    """
    parts: list[str] = []
    for a in accounts:
        parts.append(f"account:{_quote(a, '--account')}")
    for f in folders:
        parts.append(f"folder:{_quote(f, '--folder')}")
    if from_substr:
        parts.append(f"from:{_quote(from_substr, '--from')}")
    if to_substr:
        parts.append(f"to:{_quote(to_substr, '--to')}")
    if subject_substr:
        parts.append(f"subject:{_quote(subject_substr, '--subject')}")
    if after:
        parts.append(f"after:{after.date().isoformat()}")
    if before:
        parts.append(f"before:{before.date().isoformat()}")
    if has_attachment:
        parts.append("has:attachment")
    if label:
        parts.append(f"label:{_quote(label, '--label')}")
    if free_text:
        parts.append(free_text)
    return " ".join(parts)


def _quote(value: str, flag: str) -> str:
    quoted = quoted_filter_value(value)
    if quoted is None:
        raise ValueError(f"{flag}: value is empty once its quotes are removed")
    return quoted
