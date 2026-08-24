# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb
# ruff: noqa: E501

"""The two ordering axes a search request states, and their defaults.

``sort`` picks *what* orders the results; ``sort_order`` picks *which way*
that ordering runs. They are orthogonal — adding ``date_asc``-style members
to ``sort`` was rejected because a third ordering criterion would double the
enum again.

**Both defaults live beside the type they range over**, which is #312's
rule: ``Searcher.search`` and ``api.search_cursor`` each resolve an
unstated value, and two layers resolving "unstated" from two literals is
the drift itself.

They sit in their own module rather than in ``searcher.py`` because
``date_keyset.py`` — which ``searcher.py`` imports — needs ``SortOrder``
at runtime for its ORDER BY completeness check, and defining it in both
places is the same drift one level down. The co-location argument is
unchanged; only the address is. ``searcher.py`` imports these names, so
``from localmail.search.searcher import SortMode`` keeps resolving.
"""
from __future__ import annotations

from typing import Literal

SortMode = Literal["rank", "date"]

#: The sort a caller gets when it states none.
DEFAULT_SORT: SortMode = "rank"

SortOrder = Literal["asc", "desc"]

#: The direction a caller gets when it states none.
DEFAULT_SORT_ORDER: SortOrder = "desc"
