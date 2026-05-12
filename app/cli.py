"""CLI for agent-memory token management.

Usage:
    python -m app.cli create-token --agent anvil --scopes read,write
    python -m app.cli list-tokens
    python -m app.cli revoke-token --id 1
    python -m app.cli setup
"""

import argparse
import asyncio

from app.auth import generate_token, hash_token
from app.config import settings
from app.db import init_pool, close_pool, get_pool


async def _create_token(agent_name: str, scopes: list[str], created_by: str | None = None) -> str:
    raw_token = generate_token()
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO mem_api_tokens (token_hash, agent_name, scopes, created_by) VALUES ($1, $2, $3, $4)",
            hash_token(raw_token),
            agent_name,
            scopes,
            created_by,
        )
    return raw_token


async def _list_tokens() -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, agent_name, scopes, is_active, created_at, last_used_at FROM mem_api_tokens ORDER BY created_at"
        )
    return [dict(r) for r in rows]


async def _revoke_token(token_id: int) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute("UPDATE mem_api_tokens SET is_active = false WHERE id = $1", token_id)
    return result == "UPDATE 1"


async def _run(args: argparse.Namespace) -> None:
    await init_pool()
    try:
        if args.command == "create-token":
            scopes = [s.strip() for s in args.scopes.split(",")]
            raw = await _create_token(args.agent, scopes, created_by="cli")
            print(f"Token created for '{args.agent}' with scopes {scopes}")
            print(f"Token (save this — it cannot be recovered):\n  {raw}")

        elif args.command == "list-tokens":
            tokens = await _list_tokens()
            if not tokens:
                print("No tokens found.")
                return
            print(f"{'ID':<6} {'Agent':<12} {'Scopes':<20} {'Active':<8} {'Last Used':<20}")
            print("-" * 66)
            for t in tokens:
                last = str(t["last_used_at"])[:19] if t["last_used_at"] else "never"
                print(f"{t['id']:<6} {t['agent_name']:<12} {','.join(t['scopes']):<20} {t['is_active']!s:<8} {last:<20}")

        elif args.command == "revoke-token":
            ok = await _revoke_token(args.id)
            print(f"Token {args.id} {'revoked' if ok else 'not found'}.")

        elif args.command == "setup":
            agents = ["claude", "anvil", "codex", "gemini"]
            print("Creating tokens for default agents...\n")
            for agent in agents:
                raw = await _create_token(agent, ["read", "write"], created_by="setup")
                print(f"  {agent}: {raw}")
            print(f"\nSet REQUIRE_AUTH=true in .env to enable authentication.")
    finally:
        await close_pool()


def main():
    parser = argparse.ArgumentParser(description="agent-memory token management")
    sub = parser.add_subparsers(dest="command", required=True)

    ct = sub.add_parser("create-token")
    ct.add_argument("--agent", required=True)
    ct.add_argument("--scopes", default="read,write")

    sub.add_parser("list-tokens")

    rt = sub.add_parser("revoke-token")
    rt.add_argument("--id", type=int, required=True)

    sub.add_parser("setup")

    args = parser.parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
