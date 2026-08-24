# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Pure form-parsing helpers for the API-key admin screen (no IO)."""
from __future__ import annotations

from localmail.api.admin.api_keys import ApiKeyFieldError


class FormError(ValueError):
    """Malformed raw form input the service layer wouldn't otherwise see.

    Carries its field like ``ApiKeyFieldError``, so ``field_errors_from`` reads
    one attribute rather than discriminating two error types.
    """

    def __init__(self, message: str, *, field: str = "_form") -> None:
        super().__init__(message)
        self.field = field


def form_to_create_kwargs(name: object, account_ids: list[str]) -> dict:
    """Map the raw create-form values to create_key(**kwargs)."""
    cleaned = str(name or "").strip()
    if not cleaned:
        raise FormError("name must not be blank", field="name")
    parsed: list[int] = []
    for raw in account_ids:
        if not str(raw).isdigit():
            raise FormError(f"malformed account id {raw!r}")
        parsed.append(int(raw))
    return {"name": cleaned, "account_ids": parsed}


def field_errors_from(err: ApiKeyFieldError | FormError) -> dict[str, str]:
    """Map a validation error to {field: message}.

    Reads the field the error was raised with. It used to grep the message for
    ``"name"``, which mis-filed the two likeliest operator errors and would
    mis-file any future wording by accident.
    """
    return {err.field: str(err)}
