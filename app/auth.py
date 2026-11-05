from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db import get_conn
from app.errors import ApiError

KEY_PREFIX = "pxk_"


@dataclass(frozen=True)
class ApiKey:
    id: UUID
    name: str
    role: str


def hash_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def generate_key() -> tuple[str, str]:
    """Return (plaintext, sha256 hex). 32 random bytes with a recognisable prefix."""
    plaintext = KEY_PREFIX + secrets.token_urlsafe(32)
    return plaintext, hash_key(plaintext)


def require_role(*roles: str) -> Any:
    async def dependency(request: Request, conn: AsyncConnection = Depends(get_conn)) -> ApiKey:
        header = request.headers.get("x-api-key")
        if not header:
            raise ApiError(401, "unauthorized", "missing or invalid API key")
        try:
            row = (
                await conn.execute(
                    sa.text(
                        "SELECT id, name, role FROM api_keys "
                        "WHERE key_hash = :h AND revoked_at IS NULL"
                    ),
                    {"h": hash_key(header)},
                )
            ).first()
        except UnicodeEncodeError:  # non-encodable header text can never be a valid key
            row = None
        if row is None:
            raise ApiError(401, "unauthorized", "missing or invalid API key")
        key = ApiKey(row.id, row.name, row.role)
        if key.role not in roles:
            raise ApiError(403, "forbidden", f"this endpoint requires the {'/'.join(roles)} role")
        touched = await conn.execute(
            sa.text(
                "UPDATE api_keys SET last_used_at = now() WHERE id = :id "
                "AND (last_used_at IS NULL OR last_used_at < now() - interval '60 seconds')"
            ),
            {"id": key.id},
        )
        if touched.rowcount:
            await conn.commit()
        request.state.api_key = key
        return key

    return dependency
