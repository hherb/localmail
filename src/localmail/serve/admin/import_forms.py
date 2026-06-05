"""Pure form-parsing helpers for the import admin screens (no IO)."""
from __future__ import annotations

from localmail.api.admin.imports import ImportBusyError, ImportFieldError


class FormError(ValueError):
    """Malformed raw form input the service layer wouldn't otherwise see."""


def form_to_create_kwargs(form: dict) -> dict:
    """Map a raw create-form dict to create_job(**kwargs)."""
    account_raw = str(form.get("account_id", "")).strip()
    source_kind = str(form.get("source_kind", "")).strip()
    source_path = str(form.get("source_path", "")).strip()
    if not account_raw.isdigit():
        raise FormError("select an archive account")
    if source_kind not in ("mbox", "maildir"):
        raise FormError("choose a source kind (mbox or maildir)")
    if not source_path:
        raise FormError("source_path must not be blank")
    return {
        "account_id": int(account_raw),
        "source_kind": source_kind,
        "source_path": source_path,
    }


def field_errors_from(
    err: ImportFieldError | ImportBusyError | FormError,
) -> dict[str, str]:
    """Map a validation/guard error to {field: message}; default to '_form'."""
    msg = str(err)
    if "source_path" in msg or "path" in msg:
        return {"source_path": msg}
    return {"_form": msg}
