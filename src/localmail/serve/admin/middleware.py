"""ASGI middleware that rewrites sensitive query parameters before logging.

The request handler still sees the originals via get_unscrubbed_query_params,
because we stash a parsed copy in request.scope under a private key before
rewriting.
"""
from __future__ import annotations

from urllib.parse import parse_qsl, urlencode

from starlette.middleware.base import BaseHTTPMiddleware


_UNSCRUBBED_KEY = "localmail.admin.unscrubbed_query"


def get_unscrubbed_query_params(request) -> dict[str, str]:
    """Return the original (non-redacted) query params for this request."""
    return request.scope.get(_UNSCRUBBED_KEY, {})


class ScrubSensitiveQueryParamsMiddleware(BaseHTTPMiddleware):
    """Rewrites request.scope['query_string'] so subsequent access-log
    middleware sees REDACTED instead of secrets.

    Adds a copy of the original query params to request.scope under a
    private key so route handlers can still read the originals.
    """

    def __init__(self, app, *, sensitive: tuple[str, ...]) -> None:
        super().__init__(app)
        self._sensitive = set(sensitive)

    async def dispatch(self, request, call_next):
        raw = request.scope.get("query_string", b"")
        if raw:
            pairs = parse_qsl(raw.decode("latin1"), keep_blank_values=True)
            request.scope[_UNSCRUBBED_KEY] = dict(pairs)
            scrubbed = [
                (k, "REDACTED" if k in self._sensitive else v)
                for k, v in pairs
            ]
            request.scope["query_string"] = urlencode(scrubbed).encode("latin1")
        else:
            request.scope[_UNSCRUBBED_KEY] = {}
        return await call_next(request)
