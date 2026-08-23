# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Pure form-parsing helpers for the API-key admin screen (no IO)."""
from __future__ import annotations

from localmail.api.admin.api_keys import ApiKeyFieldError


class FormError(ValueError):
    """Malformed raw form input the service layer wouldn't otherwise see."""


def form_to_create_kwargs(name: object, account_ids: list[str]) -> dict:
    """Map the raw create-form values to create_key(**kwargs)."""
    cleaned = str(name or "").strip()
    if not cleaned:
        raise FormError("name must not be blank")
    parsed: list[int] = []
    for raw in account_ids:
        if not str(raw).isdigit():
            raise FormError(f"malformed account id {raw!r}")
        parsed.append(int(raw))
    return {"name": cleaned, "account_ids": parsed}


def field_errors_from(err: ApiKeyFieldError | FormError) -> dict[str, str]:
    """Map a validation error to {field: message}; fall back to '_form'."""
    msg = str(err)
    if "name" in msg:
        return {"name": msg}
    return {"_form": msg}
