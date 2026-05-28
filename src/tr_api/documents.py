"""Bulk PDF document downloads from Trade Republic.

This is the killer feature `pytr` made famous: download every PDF
TR has issued for your account — trade confirmations, monthly
statements, dividend notices, tax certificates, savings-plan receipts,
card transaction docs — into a tidy folder tree.

How it works:

  1. Walk **both** timeline topics (`timelineTransactions` and
     `timelineActivityLog`) on a SINGLE WebSocket. This is mandatory:
     TR returns 0 items on the second topic if you open a fresh WS.
  2. For each event, fetch its `timelineDetailV2` page (which is where
     the document URLs actually live — the timeline rows themselves
     don't carry them).
  3. Extract document refs via `timeline_detail.extract_documents`.
  4. Classify each ref by (year, kind) using `classify()` — the layout
     is `<out>/<YYYY>/<kind>/<filename>.pdf`.
  5. Download in parallel with bounded concurrency, deduping by file
     path (skip if already on disk → idempotent re-runs).
  6. Write a `manifest.json` at the end with one entry per attempted
     download (path, url, event_id, kind, status, bytes).

The download HTTP itself uses the same `requests.Session` that
`TrClient` uses for the REST API. The document URLs are
`documents.traderepublic.com` signed URLs — short-lived, so we download
immediately rather than collecting them all first.

CLI surface (see cli.py):

    tr-api docs list      [--since YYYY-MM-DD] [--kinds ...]
    tr-api docs download  --out DIR [--since YYYY-MM-DD] [--kinds ...]
                          [--concurrency 4] [--dry-run]

Filenames are stable and human-readable:

    YYYY-MM-DD_<kind>_<short-title>_<short-id>.pdf

where <short-id> is the first 8 chars of the event id (enough for
dedupe; collision-free in practice for accounts with <10M events).
"""
from __future__ import annotations

import asyncio
import json
import re
import unicodedata
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .client import TrClient
from .protocol import TrWebSocket
from .transactions import TOPIC as TX_TOPIC
from .activity_log import TOPIC as AL_TOPIC
from .timeline_detail import (
    TOPIC as DETAIL_TOPIC,
    extract_documents,
)

# Per-WS concurrency for detail fetches AND downloads. TR tolerates this
# fine for short bursts; going higher just risks rate-limits / E frames.
DEFAULT_CONCURRENCY = 4

# Safety cap on pagination per topic. Matches the transactions module.
MAX_PAGES_DEFAULT = 200

# Per-detail/per-download HTTP timeout.
DETAIL_TIMEOUT = 10.0
DOWNLOAD_TIMEOUT = 30.0


# ---------------------------------------------------------------------------
# Classification — eventType → folder kind
# ---------------------------------------------------------------------------
# Layout on disk: <out>/<YYYY>/<kind>/<filename>.pdf
KINDS: tuple[str, ...] = (
    "trades",
    "savings-plans",
    "dividends",
    "tax",
    "statements",
    "card",
    "transfers",
    "interest",
    "corporate-actions",
    "other",
)


def classify(event_type: str | None, doc_title: str = "") -> str:
    """Map an eventType (+ doc title hint) to one of `KINDS`.

    Covers both the 2026-renamed strings (TRADING_*, BANK_TRANSACTION_*,
    SSP_*, CARD_*) and the legacy pytr-era strings, so re-processing old
    accounts still classifies correctly.
    """
    et = (event_type or "").upper()
    title = (doc_title or "").lower()

    # Title heuristics first (statements show up under various eventTypes)
    if "monatsabrechnung" in title or "monthly statement" in title or "depotauszug" in title:
        return "statements"
    if "jahresend" in title or "yearly" in title or "year-end" in title:
        return "statements"

    # By eventType prefix
    if et.startswith("TRADING_SAVINGSPLAN") or et == "SAVINGSPLAN_EXECUTED":
        return "savings-plans"
    if et.startswith("TRADING_") or et in ("TRADE_INVOICE", "ORDER_EXECUTED"):
        return "trades"
    if "DIVIDEND" in et or "CORPORATE_ACTION_CASH" in et:
        return "dividends"
    if "TAX" in et or et == "SSP_TAX_CORRECTION":
        return "tax"
    if "CORPORATE_ACTION" in et:
        return "corporate-actions"
    if et.startswith("CARD_"):
        return "card"
    if et.startswith("BANK_TRANSACTION") or "TRANSFER" in et or "PAYMENT_" in et:
        return "transfers"
    if "INTEREST" in et:
        return "interest"
    return "other"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class DocumentRef:
    """One downloadable PDF, identified before fetching its bytes."""
    event_id: str
    event_type: str
    event_date: str          # ISO-8601 timestamp string from TR
    doc_id: str | None
    title: str
    url: str
    kind: str                # one of KINDS

    @property
    def year(self) -> str:
        ts = self.event_date or ""
        return ts[:4] if len(ts) >= 4 and ts[:4].isdigit() else "unknown"


