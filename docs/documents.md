# Documents — bulk PDF downloads

`tr_api.documents` walks your timeline and downloads every PDF Trade
Republic has issued for your account: trade confirmations, monthly
statements, dividend notices, tax certificates, savings-plan receipts,
corporate-action letters.

This is the feature `pytr` made famous (`pytr dl_docs`). We do it on
the same single-WebSocket pattern, but with newer event-type vocabulary
and a cleaner output layout.

## Quickstart

```bash
# Dry run — see what would be downloaded
tr-api docs download --out ~/tr-docs --dry-run

# Real download
tr-api docs download --out ~/tr-docs

# Only this year, only trades + dividends
tr-api docs download --out ~/tr-docs --since 2026-01-01 --kinds trades,dividends
```

## Output layout

```
~/tr-docs/
├── 2024/
│   ├── statements/
│   │   └── 2024-12-31_statements_monthly-statement_a1b2c3d4.pdf
│   ├── trades/
│   ├── dividends/
│   ├── tax/
│   ├── savings-plans/
│   ├── card/
│   ├── transfers/
│   ├── interest/
│   ├── corporate-actions/
│   └── other/
├── 2025/
│   └── …
├── 2026/
│   └── …
└── manifest.json
```

Filenames: `YYYY-MM-DD_<kind>_<slug-title>_<short-id>.pdf`

- Date is the event date (when TR registered the operation).
- `<kind>` is the same kind as the parent folder, for grep-ability when
  files are moved out.
- `<slug-title>` is the document title from TR, ASCII-slugified.
- `<short-id>` is the first 8 chars of the event id — enough for dedupe.

## Kinds

| Kind | Triggered by (modern eventType) | What's in it |
|---|---|---|
| `trades` | `TRADING_TRADE_EXECUTED`, `TRADING_*` | Trade confirmations ("Abrechnung") |
| `savings-plans` | `TRADING_SAVINGSPLAN_EXECUTED` | Monthly savings-plan receipts |
| `dividends` | `SSP_CORPORATE_ACTION_CASH` | Dividend notices |
| `tax` | `SSP_TAX_CORRECTION`, anything with `TAX` | Tax certificates, year-end recap |
| `statements` | Title contains "Monatsabrechnung" / "monthly statement" / "Depotauszug" | Monthly portfolio statements |
| `card` | `CARD_*` | Card transaction confirmations (when TR issues a PDF) |
| `transfers` | `BANK_TRANSACTION_*`, `*TRANSFER*`, `PAYMENT_*` | SEPA confirmations |
| `interest` | `INTEREST_PAYOUT*` | Interest payout notices |
| `corporate-actions` | `CORPORATE_ACTION` (non-cash) | Stock splits, spin-offs, voting docs |
| `other` | Everything else | Catch-all so nothing is dropped |

Use `--kinds trades,tax` to download only certain categories.

## Manifest

`manifest.json` at the root of `--out` lists every attempted download
with status (`downloaded`, `skipped_existing`, `dry_run`, `error`),
on-disk path, byte count, and the original event metadata. Useful for:

- Audit ("which trades got PDFs and which didn't?")
- Idempotency check (re-running skips files already present)
- Diffing two runs (what changed since last month?)

## How it works

1. Open **one** WebSocket.
2. Paginate `timelineTransactions` and `timelineActivityLog` on it
   (the mandatory pytr pattern — opening two WSs returns 0 items on
   the second).
3. For each event, call `timelineDetailV2` (also on the same WS) and
   extract document refs from the `documents` section.
4. Hand the refs to a parallel downloader that uses the same
   `requests.Session` that holds your auth cookies.
5. Stream to `.part` then rename, so a killed run never leaves a
   half-written PDF.

## Tuning

- `--concurrency N` — both for detail fetches AND HTTP downloads.
  Default 4. Going above ~8 sometimes triggers TR rate limits.
- `--since YYYY-MM-DD` — stops the timeline walk early. Pair with a
  weekly cron for incremental backups.

## Idempotency

Re-running `docs download` against the same `--out` only re-downloads
files that are missing or zero-byte. Safe to run weekly or daily.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `3003 (registered)` when starting | Another TR session is active (mobile app, another script). Close it. |
| Many `error` entries with `403`/`404` | URL signing window expired; just re-run, fresh signatures will be issued. |
| 0 items collected | Cookies expired — `tr-api auth login` and retry. |
| Some events have no documents | Normal. Only some event types carry PDFs (card spending typically doesn't). |

## Programmatic use

```python
from tr_api import TrClient, documents, profiles

with TrClient(profiles.get_active()) as c:
    report = documents.download_all(
        c,
        out_dir="~/tr-docs",
        since=None,                     # or datetime(2026, 1, 1, tzinfo=...)
        kinds=["trades", "dividends"],  # or None for all
        concurrency=4,
        dry_run=False,
    )
print(report.counts)   # {'downloaded': 487, 'skipped_existing': 12, 'total': 499}
```
