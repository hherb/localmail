# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Pure validation/parsing of the /oauth/consent POST body.

No IO. The router calls this, then (on allow) verifies credentials and mints a
code; (on deny) redirects with error=access_denied.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


class ConsentFormError(ValueError):
    """The submitted form was structurally invalid."""


@dataclass(frozen=True)
class ConsentDecision:
    req: str
    username: str | None
    password: str | None
    allow: bool


def parse_consent_form(form: Mapping[str, str]) -> ConsentDecision:
    req = form.get("req")
    if not req:
        raise ConsentFormError("missing authorization request")
    decision = form.get("decision")
    if decision not in ("allow", "deny"):
        raise ConsentFormError("decision must be 'allow' or 'deny'")
    if decision == "deny":
        return ConsentDecision(req=req, username=None, password=None, allow=False)
    username = form.get("username")
    password = form.get("password")
    if not username or not password:
        raise ConsentFormError("username and password are required to allow")
    return ConsentDecision(req=req, username=username, password=password, allow=True)
