# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""What the language detector is allowed to see.

Marketing and newsletter mail is dominated by tracking URLs whose path
segments are long runs of high-entropy alphanumerics. Lingua scores that soup
confidently and lands on a low-resource language: on the live Mac archive 17%
of all labels named a language with no plausible presence in the archive,
Yoruba alone accounting for 7593 rows (#255).

Stripping URLs before detection resolves 99% of those rows when paired with
full-accuracy mode. Measured on the same archive, stripping invisible
characters (the U+034F preheader padding), email addresses, HTML tags and
separator rules each add **zero** further benefit -- so this module does one
thing, and additions belong here only with a measurement behind them.

This is the detector's input rule and nothing else: `messages.body_text`, the
FTS tsvector, chunking and embeddings all continue to see the original body.

Pure: no IO, stdlib only.
"""

from __future__ import annotations

import re

#: Matches the URL forms that occur in mail bodies: an explicit scheme, or the
#: bare `www.` host form that mail clients linkify. `\S+` runs to the next
#: whitespace, which deliberately swallows trailing `>` and `)` from
#: angle-bracketed and markdown-parenthesised links -- we are deleting, so
#: over-consuming punctuation costs nothing and under-consuming leaves
#: high-entropy residue behind.
_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)

_WHITESPACE_RE = re.compile(r"\s+")


def normalize_for_detection(text: str) -> str:
    """Return `text` with URLs removed and whitespace collapsed.

    The result is what the language detector sees. An empty return means the
    body carried no linguistic content -- callers must treat that as "unknown"
    rather than detecting against the original.
    """
    return _WHITESPACE_RE.sub(" ", _URL_RE.sub(" ", text)).strip()
