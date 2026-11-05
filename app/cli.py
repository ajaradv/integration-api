"""Admin CLI: python -m app.cli {create-key,revoke-key,list-keys,reprocess}."""

from __future__ import annotations

import argparse
import asyncio
import sys
from uuid import UUID

import sqlalchemy as sa

from app.auth import generate_key
from app.config import get_settings
from app.db import create_engine
from app.errors import ApiError
from app.ingest import operator


async def _create_key(name: str, role: str) -> int:
    plaintext, digest = generate_key()
    engine = create_engine(get_settings(), pool_size=1)
    try:
        async with engine.begin() as conn:
            key_id = (
                await conn.execute(
                    sa.text(
                        "INSERT INTO api_keys (name, key_hash, role) "
                        "VALUES (:n, :h, :r) RETURNING id"
                    ),
                    {"n": name, "h": digest, "r": role},
                )
            ).scalar_one()
    finally:
        await engine.dispose()
    print(f"id:   {key_id}")
    print(f"role: {role}")
    print(f"key:  {plaintext}")
    print("Store this key now; it is shown once and only its hash is kept.", file=sys.stderr)
    return 0


async def _revoke_key(key_id: UUID) -> int:
    engine = create_engine(get_settings(), pool_size=1)
    try:
        async with engine.begin() as conn:
            updated = (
                await conn.execute(
                    sa.text(
                        "UPDATE api_keys SET revoked_at = now() "
                        "WHERE id = :id AND revoked_at IS NULL RETURNING id"
                    ),
                    {"id": key_id},
                )
            ).first()
    finally:
        await engine.dispose()
    if updated is None:
        print("no active key with that id", file=sys.stderr)
        return 1
    print(f"revoked {key_id}")
    return 0


async def _list_keys() -> int:
    engine = create_engine(get_settings(), pool_size=1)
    try:
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    sa.text(
                        "SELECT id, name, role, created_at, revoked_at, last_used_at "
                        "FROM api_keys ORDER BY created_at"
                    )
                )
            ).all()
    finally:
        await engine.dispose()
    for r in rows:
        state = "revoked" if r.revoked_at else "active"
        print(f"{r.id}  {r.role:<8} {state:<8} {r.name}  last_used={r.last_used_at}")
    return 0


async def _reprocess(id_: UUID | None, status: str | None, limit: int) -> int:
    engine = create_engine(get_settings(), pool_size=1)
    try:
        async with engine.begin() as conn:
            if id_ is not None:
                await operator.reprocess(conn, id_, None, source="cli")
                print(f"queued {id_}")
            else:
                assert status is not None
                count = await operator.reprocess_bulk(conn, status, limit, None, source="cli")
                print(f"queued {count}")
    except ApiError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        return 1
    finally:
        await engine.dispose()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create-key", help="create an API key (printed once)")
    create.add_argument("--name", required=True)
    create.add_argument("--role", required=True, choices=["sender", "operator"])
    revoke = sub.add_parser("revoke-key")
    revoke.add_argument("--id", required=True, type=UUID)
    sub.add_parser("list-keys")
    rp = sub.add_parser("reprocess", help="reset ingestions to pending (guarded)")
    group = rp.add_mutually_exclusive_group(required=True)
    group.add_argument("--id", type=UUID)
    group.add_argument("--status", choices=list(operator.REPROCESSABLE))
    rp.add_argument("--limit", type=int, default=100)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "create-key":
        return asyncio.run(_create_key(args.name, args.role))
    if args.command == "revoke-key":
        return asyncio.run(_revoke_key(args.id))
    if args.command == "list-keys":
        return asyncio.run(_list_keys())
    return asyncio.run(_reprocess(args.id, args.status, args.limit))


if __name__ == "__main__":
    sys.exit(main())
