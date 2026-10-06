"""Replay receipts must use the same transaction as the ingested records."""
import json


async def claim_receipt(conn, route: str, ingest_id: str | None):
    if not ingest_id:
        return None
    claimed = await conn.fetchval(
        "INSERT INTO mem_ingest_receipts(route, ingest_id) VALUES ($1,$2) "
        "ON CONFLICT DO NOTHING RETURNING ingest_id", route, ingest_id,
    )
    if claimed:
        return None
    raw = await conn.fetchval(
        "SELECT response FROM mem_ingest_receipts WHERE route=$1 AND ingest_id=$2",
        route, ingest_id,
    )
    if raw is None:
        raise RuntimeError("Incomplete ingest receipt")
    return json.loads(raw) if isinstance(raw, str) else raw


async def finish_receipt(conn, route: str, ingest_id: str | None, response: dict):
    if ingest_id:
        await conn.execute(
            "UPDATE mem_ingest_receipts SET response=$3::jsonb WHERE route=$1 AND ingest_id=$2",
            route, ingest_id, json.dumps(response),
        )
