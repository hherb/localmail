# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The `loc: msg` rendering of a request-schema failure (#370)."""
from __future__ import annotations

from localmail.serve.request_validation import validation_detail


def _err(loc, msg="bad"):
    return {"loc": loc, "msg": msg, "type": "x"}


def test_the_body_prefix_is_dropped_and_the_path_dotted():
    assert validation_detail([_err(("body", "filters", "lang"))]) == "filters.lang: bad"


def test_a_list_index_is_part_of_the_path():
    assert validation_detail([_err(("body", "fields", 1))]) == "fields.1: bad"


def test_an_error_on_the_whole_body_names_the_body():
    # A non-object body has no field to name; an empty loc would read as ": bad".
    assert validation_detail([_err(("body",))]) == "body: bad"


def test_every_error_is_reported_in_order():
    detail = validation_detail([_err(("body", "a"), "one"), _err(("body", "b"), "two")])
    assert detail == "a: one; b: two"


def test_a_non_body_location_keeps_its_prefix():
    # A query or header parameter is not the body; saying so is the diagnosis.
    assert validation_detail([_err(("query", "limit"))]) == "query.limit: bad"


def test_a_json_decode_error_keeps_body_and_names_the_position_and_reason():
    err = {"type": "json_invalid", "loc": ("body", 13), "msg": "JSON decode error",
           "ctx": {"error": "Expecting value"}}
    assert validation_detail([err]) == "body: JSON decode error at position 13: Expecting value"
