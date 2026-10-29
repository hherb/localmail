# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""A request-schema error on /v1/search is a problem+json 400 (#370, #389).

FastAPI answers a pydantic failure with 422 and an array `detail`, while every
refusal `run_search` makes is a problem+json 400 with a string `detail`. A
client that renders `detail` (kastellan) got a string for one mistake and an
array for the one beside it. The search router maps the schema failure onto
`ValidationFailed`, so one envelope covers both. Scoped to that router: other
routes keep FastAPI's 422.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from localmail.serve.app import create_app

_SCHEMA_ERRORS = [
    # #370's list, reproduced through the real router in #366's review.
    ([1, 2], "body"),
    ({"filters": None}, "filters"),
    ({"sort": "Date"}, "sort"),
    ({"limit": 500}, "limit"),
    ({"filters": {"has_attachment": "maybe"}}, "filters.has_attachment"),
    ({"filters": {"date_from": 20240101}}, "filters.date_from"),
    ({"filters": {"lang": ["en", "de"]}}, "filters.lang"),
    # #389: the two `fields` shapes that skipped `fields_error`.
    ({"fields": "subject"}, "fields"),
    ({"fields": ["snippet", 5]}, "fields.1"),
]


def _post(db_dsn, token, body):
    searcher = MagicMock()
    client = TestClient(create_app(db_dsn=db_dsn, searcher=searcher))
    r = client.post("/v1/search", json=body, headers={"Authorization": f"Bearer {token}"})
    searcher.search.assert_not_called()
    return r


@pytest.mark.parametrize(("body", "loc"), _SCHEMA_ERRORS)
def test_a_schema_error_is_a_problem_json_400_naming_the_field(db_dsn, api_token, body, loc):
    r = _post(db_dsn, api_token, body)
    assert r.status_code == 400, r.text
    assert r.headers["content-type"].startswith("application/problem+json")
    detail = r.json()["detail"]
    assert isinstance(detail, str)
    assert detail.startswith(f"{loc}: "), detail


@pytest.mark.parametrize(
    ("content", "detail"),
    [
        # FastAPI's loc is ("body", <offset>); a bare offset would read as a field.
        (b"{not json", "body: JSON decode error at position 1: "
                       "Expecting property name enclosed in double quotes"),
        # Not UTF-8: FastAPI raises its own bare 400, not a validation error.
        (b'{"query": "\xff"}', "body: There was an error parsing the body"),
    ],
    ids=["malformed-json", "not-utf8"],
)
def test_a_body_that_does_not_parse_is_a_problem_json_400(db_dsn, api_token, content, detail):
    searcher = MagicMock()
    client = TestClient(create_app(db_dsn=db_dsn, searcher=searcher))
    r = client.post(
        "/v1/search", content=content,
        headers={"Authorization": f"Bearer {api_token}",
                 "Content-Type": "application/json"},
    )
    assert r.status_code == 400, r.text
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.json()["detail"] == detail
    searcher.search.assert_not_called()


def test_other_routes_keep_fastapis_422(db_dsn, api_token):
    # The mapping is scoped to /v1/search; widening it is #370's option 2,
    # which needs the GUI's and kastellan's error handling checked first.
    client = TestClient(create_app(db_dsn=db_dsn, searcher=MagicMock()))
    r = client.get("/v1/messages?limit=500", headers={"Authorization": f"Bearer {api_token}"})
    assert r.status_code == 422
    assert isinstance(r.json()["detail"], list)
