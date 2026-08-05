from __future__ import annotations

import ipaddress
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable, Iterable
from typing import Any
from urllib.parse import urlsplit

from starlette.responses import PlainTextResponse


class FailureLimiter:
    """Bounded in-memory sliding-window limiter for failed authentication attempts."""

    def __init__(self, max_keys: int = 10_000) -> None:
        self.max_keys = max_keys
        self._events: OrderedDict[str, deque[float]] = OrderedDict()
        self._lock = threading.Lock()

    def _recent(self, key: str, window_seconds: float, now: float) -> deque[float]:
        events = self._events.get(key, deque())
        cutoff = now - window_seconds
        while events and events[0] <= cutoff:
            events.popleft()
        if events:
            self._events[key] = events
            self._events.move_to_end(key)
        else:
            self._events.pop(key, None)
        return events

    def blocked(self, key: str, limit: int, window_seconds: float) -> bool:
        with self._lock:
            return len(self._recent(key, window_seconds, time.monotonic())) >= limit

    def hit(self, key: str, window_seconds: float) -> None:
        with self._lock:
            now = time.monotonic()
            events = self._recent(key, window_seconds, now)
            events.append(now)
            self._events[key] = events
            self._events.move_to_end(key)
            while len(self._events) > self.max_keys:
                self._events.popitem(last=False)

    def clear(self, key: str) -> None:
        with self._lock:
            self._events.pop(key, None)


def client_ip_allowed(address: str, networks: Iterable[str]) -> bool:
    configured = tuple(networks)
    if not configured:
        return True
    try:
        client = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    return any(client in ipaddress.ip_network(network, strict=False) for network in configured)


class HostValidationMiddleware:
    """Allow configured hostnames and, when enabled, literal IPv4/IPv6 Host headers."""

    def __init__(
        self,
        app: Callable[..., Awaitable[None]],
        allowed_hosts: Iterable[str],
        allow_ip_hosts: bool = True,
    ) -> None:
        self.app = app
        self.allowed_hosts = tuple(host.lower().rstrip(".") for host in allowed_hosts)
        self.allow_ip_hosts = allow_ip_hosts

    @staticmethod
    def _hostname(host_header: str) -> str:
        try:
            parsed = urlsplit(f"//{host_header}")
        except ValueError:
            return ""
        if parsed.username or parsed.password or parsed.path:
            return ""
        return (parsed.hostname or "").lower().rstrip(".")

    def _allowed(self, host: str) -> bool:
        if not host:
            return False
        if "*" in self.allowed_hosts:
            return True
        if self.allow_ip_hosts:
            try:
                ipaddress.ip_address(host)
            except ValueError:
                pass
            else:
                return True
        for pattern in self.allowed_hosts:
            if pattern.startswith("*."):
                if host.endswith(pattern[1:]) and host != pattern[2:]:
                    return True
            elif host == pattern:
                return True
        return False

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        host_headers = [
            value.decode("latin-1")
            for name, value in scope.get("headers", [])
            if name.lower() == b"host"
        ]
        if len(host_headers) != 1 or not self._allowed(self._hostname(host_headers[0])):
            response = PlainTextResponse("Invalid host header", status_code=400)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


class _RequestBodyTooLarge(Exception):
    pass


class RequestBodyLimitMiddleware:
    """Reject oversized fixed-length and chunked HTTP request bodies before parsing."""

    def __init__(self, app: Callable[..., Awaitable[None]], max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        raw_length = headers.get(b"content-length")
        if raw_length:
            try:
                content_length = int(raw_length)
            except ValueError:
                await self._reject(send, 400, b'{"detail":"Invalid Content-Length"}')
                return
            if content_length < 0 or content_length > self.max_bytes:
                await self._reject(send, 413, b'{"detail":"Request body too large"}')
                return

        received = 0
        response_started = False

        async def limited_receive() -> dict[str, Any]:
            nonlocal received
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise _RequestBodyTooLarge
            return message

        async def tracked_send(message: dict[str, Any]) -> None:
            nonlocal response_started
            if message.get("type") == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except _RequestBodyTooLarge:
            if response_started:
                raise
            await self._reject(send, 413, b'{"detail":"Request body too large"}')

    @staticmethod
    async def _reject(
        send: Callable[[dict[str, Any]], Awaitable[None]], status: int, body: bytes
    ) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"cache-control", b"no-store"),
                    (b"connection", b"close"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
