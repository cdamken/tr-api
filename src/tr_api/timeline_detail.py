"""Timeline event detail via TR's `timelineDetailV2` WS topic.

Each event returned by `timelineTransactions` or `timelineActivityLog`
carries an `id`. Sending `{"type": "timelineDetailV2", "id": "<id>"}`
on a WebSocket returns the **full** detail page TR's mobile app shows
when you tap the row — which includes one or more **document links**
(monthly statement PDFs, trade confirmations, dividend notices, tax
documents, etc.).

This module is intentionally a thin wrapper:

  fetch(client, event_id)          -> dict             # one detail
  fetch_many(client, event_ids)    -> dict[id, dict]   # batched on one WS

The batched variant reuses a single `TrWebSocket` across all ids so we
don't pay the connect/handshake cost N times. It also caps concurrent
in-flight subscriptions to avoid hammering TR (see `_DEFAULT_CONCURRENCY`).

The shape of a typical answer (trimmed):

    {
      "id": "...",
      "subtitleText": "Trade • AAPL",
      "timestamp": "2026-04-15T14:32:11.000Z",
      "sections": [
        {"type": "header", "data": {...}},
        {"type": "table", "data": {"sections": [...]}},  # fees, ISIN, count
        {"type": "documents", "data": {                  # ← the prize
          "title": "Documents",
          "data": [
            {"id": "doc-uuid", "title": "Abrechnung",
             "action": {"type": "browserModal",
                        "payload": "https://documents.traderepublic.com/..."}},
            ...
          ]
        }},
        ...
      ]
    }

Extracting the document URLs is left to higher-level callers (the
`documents` module). We just return the raw detail and let them parse.
"""
from __future__ import annotations

import asyncio
from typing import Any, Iterable

from .client import TrClient
from .protocol import TrWebSocket

TOPIC = "timelineDetailV2"

# Concurrent in-flight detail subscriptions per WS. TR tolerates this
# fine for short bursts; going higher just risks an `E` frame.
_DEFAULT_CONCURRENCY = 8

# Per-detail timeout. The mobile app feels instant; 10s is generous.
_DEFAULT_TIMEOUT = 10.0


# ---------------------------------------------------------------------------
# Async core
# ---------------------------------------------------------------------------
async def _fetch_on_ws(
    ws: TrWebSocket,
    event_id: str,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Single-shot detail fetch on an already-open WS."""
    return await ws.fetch_one({"type": TOPIC, "id": event_id}, timeout=timeout)


async def _fetch_many_on_ws(
    ws: TrWebSocket,
    event_ids: Iterable[str],
    *,
    concurrency: int = _DEFAULT_CONCURRENCY,
    timeout: float = _DEFAULT_TIMEOUT,
) -> dict[str, dict[str, Any]]:
    """Fetch many details on one WS with bounded concurrency.

    Returns a dict {event_id: detail}. Failures per-id are swallowed and
    appear as a `{"error": "..."}` entry instead of raising — so a single
    bad id doesn't blow up a batch of 5000.
    """
    sem = asyncio.Semaphore(concurrency)
    out: dict[str, dict[str, Any]] = {}

    async def _one(eid: str) -> None:
        async with sem:
            try:
                out[eid] = await _fetch_on_ws(ws, eid, timeout=timeout)
            except Exception as e:
                out[eid] = {"error": f"{type(e).__name__}: {e}"}

    await asyncio.gather(*[_one(eid) for eid in event_ids])
    return out


async def _fetch_many_async(
    cookie_jar: Any,
    event_ids: Iterable[str],
    *,
    concurrency: int = _DEFAULT_CONCURRENCY,
    timeout: float = _DEFAULT_TIMEOUT,
) -> dict[str, dict[str, Any]]:
    async with TrWebSocket(cookie_jar) as ws:
        return await _fetch_many_on_ws(
            ws, event_ids, concurrency=concurrency, timeout=timeout
        )


# ---------------------------------------------------------------------------
# Sync wrappers — the public API
# ---------------------------------------------------------------------------
def fetch(client: TrClient, event_id: str, *, timeout: float = _DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Fetch the detail page for a single timeline event."""
    async def _go() -> dict[str, Any]:
        async with TrWebSocket(client.session.cookies) as ws:
            return await _fetch_on_ws(ws, event_id, timeout=timeout)

    return asyncio.run(_go())


def fetch_many(
    client: TrClient,
    event_ids: Iterable[str],
    *,
    concurrency: int = _DEFAULT_CONCURRENCY,
    timeout: float = _DEFAULT_TIMEOUT,
) -> dict[str, dict[str, Any]]:
    """Fetch details for many events on one shared WS.

    Returns {event_id: detail_or_error}. Per-id failures are recorded
    as `{"error": "..."}` rather than raising.
    """
    ids = list(event_ids)
    if not ids:
        return {}
    return asyncio.run(
        _fetch_many_async(
            client.session.cookies,
            ids,
            concurrency=concurrency,
            timeout=timeout,
        )
    )


# ---------------------------------------------------------------------------
# Helpers for downstream callers — pure functions over the detail dict
# ---------------------------------------------------------------------------
def extract_documents(detail: dict[str, Any]) -> list[dict[str, Any]]:
    """Pull out the document refs from a `timelineDetailV2` answer.

    Returns a list of {"id", "title", "url", "detail_url"} dicts (some
    may be empty if TR's payload skips a field). Detail entries with no
    documents section return [].

    The URL extraction handles TR's two action shapes:
      • {"type": "browserModal", "payload": "https://..."}
      • {"type": "documentView",  "payload": "https://..."}
    Both point at a signed `documents.traderepublic.com` URL.
    """
    sections = detail.get("sections") or []
    out: list[dict[str, Any]] = []
    for section in sections:
        if section.get("type") != "documents":
            continue
        data = section.get("data") or {}
        # Two layouts seen in the wild: {"data": [...]} or just [...]
        items = data.get("data") if isinstance(data, dict) else data
        for item in items or []:
            action = item.get("action") or {}
            url = action.get("payload") if isinstance(action.get("payload"), str) else None
            out.append(
                {
                    "id": item.get("id"),
                    "title": item.get("title") or "",
                    "url": url,
                    "detail_url": item.get("detail"),
                }
            )
    return out
