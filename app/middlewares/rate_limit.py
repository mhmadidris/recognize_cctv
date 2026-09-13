import os
import time
from collections import defaultdict, deque

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Simple per-process rate limiter for API requests.

    The service currently runs with one worker. For multiple replicas, move the
    counters to a shared store such as Redis or enforce limits at the proxy.
    """

    def __init__(self, app: FastAPI):
        super().__init__(app)
        self.window_seconds = self._get_int("RATE_LIMIT_WINDOW_SECONDS", 60)
        self.api_limit = self._get_int("RATE_LIMIT_REQUESTS", 120)
        self.mutation_limit = self._get_int("RATE_LIMIT_MUTATION_REQUESTS", 30)
        self.auth_limit = self._get_int("RATE_LIMIT_AUTH_REQUESTS", 5)
        self._requests: dict[tuple[str, str], deque[float]] = defaultdict(deque)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.method == "OPTIONS" or not request.url.path.startswith("/api/"):
            return await call_next(request)

        key = (self._client_key(request), self._bucket(request))
        limit = self._limit(request)
        now = time.monotonic()
        timestamps = self._requests[key]

        while timestamps and timestamps[0] <= now - self.window_seconds:
            timestamps.popleft()

        if len(timestamps) >= limit:
            retry_after = max(1, int(self.window_seconds - (now - timestamps[0])))
            response = JSONResponse(
                status_code=429,
                content={
                    "message": "Too many requests. Please try again later.",
                    "result": None,
                    "errors": None,
                },
                headers={"Retry-After": str(retry_after)},
            )
            self._set_headers(response, limit, 0)
            return response

        timestamps.append(now)
        self._cleanup(now)
        response = await call_next(request)
        self._set_headers(response, limit, max(0, limit - len(timestamps)))
        return response

    def _bucket(self, request: Request) -> str:
        path = request.url.path
        if path.startswith("/api/v1/auth/"):
            return "auth"
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            return "mutation"
        return "api"

    def _limit(self, request: Request) -> int:
        bucket = self._bucket(request)
        if bucket == "auth":
            return self.auth_limit
        if bucket == "mutation":
            return self.mutation_limit
        return self.api_limit

    def _client_key(self, request: Request) -> str:
        client = request.client.host if request.client else "unknown"
        if os.getenv("RATE_LIMIT_TRUST_PROXY", "false").lower() in {"1", "true", "yes"}:
            forwarded_for = request.headers.get("x-forwarded-for")
            if forwarded_for:
                client = forwarded_for.split(",", 1)[0].strip() or client
        return client

    def _cleanup(self, now: float) -> None:
        if len(self._requests) < 1000:
            return
        expired = [
            key for key, timestamps in self._requests.items()
            if not timestamps or timestamps[-1] <= now - self.window_seconds
        ]
        for key in expired:
            del self._requests[key]

    def _set_headers(self, response: Response, limit: int, remaining: int) -> None:
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        response.headers["X-RateLimit-Reset"] = str(self.window_seconds)

    @staticmethod
    def _get_int(name: str, default: int) -> int:
        try:
            return max(1, int(os.getenv(name, str(default))))
        except (TypeError, ValueError):
            return default