@dataclass
class DownloadResult:
    ref: DocumentRef
    path: str | None         # final on-disk path (None if dry-run or failed)
    status: str              # "downloaded" | "skipped_existing" | "dry_run" | "error"
    bytes: int = 0
    error: str | None = None


@dataclass
class DownloadReport:
    out_dir: str
    started_at: str
    finished_at: str
    counts: dict[str, int] = field(default_factory=dict)
    results: list[DownloadResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "out_dir": self.out_dir,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "counts": self.counts,
            "results": [
                {
                    **asdict(r.ref),
                    "path": r.path,
                    "status": r.status,
                    "bytes": r.bytes,
                    "error": r.error,
                }
                for r in self.results
            ],
        }


# ---------------------------------------------------------------------------
# Pagination over a single shared WS (the pytr pattern)
# ---------------------------------------------------------------------------
async def _paginate_topic_on_ws(
    ws: TrWebSocket,
    topic: str,
    *,
    since: datetime | None,
    max_pages: int,
) -> list[dict[str, Any]]:
    """Walk one timeline topic on an already-open WS."""
    out: list[dict[str, Any]] = []
    cursor: str | None = None
    pages = 0
    cutoff = since.astimezone(timezone.utc) if (since and since.tzinfo is None) else since

    while True:
        payload = {"type": topic}
        if cursor is not None:
            payload["after"] = cursor
        page = await ws.fetch_one(payload, timeout=DETAIL_TIMEOUT)
        items = page.get("items") or []
        stop = False
        for it in items:
            if cutoff is not None:
                ts = _parse_iso(it.get("timestamp") or it.get("eventTime"))
                if ts is not None and ts < cutoff:
                    stop = True
                    break
            out.append(it)
        if stop:
            return out
        cursor = (page.get("cursors") or {}).get("after")
        pages += 1
        if cursor is None or pages >= max_pages:
            return out


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# DocumentRef collection — combine timeline + detail
# ---------------------------------------------------------------------------
async def _collect_refs(
    cookie_jar: Any,
    *,
    since: datetime | None,
    kinds: set[str] | None,
    concurrency: int,
    max_pages: int,
) -> list[DocumentRef]:
    """Walk both timeline topics on ONE WS, fetch details, return refs."""
    async with TrWebSocket(cookie_jar) as ws:
        # Step 1: collect all events from both topics on this one WS.
        tx_events = await _paginate_topic_on_ws(
            ws, TX_TOPIC, since=since, max_pages=max_pages
        )
        al_events = await _paginate_topic_on_ws(
            ws, AL_TOPIC, since=since, max_pages=max_pages
        )
        events = tx_events + al_events
        # Dedupe by id (events occasionally appear on both topics)
        seen: set[str] = set()
        uniq: list[dict[str, Any]] = []
        for e in events:
            eid = e.get("id")
            if eid and eid not in seen:
                seen.add(eid)
                uniq.append(e)

        # Step 2: fetch detail for each, in parallel on the same WS.
        sem = asyncio.Semaphore(concurrency)
        refs: list[DocumentRef] = []
        lock = asyncio.Lock()

        async def _detail_one(ev: dict[str, Any]) -> None:
            eid = ev.get("id")
            if not eid:
                return
            async with sem:
                try:
                    detail = await ws.fetch_one(
                        {"type": DETAIL_TOPIC, "id": eid}, timeout=DETAIL_TIMEOUT
                    )
                except Exception:
                    return  # silently skip; better than aborting a 5000-event run
            for d in extract_documents(detail):
                if not d.get("url"):
                    continue
                kind = classify(ev.get("eventType"), d.get("title") or "")
                if kinds is not None and kind not in kinds:
                    continue
                ref = DocumentRef(
                    event_id=eid,
                    event_type=ev.get("eventType") or "",
                    event_date=ev.get("timestamp") or ev.get("eventTime") or "",
                    doc_id=d.get("id"),
                    title=d.get("title") or "",
                    url=d["url"],
                    kind=kind,
                )
                async with lock:
                    refs.append(ref)

        await asyncio.gather(*[_detail_one(ev) for ev in uniq])
        return refs


