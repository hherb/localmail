"""Pure form-parsing helpers for the account admin screens (no IO).

The HTML router stays thin by delegating every raw-form → service-kwargs
transformation here, where it is unit-tested in isolation. The RFC 6154
special-use flag set is the single source of truth (no magic strings in
templates or the router).
"""
from __future__ import annotations

from localmail.api.admin.accounts import Account, AccountFieldError

# RFC 6154 IMAP special-use flags offered as folder-deny checkboxes. Closed
# set — folder_deny_flags must never contain anything outside this tuple.
DENY_FLAGS: tuple[str, ...] = (
    r"\Trash", r"\Junk", r"\All", r"\Drafts",
    r"\Sent", r"\Important", r"\Flagged",
)


class FormError(ValueError):
    """Raised for malformed raw form input the service layer wouldn't see
    (e.g. a non-numeric port, an unknown deny flag)."""


def parse_lines(text: str) -> list[str] | None:
    """Split a textarea into a stripped, blank-free list. Empty → None."""
    items = [line.strip() for line in text.splitlines()]
    items = [line for line in items if line]
    return items or None


def parse_deny_flags(selected: list[str]) -> list[str] | None:
    """Validate selected deny-flag checkboxes against DENY_FLAGS. Empty → None."""
    unknown = [f for f in selected if f not in DENY_FLAGS]
    if unknown:
        raise FormError(f"unknown deny flags: {unknown}")
    return list(selected) or None


def _parse_port(raw: str, *, auth_method: str) -> int | None:
    raw = raw.strip()
    if auth_method == "archive" or not raw:
        return None
    try:
        return int(raw)
    except ValueError as e:
        raise FormError("imap_port must be a number") from e


def _none_if_blank(value: str) -> str | None:
    value = value.strip()
    return value or None


def form_to_create_kwargs(form: dict, *, deny_flags_selected: list[str]) -> dict:
    """Map a raw create-form dict to create_account(**kwargs)."""
    auth_method = form["auth_method"]
    return {
        "name": form["name"].strip(),
        "email_address": form["email_address"].strip(),
        "auth_method": auth_method,
        "imap_host": (None if auth_method == "archive"
                      else _none_if_blank(form.get("imap_host", ""))),
        "imap_port": _parse_port(form.get("imap_port", ""), auth_method=auth_method),
        "oauth_provider": _none_if_blank(form.get("oauth_provider", "")),
        "folder_allow": parse_lines(form.get("folder_allow", "")),
        "folder_deny": parse_lines(form.get("folder_deny", "")),
        "folder_deny_flags": parse_deny_flags(deny_flags_selected),
    }


def form_to_patch_fields(form: dict, *, deny_flags_selected: list[str]) -> dict:
    """Map a raw edit-form dict to update_account(**fields).

    Name is immutable post-create (it keys the keyring secret), so it is not
    part of the patch.
    """
    fields = form_to_create_kwargs(form, deny_flags_selected=deny_flags_selected)
    fields.pop("name")
    return fields


def account_to_form_values(account: Account) -> dict:
    """Inverse of the create mapping, for prefilling the edit form."""
    return {
        "name": account.name,
        "email_address": account.email_address,
        "auth_method": account.auth_method,
        "oauth_provider": account.oauth_provider or "",
        "imap_host": account.imap_host or "",
        "imap_port": str(account.imap_port) if account.imap_port else "",
        "folder_allow": "\n".join(account.folder_allow or []),
        "folder_deny": "\n".join(account.folder_deny or []),
        "deny_flags_checked": set(account.folder_deny_flags or []),
        "sync_enabled": account.sync_enabled,
    }


# Substring → field-name map for surfacing a service AccountFieldError beside
# the offending input. Order matters: first match wins.
_FIELD_HINTS: tuple[tuple[str, str], ...] = (
    ("imap_port", "imap_port"),
    ("imap_host", "imap_host"),
    ("email_address", "email_address"),
    ("name", "name"),
    ("oauth_provider", "oauth_provider"),
    ("auth_method", "auth_method"),
)


def field_errors_from(err: AccountFieldError | FormError) -> dict[str, str]:
    """Map a validation error to {field: message}; falls back to a form-level
    error under the "_form" key when no field matches."""
    msg = str(err)
    for needle, field in _FIELD_HINTS:
        if needle in msg:
            return {field: msg}
    return {"_form": msg}
