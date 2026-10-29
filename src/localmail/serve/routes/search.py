# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""POST /v1/search endpoint."""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from localmail.api.acl import allowed_account_ids
from localmail.api.errors import FeatureUnavailable, ValidationFailed
from localmail.api.search import run_search
from localmail.serve.middleware import get_authenticated_user
from localmail.serve.request_validation import ProblemJsonValidationRoute

# Schema errors answer problem+json 400 like every other refusal here (#370).
router = APIRouter(route_class=ProblemJsonValidationRoute)


class SearchFiltersModel(BaseModel):
    account_ids: list[str] | None = None
    folder_ids: list[str] | None = None
    date_from: str | None = None
    date_to: str | None = None
    has_attachment: bool | None = None
    lang: str | None = None
    from_: str | None = Field(default=None, alias="from")
    to: str | None = None
    subject: str | None = None
    after: str | None = None
    before: str | None = None

    # "allow", never "ignore": an unknown key has to reach `run_search`,
    # which refuses it by name. Ignored, a planner's `has_attachments` (the
    # hit field's name, sent as a filter) got unfiltered results and a 200
    # (#364).
    model_config = {"populate_by_name": True, "extra": "allow"}


SEARCH_LIMIT_MAX = 200


class SearchRequest(BaseModel):
    # "allow" so the route can refuse an unknown field by name rather than
    # drop it (#364). It was chosen because "forbid" meant FastAPI's 422 with
    # an array `detail`. Since #370 the router's route class makes any schema
    # error a problem+json 400, so "forbid" would no longer cost that; "allow"
    # stays because `_unknown_field_error`'s wording is already the contract.
    model_config = {"extra": "allow"}

    # Optional, and nullable like every other optional field here: a
    # filter-only search is a search. A query with no free text takes the
    # date walk (#324) and reports `rankable: false` (#353).
    query: str | None = ""
    filters: SearchFiltersModel = Field(default_factory=SearchFiltersModel)
    limit: int = Field(default=50, ge=1, le=SEARCH_LIMIT_MAX)
    # "rank" orders by rerank relevance; "date" takes the date-ordered
    # keyset walk directly rather than the hybrid pool, ordering by
    # COALESCE(internal_date, date_sent) DESC NULLS LAST.
    #
    # Omitting it resolves to whichever of those will actually serve the
    # request: "rank" when the query has free text, "date" when it has none
    # (#324). A query with no free text — blank, or only filter operators,
    # since `parse_query` lifts those out — has nothing for the pool to
    # rank, so it takes the date walk and a *stated* "rank" for it is a 400
    # rather than a silent drop.
    #
    # Null rather than "rank" so that omitting it is distinguishable from
    # asking for it: alongside a `cursor` the cursor decides the ordering,
    # and a *stated* sort it cannot serve is a 400 rather than a silently
    # dropped cursor.
    sort: Literal["rank", "date"] | None = None
    # Direction for the sort criterion above. Orthogonal to `sort` so a
    # future criterion inherits it without doubling the `sort` enum.
    # "asc" is rejected for a *resolved* sort="rank": the rank path serves a
    # bounded candidate pool, so reversing it returns the least relevant of
    # the top hits rather than of the archive. It reads the resolved sort, so
    # "asc" alone on a query with no free text is honoured — that query
    # resolves to "date" (#324).
    #
    # Null rather than "desc" for the reason `sort` is null: alongside a
    # `cursor` the cursor decides the direction, and a model default would
    # be a statement the caller never made — contradicting every ascending
    # cursor.
    sort_order: Literal["asc", "desc"] | None = None
    cursor: str | None = None
    # Opt-in LLM query rewrite (Phase 4). Ignored gracefully when the server
    # has no rewriter configured — the response's rewrite_skipped reflects it.
    smart: bool = False
    # Slice E: project each hit to exactly these keys (`snippet` is the
    # plain-text name for `snippet_html`). The element type is pydantic's:
    # a non-list `fields`, or a non-string in it, is a schema error, which
    # the route class answers as a problem+json 400 (#370, #389), so
    # `fields_error`'s type branches serve library callers only. Not `Any`
    # like `snippet_chars`: pydantic does not lax-coerce into `list[str]`, so
    # there is no silent 200 to escape, and the OpenAPI schema keeps the
    # type. No `min_length`/`max_length`: emptiness and the cap (#394) are
    # `fields_error`'s, whose messages name the rule rather than a location.
    fields: list[str] | None = None
    # `Any`, not `int | bool` and not a bare `int`: pydantic's lax coercion is
    # exactly what this field must not have. A plain `int` field silently
    # coerces `"5"` and `5.0` to `5` — answered 200 under a request that named
    # a string or a float, never the integer `snippet_width_error` checked.
    # That silent 200 is the reason; the route class (#370) only turns a
    # *failed* validation into problem+json, and coercion never fails.
    # `int | bool` fixed the `bool` half (a
    # JSON `true` stayed a `bool` rather than coercing to `1`) but left the
    # string/float coercions live. `Any` reaches every JSON value into
    # `search.snippet_width.snippet_width_error` unmodified, so that pure
    # rule is the one authority: every refusal — bool, non-int,
    # out-of-range — is its call. Since #390 the Searcher reads the same
    # rule, passing `max_chars=None`, so the *type* refusal is worded once
    # for the wire and for library callers alike. The floor refusal is not:
    # an uncapped caller is told the floor without a ceiling they do not
    # have. Out-of-range does not reach a library caller at all.
    #
    # `null` is "unstated", not "anything else": the gate is `if
    # snippet_chars is not None`, matching `query`/`sort`/`cursor`/`fields`,
    # so the description says so rather than promising a 400 it will not
    # give (#393 review).
    snippet_chars: Any = Field(
        default=None,
        description=("Snippet window width in characters: an integer from 1 "
                     "to [search] snippet_max_chars. Omit it, or send null, "
                     "for the configured default. Anything else is a 400."),
    )


