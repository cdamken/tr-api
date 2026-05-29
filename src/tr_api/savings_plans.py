"""Savings-plans read access via TR WebSocket.

A savings plan is a scheduled automatic purchase: "buy €X of ISIN every
month/quarter". TR exposes them via the `savingsPlans` topic.

Each entry has at least:
  - id              : uuid
  - instrumentId    : ISIN being bought
  - amount          : EUR per execution
  - interval        : "weekly" | "biweekly" | "monthly" | "quarterly"
  - startDate       : {type, value, nextExecutionDate}
  - nextExecutionDate
  - paused          : bool
  - fundingCashAccNo / secAccNo

This module is read-only. TR's frontend supports create/edit/cancel
but we haven't validated those endpoints yet — pytr doesn't expose
them either.
"""
from __future__ import annotations

import asyncio
from typing import Any

from .client import TrClient
from .protocol import TrWebSocket

TOPIC = "savingsPlans"


async def _fetch(cookie_jar: Any) -> Any:
    async with TrWebSocket(cookie_jar) as ws:
        return await ws.fetch_one({"type": TOPIC}, timeout=10.0)


def list_plans(client: TrClient) -> list[dict[str, Any]]:
    """Return all savings plans for the account."""
    raw = asyncio.run(_fetch(client.session.cookies))
    if isinstance(raw, dict):
        items = raw.get("savingsPlans") or raw.get("items") or []
    elif isinstance(raw, list):
        items = raw
    else:
        items = []
    return list(items)
