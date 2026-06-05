"""Pure tests for user_forms + the service's pure guard predicate."""
from __future__ import annotations

import pytest

from localmail.api.admin.users import (
    LastAdminError,
    SelfActionError,
    UserFieldError,
    would_orphan_last_admin,
)
from localmail.serve.admin import user_forms as forms


def test_form_to_create_kwargs_basic():
    out = forms.form_to_create_kwargs({"username": " amy ", "password": "pw12345"})
    assert out == {"username": "amy", "password": "pw12345", "is_admin": False}


def test_form_to_create_kwargs_admin_checkbox_on():
    out = forms.form_to_create_kwargs(
        {"username": "boss", "password": "pw12345", "is_admin": "on"}
    )
    assert out["is_admin"] is True


@pytest.mark.parametrize("form", [
    {"username": "", "password": "pw12345"},
    {"username": "ok", "password": ""},
    {"password": "pw12345"},
])
def test_form_to_create_kwargs_blank_raises(form):
    with pytest.raises(forms.FormError):
        forms.form_to_create_kwargs(form)


def test_field_errors_username():
    assert forms.field_errors_from(UserFieldError("username 'x' already exists")) == {
        "username": "username 'x' already exists"
    }


def test_field_errors_password():
    assert forms.field_errors_from(UserFieldError("password must not be blank")) == {
        "password": "password must not be blank"
    }


def test_field_errors_fallback_form_level():
    out = forms.field_errors_from(LastAdminError("cannot demote the last active admin"))
    assert out == {"_form": "cannot demote the last active admin"}


def test_field_errors_self_action_fallback():
    out = forms.field_errors_from(SelfActionError("you cannot delete your own account"))
    assert out == {"_form": "you cannot delete your own account"}


@pytest.mark.parametrize("active_admin,count,expect", [
    (True, 1, True),
    (True, 2, False),
    (True, 0, True),   # defensive: never go negative
    (False, 1, False),
    (False, 5, False),
])
def test_would_orphan_last_admin(active_admin, count, expect):
    assert would_orphan_last_admin(
        target_is_active_admin=active_admin, active_admin_count=count
    ) is expect
