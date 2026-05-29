"""Price alarms (price alerts) via TR WebSocket.

A price alarm tells TR to notify you when a given instrument's price
crosses a threshold. Each alarm has an id, instrumentId, targetPrice,
and a status (active / triggered / cancelled).

Topics:
  {"type": "priceAlarms"}
      -> [ {id, instrumentId, status, createdPrice, targetPrice, ...}, ... ]
  {"type": "createPriceAlarm", "instrumentId": "ISIN", "targetPrice": "12.34"}
      -> {alarm: {...}}  (the new alarm)
  {"type": "cancelPriceAlarm", "id": "<uuid>"}
      -> {} or error
"""
from __future__ import annotations

import asyncio
from typing import Any

from .client import TrClient
from .protocol import TrWebSocket

TOPIC_LIST = "priceAlarms"
TOPIC_CREATE = "createPriceAlarm"
TOPIC_CANCEL = "cancelPriceAlarm"


async def _fetch(cookie_jar: Any, payload: dict[str, Any]) -> Any:
    async with TrWebSocket(cookie_jar) as ws:
        return await ws.fetch_one(payload, timeout=10.0)


def list_alarms(client: TrClient) -> list[dict[str, Any]]:
    """Return all price alarms for the account."""
    raw = asyncio.run(_fetch(client.session.cookies, {"type": TOPIC_LIST}))
    if isinstance(raw, dict):
        items = raw.get("alarms") or raw.get("items") or []
    elif isinstance(raw, list):
        items = raw
    else:
        items = []
    return list(items)


def create(
    client: TrClient,
    instrument_id: str,
    target_price: float | str,
) -> dict[str, Any]:
    """Create a price alarm. TR expects target_price as a string (numeric)."""
    payload = {
        "type": TOPIC_CREATE,
        "instrumentId": instrument_id,
        "targetPrice": str(target_price),
    }
    return asyncio.run(_fetch(client.session.cookies, payload)) or {}


def cancel(client: TrClient, alarm_id: str) -> dict[str, Any]:
    """Cancel a price alarm by its TR id."""
    return asyncio.run(_fetch(
        client.session.cookies,
        {"type": TOPIC_CANCEL, "id": alarm_id},
    )) or {}
