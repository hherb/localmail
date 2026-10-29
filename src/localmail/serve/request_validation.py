# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Answer a request-schema error as a problem+json 400 (#370).

FastAPI answers a pydantic failure with 422 and an array ``detail``. Every
refusal ``run_search`` makes is a ``ValidationFailed`` 400 with a string
``detail``, so a client rendering ``detail`` got a string for one mistake and
an array for the one beside it (#389 is the ``fields`` case). A router opts in
with ``route_class=ProblemJsonValidationRoute``; it is not app-wide, because
that would change every route's 422 (#370's option 2).
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine, Sequence
from typing import Any

from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from starlette.requests import Request
from starlette.responses import Response

from localmail.api.errors import ValidationFailed

_BODY = "body"
_SEPARATOR = "; "


def _location(loc: Sequence[str | int]) -> str:
    # The body is implied for a POST body; a query/header location is not.
    parts = list(loc[1:] if len(loc) > 1 and loc[0] == _BODY else loc)
    return ".".join(str(p) for p in parts)


def validation_detail(errors: Sequence[dict[str, Any]]) -> str:
    """One ``loc: msg`` per pydantic error, in the order pydantic reports them."""
    return _SEPARATOR.join(f"{_location(e['loc'])}: {e['msg']}" for e in errors)


class ProblemJsonValidationRoute(APIRoute):
    """An ``APIRoute`` whose schema errors become ``ValidationFailed``.

    The raised ``APIError`` reaches the app's existing problem+json handler,
    so the envelope is the one every other 400 on the route already uses.
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def problem_json_handler(request: Request) -> Response:
            try:
                return await handler(request)
            except RequestValidationError as exc:
                raise ValidationFailed(validation_detail(exc.errors())) from exc

        return problem_json_handler
