"""Pure form-parse tests for the import screens."""
from __future__ import annotations

import pytest

from localmail.api.admin.imports import ImportBusyError, ImportFieldError
from localmail.serve.admin.import_forms import (
    FormError,
    field_errors_from,
    form_to_create_kwargs,
)


def test_form_to_create_kwargs_ok():
    kwargs = form_to_create_kwargs(
        {"account_id": "5", "source_kind": "mbox", "source_path": "/srv/a.mbox"})
    assert kwargs == {"account_id": 5, "source_kind": "mbox", "source_path": "/srv/a.mbox"}


def test_form_strips_whitespace_on_path_and_account():
    kwargs = form_to_create_kwargs(
        {"account_id": " 7 ", "source_kind": "maildir", "source_path": "  /srv/md  "})
    assert kwargs == {"account_id": 7, "source_kind": "maildir", "source_path": "/srv/md"}


def test_form_rejects_blank_path():
    with pytest.raises(FormError):
        form_to_create_kwargs({"account_id": "5", "source_kind": "mbox", "source_path": ""})


def test_form_rejects_non_digit_account():
    with pytest.raises(FormError):
        form_to_create_kwargs(
            {"account_id": "x", "source_kind": "mbox", "source_path": "/a"})


def test_form_rejects_bad_source_kind():
    with pytest.raises(FormError):
        form_to_create_kwargs(
            {"account_id": "5", "source_kind": "zip", "source_path": "/a"})


def test_field_errors_path_maps_to_source_path_field():
    out = field_errors_from(FormError("source_path must not be blank"))
    assert out == {"source_path": "source_path must not be blank"}


def test_field_errors_non_path_maps_to_form():
    archive = field_errors_from(ImportFieldError("imports target an archive account"))
    assert archive == {"_form": "imports target an archive account"}
    busy = field_errors_from(ImportBusyError("an import is already running"))
    assert busy == {"_form": "an import is already running"}
