# WebSocket topics

Trade Republic exposes most data over an authenticated WebSocket to
`wss://api.traderepublic.com`. Each piece of data lives under a named
**topic**, which the client subscribes to by sending:

```
sub <id> {"type": "<topic>", ...other args}
```

`tr-api`'s `tr_api.protocol.TrWebSocket` is the low-level transport;
higher-level modules (`portfolio`, `transactions`, …) wrap it.

This doc lists the topics `tr-api` knows about, what each returns, and —
importantly — which topics we **don't yet** wrap and what they would
give us.

---

## Wrapped topics

### `compactPortfolio`

| Wrapper | `tr_api.portfolio.snapshot(client)` |
|---|---|
| Response | `{"positions": [...], "cash": [...]}` |
| Per-position fields | `instrumentId`, `netSize`, `averageBuyIn` |
| Use case | Quick "what do I own" — no instrument names, no live prices |

### `compactPortfolio` + per-ISIN `instrument` + `ticker` (composite)

| Wrapper | `tr_api.portfolio.snapshot_full(client)` |
|---|---|
| What it does | Subscribes to `compactPortfolio`, then fans out to one `instrument` and one `ticker` subscription per held ISIN, all over the same WS connection |
| Per-position fields | `instrumentId`, `name`, `isin`, `netSize`, `averageBuyIn`, `currentPrice` (live) |
| Use case | Anything that renders a portfolio with names and current values — dashboards |

### `timelineTransactions`

| Wrapper | `tr_api.transactions.fetch_all`, `fetch_since`, `fetch_until_id` |
|---|---|
| Topic args | `{"type": "timelineTransactions", "after": <cursor>?}` |
| Response | `{"items": [...], "cursors": {"after": <cursor> \| null}}` |
| Returns event types | `INCOMING_TRANSFER`, `OUTGOING_TRANSFER`, `PAYMENT_INBOUND`, `PAYMENT_OUTBOUND`, `CARD_TRANSACTION`, `INTEREST_PAYOUT`, `TAX_REFUND`, `ssp_tax_correction_invoice`, etc. |
| **Does NOT return** | `TRADE_INVOICE`, `ORDER_EXECUTED`, `DIVIDEND`, `ssp_corporate_action_invoice_cash` (see gap below) |
| Pagination | `cursors.after` → pass back as the next `after`. `null` = end. Pages of ~20–30 items typically. |
| Use case | Cash-flow analytics, card spending, deposits |

See [docs/events.md](events.md) for what each event type means.

---

## Known gap: trades and dividends are on a *different* topic

This is the lesson learned from migrating `trade-republic-dashboard`
from `pytr` to `tr-api`.

[`pytr`](https://github.com/pytr-org/pytr) subscribes to **two**
timeline topics back-to-back:

```python
# pytr/timeline.py — get_next_timeline_transactions() then
# get_next_timeline_activity_log()
async def timeline_transactions(self, after=None):
    return await self.subscribe({"type": "timelineTransactions", "after": after})

async def timeline_activity_log(self, after=None):
    return await self.subscribe({"type": "timelineActivityLog", "after": after})
```

`tr-api`'s `transactions` module currently wraps only the first topic.
For most accounts that means:

| Event class | Where it appears |
|---|---|
| Card spending, transfers in/out, deposits, interest, tax refunds | `timelineTransactions` ✅ (we get these) |
| **Buy / Sell orders** (TRADE_INVOICE, ORDER_EXECUTED) | `timelineActivityLog` ❌ (not wrapped yet) |
| **Dividends and coupon payments** | `timelineActivityLog` ❌ |
| Corporate actions (splits, spinoffs, swaps) | `timelineActivityLog` ❌ |

### Symptom

A fresh `transactions.fetch_all()` on an account with hundreds of trades
returns only a few hundred items, all of them card transactions and
similar. CSV row counts will look something like:

```
319 Removal       (card spending)
  8 Interest
  2 Deposit
  0 Buy           ← missing
  0 Sell          ← missing
  0 Dividend      ← missing
```

The local Dashboard masks this for users who migrated from pytr because
the historical Buy/Sell/Dividend rows from the pytr era stay in
`account_transactions.csv` (incremental merges don't wipe them). New
trades after the pytr→tr-api migration silently stop being collected.

### Fix (shipped in `tr_api.activity_log`)

`tr_api.activity_log` mirrors `transactions.py` but uses
`TOPIC = "timelineActivityLog"`. Same `fetch_all` / `fetch_since` /
`fetch_until_id` surface, same pagination logic, different payload
stream. Downstream callers do:

```python
from tr_api import transactions, activity_log
recent = transactions.fetch_since(client, cutoff) + activity_log.fetch_since(client, cutoff)
```

The two streams are disjoint by `eventType` for most accounts, so naïve
concatenation works. Dedupe on `item['id']` if you want belt-and-braces.

> Reference downstream wiring:
> [`trade-republic-dashboard@4ba866d`](https://github.com/cdamken/trade-republic-dashboard/commit/4ba866d)
> patches `tr_fetch.py::fetch_transactions` to call both modules and
> union their items into `account_transactions.csv`.

---

## Other topics worth knowing about (not yet wrapped)

Discovered by reading pytr's source and TR's web app. Listed here so we
have a single index of "what else TR's WS speaks":

| Topic | Returns | pytr wrapper |
|---|---|---|
| `timelineDetailV2` | Full detail of a single timeline item by id (PDF document refs, fee breakdown, related ISINs) | `pytr/timeline.py::process_timelineDetail` |
| `ticker` | Live last/bid/ask for an ISIN | used by `snapshot_full` |
| `instrument` | Instrument metadata (name, symbol, ISIN, exchange) | used by `snapshot_full` |
| `cash` | Just the cash balances (already inside `compactPortfolio`) | `pytr/portfolio.py` |
| `availableCash` | Free cash for investing | — |
| `searchTags` | User's watchlist tag definitions | `pytr/api.py::search_tags` |
| `watchlists` | Watchlist contents per tag | — |
| `priceForOrder` | Quote a buy/sell order before placing | — |
| `homeInstrumentExchange` | Default exchange for an instrument given user's jurisdiction | — |
| `savingsPlans` | Recurring buy plans | — |
| `compactSavingsPlans` | Lightweight version of the above | — |
| `pendingTimelineEventCash` | Yet-to-settle cash movements | — |

If you need one of these and we don't have it yet: open a PR adding a
module that wraps it. The pattern from `transactions.py` is small
enough to mimic in ~50 lines.

---

## Wire-protocol details

For protocol-level questions (delta encoding, reconnects, errors),
see the docstring at the top of `src/tr_api/protocol.py`. The
relevant TL;DR:

- Text frames only. Format: `<id> <CODE> <payload>`.
- `A` = full answer, `D` = delta (TR's custom tab-separated patch
  format), `C` = close, `E` = error.
- Cookies travel in the upgrade request's `Cookie:` header. No separate
  auth handshake.
- TR closes with code **`3003 (registered)`** when another session
  claims the same registration. See
  [docs/troubleshooting.md](troubleshooting.md).
