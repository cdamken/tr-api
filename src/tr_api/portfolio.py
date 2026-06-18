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
import re
from typing import Any

from .client import TrClient
from .protocol import TrWebSocket

# Bond-name pattern: TR names fixed-income instruments after their maturity
# month + year, e.g. "Aug 2040", "Januar 2030", "Mar. 2027". Used to decide
# whether to apply the per-100-face-value scaling to the ticker price.
# Mirrors pytr's regex (same approach, same months in EN + DE).
_BOND_NAME_RE = re.compile(
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec|"
    r"January|February|March|April|June|July|August|September|October|November|December|"
    r"Januar|Februar|März|Mai|Juni|Juli|Oktober|Dezember)"
    r"\.?\s+20\d{2}",
    re.IGNORECASE,
)


def _is_bond_name(name: str) -> bool:
    """Heuristic: TR labels fixed-income holdings by their maturity month +
    year, which is how pytr distinguishes them too. Not perfect (an equity
    happening to be named "May 2030 Corp" would match), but cheap and
    catches every government / corporate bond in practice.
    """
    return bool(name) and bool(_BOND_NAME_RE.search(name))

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


def compact_portfolio_by_type(
    client: TrClient,
    *,
    sec_acc_no: str | None = None,
) -> dict[str, Any]:
    """Categorised portfolio view, grouped by asset category.

    Returns ``{"categories": [...]}``. Categories observed in the wild:
    ``stocksAndETFs``, ``cryptos``, ``bonds``, ``privateMarkets``,
    ``others``. Each category has a ``positions: [...]`` list.

    The ``sec_acc_no`` parameter targets a specific securities account
    (the value of ``securitiesAccountNumber`` from
    :func:`tr_api.accounts.account_pairs`). For users with only one
    account it can be omitted — TR returns the default account in that
    case. For multi-account users you must pass the right one explicitly.
    """
    extra: dict[str, Any] = {}
    if sec_acc_no is not None:
        extra["secAccNo"] = sec_acc_no
    return asyncio.run(_fetch(client, TOPIC_COMPACT_PORTFOLIO_BY_TYPE, **extra))


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