def _unknown_field_error(req: SearchRequest) -> str | None:
    """Name the request fields this route does not define, or ``None``.

    Checked here rather than in `run_search`, which takes keyword arguments
    and so cannot be handed one. It matters beyond typos: an older server
    ignoring a field a newer client relies on returns an answer that looks
    like the one asked for.
    """
    unknown = sorted(req.model_extra or {})
    if not unknown:
        return None
    noun = "field" if len(unknown) == 1 else "fields"
    supported = ", ".join(sorted(SearchRequest.model_fields))
    return (f"unknown {noun} {', '.join(repr(k) for k in unknown)}; "
            f"supported: {supported}")


@router.post("")
def search_endpoint(
    req: SearchRequest,
    request: Request,
    user=Depends(get_authenticated_user),
) -> dict[str, Any]:
    field_error = _unknown_field_error(req)
    if field_error is not None:
        raise ValidationFailed(field_error)
    searcher = request.app.state.searcher
    if searcher is None:
        raise FeatureUnavailable("search not configured on this server")
    pool = request.app.state.pool
    with pool.connection() as conn:
        allowed = allowed_account_ids(conn, user.id)
    filters_dict = {
        **req.filters.model_dump(by_alias=True, exclude_none=True),
        # Unknown keys again, `null` ones included: `exclude_none` drops those,
        # and a key that is not a filter is a mistake whatever its value.
        **(req.filters.model_extra or {}),
    }
    return run_search(
        searcher=searcher,
        free_text=req.query or "",
        filters=filters_dict,
        limit=req.limit,
        allowed_account_ids=allowed,
        user_id=user.id,
        sort=req.sort,
        sort_order=req.sort_order,
        cursor=req.cursor,
        smart=req.smart,
        fields=req.fields,
        snippet_chars=req.snippet_chars,
    )
