"""Timeline transactions via TR's `timelineTransactions` WS topic.

TR returns transactions in reverse chronological order, paginated. Each
response looks roughly like:

    {
      "items": [
        {"id": "…", "timestamp": "2026-05-20T…", "eventType": "ORDER_EXECUTED", …},
        …
      ],
      "cursors": {"after": "<opaque-cursor>" | null}
    }

A null `cursors.after` (or missing) means we've reached the end.

Pagination reuses a single WS connection across all pages — opening a
fresh connection per page would be wasteful and TR rate-limits new
connections more aggressively than additional subscriptions on an
existing one.

The public API supports three common patterns:

  fetch_all(client)                    -> all transactions ever
  fetch_since(client, dt)              -> stop once we see an item older than dt
  fetch_until_id(client, last_seen_id) -> stop once we see a known id
                                          (matches the dashboard's incremental
                                          update flow)
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Iterable

from .client import TrClient
from .protocol import TrWebSocket

TOPIC = "timelineTransactions"

# Safety cap so a broken cursor never spins forever. TR users with very
# long histories should still be well under this.
MAX_PAGES_DEFAULT = 200


# ---------------------------------------------------------------------------
# Async core
# ---------------------------------------------------------------------------
async def _paginate(
    cookie_jar: Any,
    *,
    after: str | None,
    stop_predicate: "_StopFn | None",
    max_pages: int,
) -> list[dict[str, Any]]:
    """Walk the paginated timeline, accumulating items until done.

    stop_predicate(item) -> True ends pagination after the current page
    is collected up to (but not including) the matched item. Used by
    fetch_since / fetch_until_id.
    """
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
# Stop-predicate helpers (kept as functions so they're easy to compose/test)
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
        # Normalize trailing Z (Python 3.10 datetime.fromisoformat doesn't
        # accept 'Z' until 3.11; this works on both).
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
        # If we can't parse the timestamp, keep going — better to over-fetch
        # than miss a recent transaction.
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
    """Fetch every transaction (or every one after the given cursor).

    Use sparingly — for a long-running account this can be hundreds of
    pages. Prefer fetch_since / fetch_until_id for incremental updates.
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
    """Fetch transactions newer than `cutoff` (exclusive).

    Stops paginating as soon as an item with timestamp < cutoff appears.
    Items without a parseable timestamp are kept (better to over-fetch
    than miss a recent transaction).
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
    """Fetch transactions until we hit a known ID.

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