async def _snapshot_full_async(
    client: TrClient,
    *,
    timeout: float = 90.0,
) -> dict[str, Any]:
    """Single-connection multi-step fetch — gives back a portfolio with
    names and live prices on every position.

    Sequence on one WS:
      1. compactPortfolio + cash         (2 subs)
      2. instrument per ISIN             (N subs, gives name + exchangeIds)
      3. ticker per "ISIN.EXCHANGE"      (N subs, gives current price)

    Total: 2 + 2N subscriptions over one connection — much cheaper than
    opening many WS sessions (which TR flags as bot-like). Result shape:

        {
          "portfolio": {"positions": [
              {instrumentId, netSize, averageBuyIn,
               name, exchangeIds, currentPrice, ...},
              ...
          ]},
          "cash": <cash payload>,
        }

    instrumentId / netSize / averageBuyIn come from compactPortfolio (as
    strings — TR uses decimal strings to avoid float drift). name and
    exchangeIds come from the instrument topic; currentPrice comes from
    the ticker topic. Positions where instrument or ticker doesn't return
    in time are still present with whatever fields we got — callers
    should treat name/price as optional.
    """
    async with TrWebSocket(client.session.cookies) as ws:
        # Step 1: flat portfolio + categorised portfolio + cash together.
        # TR (v3x) increasingly returns the flat `compactPortfolio` with an
        # EMPTY positions list and only populates the categorised
        # `compactPortfolioByType` (the buckets the mobile "Wealth" screen
        # shows: stocksAndETFs / cryptos / bonds / privateMarkets / others).
        # When the flat list is empty we flatten the categories instead.
        first = await ws.batch_fetch(
            [
                {"type": TOPIC_COMPACT_PORTFOLIO},
                {"type": TOPIC_COMPACT_PORTFOLIO_BY_TYPE},
                {"type": TOPIC_CASH},
            ],
            timeout=15,
        )
        compact = first[0] or {}
        by_type = first[1] or {}
        cash = first[2]

        positions = list(compact.get("positions") or [])
        if not positions:
            for cat in (by_type.get("categories") or []):
                for pos in (cat.get("positions") or []):
                    p = dict(pos)
                    # Enrichment below keys on instrumentId; byType positions
                    # carry the ISIN under `isin` (or instrumentId).
                    p.setdefault("instrumentId", pos.get("instrumentId") or pos.get("isin"))
                    positions.append(p)
        if not positions:
            return {"portfolio": {"positions": []}, "cash": cash}

        isins = [str(p.get("instrumentId") or "") for p in positions]

        # Step 2: instrument details for each ISIN -> name + exchangeIds
        instrument_payloads = [{"type": "instrument", "id": isin} for isin in isins if isin]
        instruments = await ws.batch_fetch(instrument_payloads, timeout=timeout)
        instrument_by_isin: dict[str, dict[str, Any]] = {}
        for isin, inst in zip([i for i in isins if i], instruments):
            if isinstance(inst, dict):
                instrument_by_isin[isin] = inst

        # Step 3: ticker per ISIN+exchange -> currentPrice
        ticker_payloads: list[dict[str, Any]] = []
        ticker_keys: list[str] = []   # parallel list to ticker_payloads, indexed by isin
        for isin in isins:
            inst = instrument_by_isin.get(isin) or {}
            exchanges = inst.get("exchangeIds") or inst.get("exchanges") or []
            # exchangeIds is usually a list of strings like ["LSX", "TDG", ...].
            # Pick the first; TR sorts them by preference for the user's jurisdiction.
            exchange = None
            if isinstance(exchanges, list) and exchanges:
                first_ex = exchanges[0]
                if isinstance(first_ex, str):
                    exchange = first_ex
                elif isinstance(first_ex, dict):
                    exchange = first_ex.get("slug") or first_ex.get("id")
            if isin and exchange:
                ticker_payloads.append({"type": "ticker", "id": f"{isin}.{exchange}"})
                ticker_keys.append(isin)

        ticker_results = await ws.batch_fetch(ticker_payloads, timeout=timeout)
        ticker_by_isin: dict[str, dict[str, Any]] = {}
        for isin, tick in zip(ticker_keys, ticker_results):
            if isinstance(tick, dict):
                ticker_by_isin[isin] = tick

        # Merge everything back into positions
        merged: list[dict[str, Any]] = []
        for raw in positions:
            isin = str(raw.get("instrumentId") or "")
            inst = instrument_by_isin.get(isin) or {}
            tick = ticker_by_isin.get(isin) or {}
            # Pull a sensible "name" out of the instrument record. TR puts
            # the display name in different places depending on instrument
            # type (shortName for stocks/ETFs, name elsewhere; derivatives
            # have it nested under derivativeInfo).
            name = (
                inst.get("shortName")
                or inst.get("name")
                or (inst.get("derivativeInfo") or {}).get("underlying", {}).get("shortName")
                or ""
            )
            current_price = None
            bid = tick.get("bid") if isinstance(tick, dict) else None
            ask = tick.get("ask") if isinstance(tick, dict) else None
            last = tick.get("last") if isinstance(tick, dict) else None
            # `bid`/`ask`/`last` are typically {"price": "12.34", ...}.
            if isinstance(last, dict) and last.get("price") is not None:
                current_price = last.get("price")
            elif isinstance(bid, dict) and bid.get("price") is not None:
                current_price = bid.get("price")
            elif isinstance(ask, dict) and ask.get("price") is not None:
                current_price = ask.get("price")

            # Bond price scaling: TR ticker quotes bonds as a percentage of
            # face value (so a price of "61.31" means 61.31% of nominal,
            # i.e. €0.6131 per unit). Apply the /100 fix here so the caller
            # can do plain `price × qty` and get the right netValue. This
            # matches what pytr does in portfolio.py:179.
            if current_price is not None and _is_bond_name(name):
                try:
                    current_price = str(float(current_price) / 100.0)
                except (TypeError, ValueError):
                    pass

            merged.append({
                **raw,
                "isin": isin,
                "name": name,
                "exchangeIds": inst.get("exchangeIds") or [],
                "currentPrice": current_price,
                "isBond": _is_bond_name(name),
            })

        return {
            "portfolio": {"positions": merged},
            "cash": cash,
        }


def snapshot_full(client: TrClient, *, timeout: float = 90.0) -> dict[str, Any]:
    """Sync wrapper for the full multi-subscription portfolio fetch.

    Use this when you want names + live prices for every position. Slower
    than snapshot() (one WS but 2N+2 subs), but the result is what a
    dashboard actually needs to render rows.
    """
    return asyncio.run(_snapshot_full_async(client, timeout=timeout))


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
