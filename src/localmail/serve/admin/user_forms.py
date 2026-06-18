# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Pure form-parsing helpers for the user admin screens (no IO).

Keeps the HTML router thin: every raw-form → service-kwargs transform and every
service-error → field mapping is unit-tested here in isolation.
"""
from __future__ import annotations

from localmail.api.admin.users import (
    LastAdminError,
    SelfActionError,
    UserFieldError,
)


class FormError(ValueError):
    """Malformed raw form input the service layer wouldn't otherwise see."""


def _checkbox(value: object) -> bool:
    """An HTML checkbox sends its value (e.g. 'on') when checked, nothing when not."""
    return bool(value)


def form_to_create_kwargs(form: dict) -> dict:
    """Map a raw create-form dict to create_user(**kwargs)."""
    username = str(form.get("username", "")).strip()
    password = str(form.get("password", ""))
    if not username:
        raise FormError("username must not be blank")
    if not password:
        raise FormError("password must not be blank")
    return {
        "username": username,
        "password": password,
        "is_admin": _checkbox(form.get("is_admin")),
    }


# Substring → field-name map for surfacing a validation error beside the input.
# Order matters: first match wins.
_FIELD_HINTS: tuple[tuple[str, str], ...] = (
    ("username", "username"),
    ("password", "password"),
)


def field_errors_from(
    err: UserFieldError | FormError | LastAdminError | SelfActionError,
) -> dict[str, str]:
    """Map a validation/guard error to {field: message}; fall back to '_form'."""
    msg = str(err)
    for needle, field in _FIELD_HINTS:
        if needle in msg:
            return {field: msg}
    return {"_form": msg}
