# CLAUDE.md — tr-api

> Context for AI assistants. Humans: see [README.md](README.md).

## What this is

`tr-api` is the **canonical Python library** for talking to Trade
Republic's backend (REST + WebSocket). Two downstream projects depend on
it:

- [`Trade-Republic-Dashboard`](https://github.com/cdamken/trade-republic-dashboard) — local single-user dashboard
- [`Trade-Republic-owncloud`](https://github.com/cdamken/trade-republic-owncloud) — multi-user ownCloud port

**This repo is upstream.** Any change that touches the TR protocol
(endpoints, event schemas, auth flow, WS topics) lands here first, then
the downstreams adopt it.

## Two-mode auth

The library supports both, side-by-side:

1. **Cookie-import** (`tr_api.cookies.import_from_chrome`) — read TR's
   session cookies from a real Chrome on the user's machine via
   `pycookiecheat`. No Playwright at all. Used on workstations.
2. **Programmatic login** (`tr_api.auth.initiate_login` /
   `complete_login`) — phone+PIN, with `tr_api.waf.get_waf_token` running
   the AWS WAF challenge under Playwright. Used on headless servers
   (ownCloud, CI).

The README and `docs/auth-modes.md` cover the trade-offs.

## Two WS timeline topics (the gotcha)

TR splits the timeline across **two parallel topics**:

| Topic | What it returns |
|---|---|
| `timelineTransactions` | Cash movements: card spending, deposits, transfers, interest, tax refunds, trades, dividends, savings-plan executions, corporate actions |
| `timelineActivityLog` | Order lifecycle and informational events: order created/cancelled/expired, corporate-action notifications, chat, documents (NOT cash events for most accounts) |

`pytr` subscribes to both back-to-back on **one WebSocket**. So do we
(see `Trade-Republic-Dashboard/app/tr_fetch.py::_paginate_topic_on_ws`).

**Critical**: when you open TWO separate WS connections (one per topic),
TR returns 0 items on the second. Pytr's one-WS pattern is mandatory.

`tr_api.transactions` and `tr_api.activity_log` give you per-topic
fetch_all/fetch_since helpers, but each opens its own WS — fine for
single-topic use, broken for combined fetches. Downstream apps
implement the combined fetch themselves to share one socket.

## eventType vocabulary (2026 rename)

TR renamed almost every eventType during 2026. The pytr-era strings
(`INCOMING_TRANSFER`, `TRADE_INVOICE`, `DIVIDEND`, etc.) no longer
appear on live responses. Current strings use uppercase prefixes:

| Prefix | Examples | Mapped category |
|---|---|---|
| `TRADING_` | `TRADING_SAVINGSPLAN_EXECUTED`, `TRADING_TRADE_EXECUTED` | Buy/Sell |
| `BANK_TRANSACTION_` | `BANK_TRANSACTION_INCOMING`, `BANK_TRANSACTION_OUTGOING*` | Deposit / Withdrawal |
| `SSP_` | `SSP_CORPORATE_ACTION_CASH`, `SSP_TAX_CORRECTION` | Dividend / Tax Refund |
| `CARD_` | `CARD_TRANSACTION`, `CARD_REFUND` | Removal / Deposit |

Full catalogue: `docs/events.md`. The map lives in downstream apps'
`EVENT_TYPE_MAP` — keep it in sync between both.

## Repo layout

```
src/tr_api/
├── __init__.py        ← public re-exports
├── account.py         ← /api/v2/auth/account + ping
├── activity_log.py    ← timelineActivityLog topic (Phase 12+)
├── auth.py            ← initiate_login / complete_login (programmatic mode)
├── cli.py             ← `tr-api ...` command
├── client.py          ← TrClient (authenticated REST)
├── cookies.py         ← import_from_chrome / save / load / validate
├── exceptions.py      ← hierarchy: TrApiError → AuthError / ApiError / ...
├── portfolio.py       ← snapshot() and snapshot_full() (WS)
├── profiles.py        ← multi-account profile management
├── protocol.py        ← TrWebSocket (async, low-level)
├── transactions.py    ← timelineTransactions topic
└── waf.py             ← AWS WAF token via Playwright
docs/
├── auth-modes.md          ← cookie-import vs programmatic-login
├── cli-contract.md        ← CLI surface
├── events.md              ← eventType vocabulary
├── troubleshooting.md     ← 3003 registered, 401, 405, 429, etc.
└── websocket-topics.md    ← timelineTransactions vs timelineActivityLog gap
```

## Workflow rules (read these before changing code)

1. **Don't break the public surface.** `tr_api.transactions`,
   `tr_api.activity_log`, `tr_api.portfolio`, `tr_api.auth` are used by
   downstreams. If you must change a function signature, do it as an
   add (new function) + deprecate, not a rename.
2. **Event-type changes**: if TR adds a new eventType, document it in
   `docs/events.md` AND update both downstream `EVENT_TYPE_MAP`s in
   the same commit cycle. Mismatch = silently dropped data.
3. **Tests**: this repo doesn't have a test suite yet. Validate
   manually by running the downstream Dashboard's
   `app/tr_fetch.py --non-interactive --full` and counting rows in the
   resulting CSV.

## Recently resolved investigations

- **2026-05-28**: "fetch_all returns only 330 items / no trades" was
  the 2026 eventType rename — old map matched only ~5% of events. Fixed
  in downstream `EVENT_TYPE_MAP`. See `docs/events.md`.
- **2026-05-28**: Single-WS pattern matters. Two-WS gives 0 items on
  the second topic. Downstream's `_paginate_topic_on_ws` is the
  reference implementation.
