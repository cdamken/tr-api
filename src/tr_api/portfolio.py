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
#
# Note (May 2026, protocol v31): TR removed the unqualified "portfolio" and
# "portfolioAggregateHistory" topics; using either now returns
# BAD_SUBSCRIPTION_TYPE with "Unknown topic type: <name>.31". The working
# replacement for portfolio reads is `compactPortfolio` (flat list of
# positions) or `compactPortfolioByType` (categorised). We default to
# compactPortfolio because its shape matches what we already expose.
TOPIC_PORTFOLIO = "compactPortfolio"
TOPIC_COMPACT_PORTFOLIO = "compactPortfolio"
TOPIC_COMPACT_PORTFOLIO_BY_TYPE = "compactPortfolioByType"
TOPIC_CASH = "cash"
TOPIC_AVAILABLE_CASH_FOR_PAYOUT = "availableCashForPayout"

# Historical/aggregate timeframes per TR. We keep the constant for callers
# even though the topic itself is currently in flux — once we identify
# the replacement, history() will start working again.
HISTORY_RANGES = frozenset({"1d", "5d", "1m", "3m", "1y", "max"})


# ---------------------------------------------------------------------------
# Async core
# ---------------------------------------------------------------------------
async def _fetch(client: TrClient, topic: str, **extra: Any) -> Any:
    payload: dict[str, Any] = {"type": topic, **extra}
    async with TrWebSocket(client.session.cookies) as ws:
        return await ws.fetch_one(payload)


async def _fetch_many(client: TrClient, *payloads: dict[str, Any]) -> list[Any]:
    """Issue multiple subscriptions on a single connection, return results in order.

    We run the fetches **serially** (not via asyncio.gather) because a
    WebSocket has a single reader: two coroutines calling ws.recv() at the
    same time raises websockets' ConcurrencyError. Doing them serially on
    one connection is still significantly faster than reopening a fresh
    WS per topic (most of the cost is the TLS handshake + auth check).

    A future optimisation could implement a dispatcher coroutine that
    reads frames once and demultiplexes by subscription id, allowing real
    concurrency. Not needed for the small number of snapshot topics we
    use today.
    """
    async with TrWebSocket(client.session.cookies) as ws:
        results: list[Any] = []
        for p in payloads:
            results.append(await ws.fetch_one(p))
        return results


# ---------------------------------------------------------------------------
# Sync wrappers — the public API
# ---------------------------------------------------------------------------
def portfolio(client: TrClient) -> dict[str, Any]:
    """Full portfolio: list of positions with current values.

    Topic: compactPortfolio (the v31 replacement for the deprecated
    "portfolio" topic). Returns ``{"positions": [...]}``. Each position
    has at least: instrumentId, netSize (quantity), averageBuyIn,
    netValue, currentPrice. Field set may grow over time; treat unknown
    fields as opaque.
    """
    return asyncio.run(_fetch(client, TOPIC_COMPACT_PORTFOLIO))


def compact_portfolio_by_type(client: TrClient) -> dict[str, Any]:
    """Categorised portfolio view, grouped by asset category.

    Returns ``{"categories": [...], "products": {...}}``. Useful if you
    want stocks/ETFs/crypto buckets without doing the grouping yourself.
    """
    return asyncio.run(_fetch(client, TOPIC_COMPACT_PORTFOLIO_BY_TYPE))


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

    **Currently broken on protocol v31**: TR removed
    `portfolioAggregateHistory` without an obvious drop-in replacement.
    Calling this raises ApiError. We keep the function so callers don't
    have to detect feature absence — once we identify the new topic,
    this becomes wired up again. Filed as a TODO in tr-api.

    timeframe must be one of HISTORY_RANGES: 1d, 5d, 1m, 3m, 1y, max.
    """
    if timeframe not in HISTORY_RANGES:
        raise ValueError(
            f"timeframe must be one of {sorted(HISTORY_RANGES)}, got {timeframe!r}"
        )
    # Best-known candidate; will fail BAD_SUBSCRIPTION_TYPE today but we
    # keep the call so an upgraded TR backend "just works".
    return asyncio.run(_fetch(client, "portfolioAggregateHistory", range=timeframe))


def snapshot(
    client: TrClient,
    *,
    include_history: bool = False,
    history_range: str = "1y",
) -> dict[str, Any]:
    """One-call snapshot for dashboards: portfolio + cash (+ optional history).

    Returns:

        {
          "portfolio": {"positions": [...]},   # compactPortfolio response
          "cash":      [{"currencyId": "EUR", "amount": ...}, ...],
          "history":   {...}   # only if include_history (currently broken in TR)
        }

    Note: include_history defaults to False because TR removed the
    `portfolioAggregateHistory` topic in v31 and we don't have a
    replacement yet. The dashboard's net-worth chart is reconstructed
    locally from daily snapshots instead.
    """
    if include_history and history_range not in HISTORY_RANGES:
        raise ValueError(
            f"history_range must be one of {sorted(HISTORY_RANGES)}, "
            f"got {history_range!r}"
        )

    payloads: list[dict[str, Any]] = [
        {"type": TOPIC_COMPACT_PORTFOLIO},
        {"type": TOPIC_CASH},
    ]
    if include_history:
        payloads.append({"type": "portfolioAggregateHistory", "range": history_range})

    results = asyncio.run(_fetch_many(client, *payloads))
    out: dict[str, Any] = {
        "portfolio": results[0],
        "cash": results[1],
    }
    if include_history:
        out["history"] = results[2]
    return out
