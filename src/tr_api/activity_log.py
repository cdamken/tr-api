"""Timeline activity log via TR's `timelineActivityLog` WS topic.

Trade Republic publishes its timeline across **two** parallel topics:

  - `timelineTransactions` (wrapped by `tr_api.transactions`) — cash
    movements: card spending, transfers, deposits, interest, tax refunds.
  - `timelineActivityLog` (wrapped here)                     — instrument
    activity: trade invoices, dividends, corporate actions (splits,
    spinoffs, swaps), savings-plan executions.

Both topics share the same paginated wire shape:

    {
      "items": [
        {"id": "…", "timestamp": "2026-05-20T…", "eventType": "TRADE_INVOICE", …},
        …
      ],
      "cursors": {"after": "<opaque-cursor>" | null}
    }

A null `cursors.after` (or missing) means we've reached the end.

`pytr` (the upstream community client) calls both topics back-to-back and
unions the results in its CSV export — see
`pytr/timeline.py::get_next_timeline_transactions` and
`get_next_timeline_activity_log`. Downstream callers of `tr-api` should do
the same when they want the full picture:

    from tr_api import transactions, activity_log
    tx  = transactions.fetch_all(client)
    act = activity_log.fetch_all(client)
    all_items = tx + act        # dedupe on item['id'] if you keep state

The two streams are disjoint by event type for most accounts (an event
appears on exactly one topic), so naïve concatenation works. The `id`
field is unique per topic+item; cross-topic dedupe is rarely needed.

Why the duplication with `tr_api.transactions`? Each topic gets its own
module so callers can be explicit about which feed they want, and so we
can evolve the per-topic shape (e.g. dataclass output) independently. A
shared `_timeline.py` core might land in a future refactor; this module
is intentionally a structural copy of `transactions.py` for now — review
it side-by-side and the only differences are the TOPIC constant and the
docstrings.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Iterable

from .client import TrClient
from .protocol import TrWebSocket

TOPIC = "timelineActivityLog"

# Safety cap so a broken cursor never spins forever. TR users with very
# long histories of trades should still be well under this.
MAX_PAGES_DEFAULT = 200


# ---------------------------------------------------------------------------
# Async core (same logic as transactions.py — see its docstring)
# ---------------------------------------------------------------------------
async def _paginate(
    cookie_jar: Any,
    *,
    after: str | None,
    stop_predicate: "_StopFn | None",
    max_pages: int,
) -> list[dict[str, Any]]:
    """Walk the paginated activity log, accumulating items until done."""
    items: list[dict[str, Any]] = []
    cursor: str | None = after
    pages = 0

    async with TrWebSocket(cookie_jar) as ws:
        while True:
            payload: dict[str, Any] = {"type": TOPIC}
            if cursor is not None:
                payload["after"] = cursor
            page = await ws.fetch_one(payload)

            page_items = page.get("items") or []
            for it in page_items:
                if stop_predicate is not None and stop_predicate(it):
                    return items
                items.append(it)

            cursor = (page.get("cursors") or {}).get("after")
            pages += 1
            if cursor is None or pages >= max_pages:
                return items


# ---------------------------------------------------------------------------
# Stop-predicate helpers
# ---------------------------------------------------------------------------
_StopFn = "callable[[dict[str, Any]], bool]"


def _parse_ts(item: dict[str, Any]) -> datetime | None:
    """Best-effort parse of TR's timestamp field.

    TR returns ISO-8601 with 'Z' suffix, e.g. '2026-05-20T14:32:11.000Z'.
    Returns None if the field is missing or unparseable.
    """
    ts = item.get("timestamp") or item.get("eventTime")
    if not ts:
        return None
    try:
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _stop_since(cutoff: datetime) -> "_StopFn":
    cutoff = cutoff if cutoff.tzinfo else cutoff.replace(tzinfo=timezone.utc)

    def pred(item: dict[str, Any]) -> bool:
        ts = _parse_ts(item)
        return ts is not None and ts < cutoff

    return pred


def _stop_at_id(known_ids: Iterable[str]) -> "_StopFn":
    known = set(known_ids)

    def pred(item: dict[str, Any]) -> bool:
        return item.get("id") in known

    return pred


# ---------------------------------------------------------------------------
# Sync wrappers — the public API
# ---------------------------------------------------------------------------
def fetch_all(
    client: TrClient,
    *,
    after: str | None = None,
    max_pages: int = MAX_PAGES_DEFAULT,
) -> list[dict[str, Any]]:
    """Fetch every activity-log item (or every one after the given cursor).

    Returns Buy/Sell/Dividend/corporate-action events — anything TR
    classifies as "instrument activity" rather than "cash movement".

    Use sparingly — for an active trader this can be many pages. Prefer
    fetch_since / fetch_until_id for incremental updates.
    """
    return asyncio.run(
        _paginate(
            client.session.cookies,
            after=after,
            stop_predicate=None,
            max_pages=max_pages,
        )
    )


def fetch_since(
    client: TrClient,
    cutoff: datetime,
    *,
    max_pages: int = MAX_PAGES_DEFAULT,
) -> list[dict[str, Any]]:
    """Fetch activity-log items newer than `cutoff` (exclusive).

    Stops paginating as soon as an item with timestamp < cutoff appears.
    Items without a parseable timestamp are kept (better to over-fetch
    than miss a recent event).
    """
    return asyncio.run(
        _paginate(
            client.session.cookies,
            after=None,
            stop_predicate=_stop_since(cutoff),
            max_pages=max_pages,
        )
    )


def fetch_until_id(
    client: TrClient,
    known_ids: Iterable[str],
    *,
    max_pages: int = MAX_PAGES_DEFAULT,
) -> list[dict[str, Any]]:
    """Fetch activity-log items until we hit a known ID.

    Designed for incremental dashboard updates: pass in the IDs you've
    already stored, and this returns just the new ones. Order is
    preserved (newest first, as TR returns them).

    If `known_ids` is empty, behaves like fetch_all().
    """
    if not known_ids:
        return fetch_all(client, max_pages=max_pages)
    return asyncio.run(
        _paginate(
            client.session.cookies,
            after=None,
            stop_predicate=_stop_at_id(known_ids),
            max_pages=max_pages,
        )
    )
