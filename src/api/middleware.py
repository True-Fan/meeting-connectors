"""HTTP middleware.

Binds a correlation id for the lifetime of every request so that the requirement
"every log carries a correlation id" holds for the HTTP edge too, not only inside
sessions. An inbound ``X-Correlation-ID`` is honoured, which lets an operator's
request be traced across service boundaries; otherwise one is minted.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from src.domain.ids import CorrelationId, new_correlation_id
from src.infrastructure.context import bind_context

CORRELATION_HEADER = "X-Correlation-ID"


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Bind a correlation id per request and echo it on the response."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        inbound = request.headers.get(CORRELATION_HEADER)
        correlation_id = CorrelationId(inbound) if inbound else new_correlation_id()

        with bind_context(correlation_id=correlation_id):
            response = await call_next(request)

        response.headers[CORRELATION_HEADER] = correlation_id
        return response
