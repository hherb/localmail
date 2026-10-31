# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The IDLE renewal default sits well inside RFC 2177's 29-minute limit (#416).

The old 1740 s renewed at the limit itself. Both hosts carried it because they
copied ``config.example.toml``, so the example is pinned to the model default.
(The resets #416 reported came from a silent-flow drop on the path,
which TCP keepalive fixes; see ``test_imap_keepalive.py``.)
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from localmail.config import DaemonConfig
from localmail.idle import RFC2177_IDLE_LIMIT_S

EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.toml"


def test_the_default_renews_with_at_least_half_the_ceiling_to_spare() -> None:
    assert DaemonConfig().idle_renew_seconds <= RFC2177_IDLE_LIMIT_S // 2


def test_the_example_config_carries_the_default() -> None:
    with EXAMPLE.open("rb") as fh:
        example = tomllib.load(fh)["daemon"]["idle_renew_seconds"]
    assert example == DaemonConfig().idle_renew_seconds
