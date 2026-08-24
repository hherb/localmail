# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""API-key name rules. Pure: no IO, no DB.

The key's name is its principal's ``api_users.username``, so uniqueness comes
free from that column's constraint and is not re-stated here. Shaped like
``account_names.account_name_error`` — a message, or None — so each caller wraps
it in its own error type. The admin panel renders it beside the Name input via
``api_key_forms.field_errors_from``; the CLI and the JSON route surface it as
the whole message.
"""
from __future__ import annotations

#: Upper bound on an API-key name, in characters.
NAME_MAX_CHARS = 128


def api_key_name_error(name: str) -> str | None:
    """Return why ``name`` is unusable as an API-key name, or None if it is fine."""
    if not name or not name.strip():
        return "name must not be blank"
    if len(name.strip()) > NAME_MAX_CHARS:
        return f"name longer than {NAME_MAX_CHARS} chars"
    return None
