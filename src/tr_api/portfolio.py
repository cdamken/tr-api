"""High-level portfolio fetches via Trade Republic's WebSocket.

This module is the sync-friendly face of the WS protocol. Each public
function:

  1. Opens a WS connection authenticated with the client's cookies.
  2. Subscribes to one or more topics.
  3. Waits for the first Answer frame from each.
  4. Closes the connection.
  5. Returns the merged result.

We never apply deltas here — for one-shot snapshots the initial Answer
is the full state. Streaming/incremental updates would live in a
separate module (not built yet — the dashboard doesn't need it).

The data shape returned by TR is preserved as-is. Callers wanting typed
access can layer their own dataclasses on top.
"""
from __future__ import annotations

import asyncio
from typing import Any

from .client import TrClient
from .protocol import TrWebSocket

# Topic constants — string literals are a footgun; centralize them.
TOPIC_PORTFOLIO = "portfolio"
TOPIC_COMPACT_PORTFOLIO = "compactPortfolio"
TOPIC_CASH = "cash"
TOPIC_AVAILABLE_CASH_FOR_PAYOUT = "availableCashForPayout"
TOPIC_PORTFOLIO_HISTORY = "portfolioAggregateHistory"

# Valid history timeframes per TR.
HISTORY_RANGES = frozenset({"1d", "5d", "1m", "3m", "1y", "max"})


# ---------------------------------------------------------------------------
# Async core
# ---------------------------------------------------------------------------
async def _fetch(client: TrClient, topic: str, **extra: Any) -> Any:
    payload: dict[str, Any] = {"type": topic, **extra}
    async with TrWebSocket(client.session.cookies) as ws:
        return await ws.fetch_one(payload)


async def _fetch_many(client: TrClient, *payloads: dict[str, Any]) -> list[Any]:
    """Issue multiple subscriptions on a single connection, return results in order."""
    async with TrWebSocket(client.session.cookies) as ws:
        # Subscribe to all, then collect answers concurrently.
        return await asyncio.gather(*(ws.fetch_one(p) for p in payloads))


# ---------------------------------------------------------------------------
# Sync wrappers — the public API
# ---------------------------------------------------------------------------
def portfolio(client: TrClient) -> dict[str, Any]:
    """Full portfolio: list of positions with current values.

    Topic: portfolio.
    Returns the raw TR payload (a dict with keys like 'positions',
    'cash', etc. — schema is not officially documented).
    """
    return asyncio.run(_fetch(client, TOPIC_PORTFOLIO))


def cash(client: TrClient) -> Any:
    """Cash balances per currency.

    Topic: cash. Returns a list of dicts like
    [{"currencyId": "EUR", "amount": 1234.56, "inflows": 1234.56, …}, …]
    """
    return asyncio.run(_fetch(client, TOPIC_CASH))


def available_cash_for_payout(client: TrClient) -> Any:
    """Cash that can be withdrawn right now.

    Topic: availableCashForPayout.
    """
    return asyncio.run(_fetch(client, TOPIC_AVAILABLE_CASH_FOR_PAYOUT))


def history(client: TrClient, timeframe: str = "1y") -> dict[str, Any]:
    """Aggregate portfolio value history for the net-worth chart.

    timeframe must be one of HISTORY_RANGES: 1d, 5d, 1m, 3m, 1y, max.

    Topic: portfolioAggregateHistory. Returns
    {"aggregates": [{"time": <ms>, "value": <eur>}, …]}.
    """
    if timeframe not in HISTORY_RANGES:
        raise ValueError(
            f"timeframe must be one of {sorted(HISTORY_RANGES)}, got {timeframe!r}"
        )
    return asyncio.run(_fetch(client, TOPIC_PORTFOLIO_HISTORY, range=timeframe))


def snapshot(
    client: TrClient,
    *,
    include_history: bool = True,
    history_range: str = "1y",
) -> dict[str, Any]:
    """One-call snapshot for dashboards: portfolio + cash + (optional) history.

    All subscriptions run on a single WS connection, so this is roughly
    one round-trip + the latency of the slowest topic. Returns:

        {
          "portfolio": <portfolio payload>,
          "cash": <cash payload>,
          "history": <history payload>,    # only if include_history
        }
    """
    if include_history and history_range not in HISTORY_RANGES:
        raise ValueError(
            f"history_range must be one of {sorted(HISTORY_RANGES)}, "
            f"got {history_range!r}"
        )

    payloads: list[dict[str, Any]] = [
        {"type": TOPIC_PORTFOLIO},
        {"type": TOPIC_CASH},
    ]
    if include_history:
        payloads.append({"type": TOPIC_PORTFOLIO_HISTORY, "range": history_range})

    results = asyncio.run(_fetch_many(client, *payloads))
    out: dict[str, Any] = {
        "portfolio": results[0],
        "cash": results[1],
    }
    if include_history:
        out["history"] = results[2]
    return out
