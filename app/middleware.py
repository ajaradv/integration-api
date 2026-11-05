from __future__ import annotations

import uuid
from typing import Any

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.errors import ApiError


class BodySizeLimitMiddleware:
    """Outermost. Rejects oversized bodies before auth and before buffering them."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k: v for k, v in scope["headers"]}
        declared = headers.get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > self.max_bytes:
            await self._reject(send)
            return
        seen = 0

        async def counting_receive() -> Message:
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.max_bytes:
                    raise ApiError(413, "payload_too_large", self._text())
            return message

        await self.app(scope, counting_receive, send)

    def _text(self) -> str:
        return f"request body exceeds {self.max_bytes} bytes"

    async def _reject(self, send: Send) -> None:
        import json

        body = json.dumps(ApiError(413, "payload_too_large", self._text()).body()).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"connection", b"close"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


class RequestIdMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = ""
        for name, value in scope["headers"]:
            if name == b"x-request-id":
                request_id = value.decode("latin-1")[:128]
        request_id = request_id or str(uuid.uuid4())
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers: list[Any] = list(message.get("headers", []))
                headers.append((b"x-request-id", request_id.encode("latin-1", "replace")))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_id)