# ---------------------------------------------------------------------------
# Filename sanitization
# ---------------------------------------------------------------------------
_BAD_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(s: str, max_len: int = 40) -> str:
    """Filesystem-safe slug. Keeps ASCII letters/digits/._-; collapses runs."""
    n = unicodedata.normalize("NFKD", s or "")
    n = n.encode("ascii", "ignore").decode("ascii")
    n = _BAD_CHARS.sub("-", n).strip("-_.")
    return (n[:max_len] or "doc").lower()


def filename_for(ref: DocumentRef) -> str:
    """Build the on-disk filename for a DocumentRef.

    Pattern: YYYY-MM-DD_<kind>_<slug-title>_<short-id>.pdf
    """
    date = (ref.event_date or "")[:10] or "unknown"
    short_id = (ref.event_id or "x")[:8]
    title = _slug(ref.title or ref.kind)
    return f"{date}_{ref.kind}_{title}_{short_id}.pdf"


def path_for(ref: DocumentRef, out_dir: Path) -> Path:
    """Full path: <out_dir>/<YYYY>/<kind>/<filename>.pdf"""
    return out_dir / ref.year / ref.kind / filename_for(ref)


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------
def _download_one_sync(session: Any, url: str, dest: Path, timeout: float) -> int:
    """Stream a single URL to dest using a `requests.Session`. Returns bytes written."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    total = 0
    with session.get(url, stream=True, timeout=timeout) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=64 * 1024):
                if chunk:
                    f.write(chunk)
                    total += len(chunk)
    tmp.rename(dest)
    return total


async def _download_all(
    session: Any,
    refs: list[DocumentRef],
    out_dir: Path,
    *,
    concurrency: int,
    dry_run: bool,
) -> list[DownloadResult]:
    sem = asyncio.Semaphore(concurrency)
    results: list[DownloadResult] = []
    lock = asyncio.Lock()

    async def _one(ref: DocumentRef) -> None:
        dest = path_for(ref, out_dir)
        if dry_run:
            res = DownloadResult(ref=ref, path=str(dest), status="dry_run")
        elif dest.exists() and dest.stat().st_size > 0:
            res = DownloadResult(
                ref=ref, path=str(dest), status="skipped_existing",
                bytes=dest.stat().st_size,
            )
        else:
            async with sem:
                try:
                    n = await asyncio.to_thread(
                        _download_one_sync, session, ref.url, dest, DOWNLOAD_TIMEOUT
                    )
                    res = DownloadResult(
                        ref=ref, path=str(dest), status="downloaded", bytes=n
                    )
                except Exception as e:
                    res = DownloadResult(
                        ref=ref, path=None, status="error",
                        error=f"{type(e).__name__}: {e}",
                    )
        async with lock:
            results.append(res)

    await asyncio.gather(*[_one(r) for r in refs])
    return results


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def list_refs(
    client: TrClient,
    *,
    since: datetime | None = None,
    kinds: Iterable[str] | None = None,
    concurrency: int = DEFAULT_CONCURRENCY,
    max_pages: int = MAX_PAGES_DEFAULT,
) -> list[DocumentRef]:
    """Walk the timeline and return all document refs (no download)."""
    kind_set = set(kinds) if kinds else None
    return asyncio.run(
        _collect_refs(
            client.session.cookies,
            since=since,
            kinds=kind_set,
            concurrency=concurrency,
            max_pages=max_pages,
        )
    )


def download_all(
    client: TrClient,
    out_dir: str | Path,
    *,
    since: datetime | None = None,
    kinds: Iterable[str] | None = None,
    concurrency: int = DEFAULT_CONCURRENCY,
    max_pages: int = MAX_PAGES_DEFAULT,
    dry_run: bool = False,
    write_manifest: bool = True,
) -> DownloadReport:
    """Walk the timeline and download every PDF into a year/kind tree.

    Idempotent: re-running skips files already on disk. Failures are
    recorded per-file in the report (no exception raised for one bad URL).

    Layout:  <out_dir>/<YYYY>/<kind>/<filename>.pdf

    Writes <out_dir>/manifest.json unless write_manifest=False.
    """
    out = Path(out_dir).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc).isoformat()

    refs = list_refs(
        client,
        since=since,
        kinds=kinds,
        concurrency=concurrency,
        max_pages=max_pages,
    )

    results = asyncio.run(
        _download_all(
            client.session,
            refs,
            out,
            concurrency=concurrency,
            dry_run=dry_run,
        )
    )

    finished = datetime.now(timezone.utc).isoformat()
    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    counts["total"] = len(results)

    report = DownloadReport(
        out_dir=str(out),
        started_at=started,
        finished_at=finished,
        counts=counts,
        results=results,
    )

    if write_manifest and not dry_run:
        (out / "manifest.json").write_text(
            json.dumps(report.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    return report
