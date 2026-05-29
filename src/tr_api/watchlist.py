"""Watchlist read/add/remove via TR WebSocket.

The TR app's watchlist is a flat list of instrumentIds (ISINs). It's
shared across the app, mobile, and any auth'd session.

Topic shapes:

    {"type": "watchlist"}
        -> {"watchlist": [{"instrumentId": "...", "addedAt": ts}, ...]}

    {"type": "addToWatchlist",     "instrumentId": "ISINxxx"}
        -> {"ok": true} or an error frame
    {"type": "removeFromWatchlist","instrumentId": "ISINxxx"}
        -> ditto

A successful add/remove echoes the new watchlist via a delta on the
'watchlist' subscription (if you have one active). Our wrappers don't
listen for that — they just trust the answer frame.
"""
from __future__ import annotations

import asyncio
from typing import Any

from .client import TrClient
from .protocol import TrWebSocket

TOPIC_LIST = "watchlist"
TOPIC_ADD = "addToWatchlist"
TOPIC_REMOVE = "removeFromWatchlist"


async def _fetch(cookie_jar: Any, payload: dict[str, Any]) -> Any:
    async with TrWebSocket(cookie_jar) as ws:
        return await ws.fetch_one(payload, timeout=10.0)


def list_watchlist(client: TrClient) -> list[dict[str, Any]]:
    """Return the current watchlist as a list of {instrumentId, addedAt, ...} dicts."""
    raw = asyncio.run(_fetch(client.session.cookies, {"type": TOPIC_LIST}))
    if isinstance(raw, dict):
        # TR returns either {"watchlist": [...]} or directly [...]
        items = raw.get("watchlist") or raw.get("items") or []
    elif isinstance(raw, list):
        items = raw
    else:
        items = []
    return list(items)


def add(client: TrClient, instrument_id: str) -> dict[str, Any]:
    """Add an instrument (by ISIN) to the watchlist. Idempotent on TR side."""
    return asyncio.run(_fetch(
        client.session.cookies,
        {"type": TOPIC_ADD, "instrumentId": instrument_id},
    )) or {}


def remove(client: TrClient, instrument_id: str) -> dict[str, Any]:
    """Remove an instrument (by ISIN) from the watchlist."""
    return asyncio.run(_fetch(
        client.session.cookies,
        {"type": TOPIC_REMOVE, "instrumentId": instrument_id},
    )) or {}
