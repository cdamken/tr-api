# tr-api

Minimal Python client for the **Trade Republic** (Germany) backend API.

`tr-api` exists because [`pytr`](https://github.com/pytr-org/pytr)'s
Playwright-based WAF bypass started getting rate-limited in mid-2026. We
went down two paths:

1. **Cookie-import mode** — reuse the cookies from the user's real Chrome,
   where the AWS WAF challenge has already been solved by an actual
   browser with a real fingerprint TR trusts. No headless browser for
   day-to-day API calls.
2. **Programmatic login mode** — for headless servers (CI, ownCloud,
   anything without a desktop Chrome). Uses Playwright **only** to fetch
   one WAF token, then handles the 4-digit MFA push challenge against
   `/api/v1/auth/web/login` directly.

Both modes leave you with the same per-profile cookie jar that powers all
subsequent API calls.

---

## 🎯 What it does

- 🔐 **Two auth modes** — cookie-import from real Chrome (the original
  pitch) **or** programmatic phone+PIN+MFA login on a headless host
  (added in Phase 12). See [docs/auth-modes.md](docs/auth-modes.md).
- 📋 Authenticated REST endpoints (`account.summary`, etc.).
- 📊 Portfolio over the WebSocket (`portfolio.snapshot_full` returns
  positions with names + live prices in one round-trip).
- 📜 Transactions timeline (`transactions.fetch_all` / `fetch_since` /
  `fetch_until_id`). **Known gap**: only covers the
  `timelineTransactions` topic; the trade/dividend feed lives on
  `timelineActivityLog` — see [docs/websocket-topics.md](docs/websocket-topics.md).
- 🔁 Auto-refresh session via `/auth/web/session`.
- 👥 Multi-account: each TR phone number gets its own profile on disk.
- 🛠️ CLI (`tr-api …`) covering all of the above; see
  [docs/cli-contract.md](docs/cli-contract.md).

## 🙅 What it doesn't do

- ❌ Solve the AWS WAF JS challenge purely in Python — Playwright runs
  it for us (cookie-import mode skips this; programmatic-login uses it).
- ❌ Device pairing / ECDSA login (the deprecated pre-PIN flow).
- ❌ Parse every kind of timeline event — only the common dashboard
  ones. See [docs/events.md](docs/events.md) for the catalogue.

---

## 📐 Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│  Auth — pick one                                                    │
│                                                                     │
│  ┌───────────────────────────┐    ┌──────────────────────────────┐  │
│  │  Cookie-import (Chrome)   │    │  Programmatic login          │  │
│  │  cookies.import_from_     │    │  auth.initiate_login(phone,  │  │
│  │  chrome() → cookies.txt   │    │       pin)  → push 4-digit   │  │
│  │                           │    │       code to TR mobile app  │  │
│  │  Needs the user logged    │    │  auth.complete_login(        │  │
│  │  into app.traderepublic   │    │       processId, code)       │  │
│  │  .com in real Chrome.     │    │       → harvest cookies      │  │
│  └─────────────┬─────────────┘    └────────────────┬─────────────┘  │
│                │                                   │                │
│                └────────────────┬──────────────────┘                │
│                                 ▼                                   │
│              ~/.tr-api/profiles/<phone>/cookies.txt                 │
└────────────────────────────────┬────────────────────────────────────┘
                                 │ (Mozilla cookie jar)
                                 ▼
┌─────────────────────────────────────────────────────────────────────┐
│  TrClient — authenticated REST (api.traderepublic.com)              │
│                                                                     │
│   c = TrClient.from_active()                                        │
│   c.get_json("/api/v2/auth/account")                                │
│                                                                     │
│  Sends Origin, Referer, Sec-Fetch-* and a real-Chrome UA so TR      │
│  doesn't flag us. Raises SessionExpired on 401 — caller re-auths.   │
└────────────────────────────────┬────────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────────┐
│  TrWebSocket — async WS subscriptions (api.traderepublic.com)       │
│                                                                     │
│   portfolio.snapshot_full(client)   → portfolio + cash + tickers    │
│   transactions.fetch_all(client)    → timelineTransactions paginated│
│                                                                     │
│  Cookies travel in the upgrade request's Cookie: header. TR's wire  │
│  protocol is documented in src/tr_api/protocol.py.                  │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 🚀 Quick start

### Cookie-import mode (workstation with Chrome)

```bash
# 1. Install
pipx install "tr-api @ git+https://github.com/cdamken/tr-api.git"

# 2. Log in to TR in Chrome normally (one-time)
open "https://app.traderepublic.com"

# 3. Import cookies into a profile and make it active
tr-api profiles add        # imports cookies from Chrome

# 4. Use it
tr-api portfolio
tr-api transactions --since 7d
```

### Programmatic-login mode (headless server)

```bash
# 1. Install with the browser extra (pulls Playwright)
pipx install "tr-api[browser] @ git+https://github.com/cdamken/tr-api.git"
playwright install chromium

# 2. Log in once (TR pushes a 4-digit code to your mobile app)
tr-api login --phone +491701234567 --pin 1234
# >> Push sent. Enter code: 5678
# >> Saved cookies to ~/.tr-api/profiles/+491701234567/cookies.txt
# >> Active profile set.

# 3. Use it
tr-api portfolio
```

Programmatic from Python (what dashboards do):

```python
from tr_api import (
    profiles, TrClient,
    portfolio as p_mod,
    transactions as t_mod,
    auth,
)

# First-time login (per-account, one-shot)
init = auth.initiate_login("+491701234567", "1234")
# … prompt the user for the 4-digit code that TR just pushed …
result = auth.complete_login(init.process_id, "5678")

# Save and activate
prof = profiles.create("+491701234567")
profiles.set_active(prof.phone)
from tr_api import cookies as c
c.save_to_file(result.cookies, prof.cookies_file)

# Now use the API
client = TrClient(prof)
snap = p_mod.snapshot_full(client)
txs  = t_mod.fetch_all(client)
```

---

## 🧠 Multi-account support

A "profile" is one Trade Republic account, identified by phone number
(E.164 format, e.g. `+491701234567`). PIN is never stored on disk;
authentication is entirely cookie-based after the initial login.

```
~/.tr-api/
├── active                  ← text file with the active profile phone
└── profiles/
    ├── +4912345678/
    │   ├── meta.json       ← phone, jurisdiction, friendly name
    │   └── cookies.txt     ← Mozilla cookie jar (HttpOnly cookies included)
    └── +4998765432/
        ├── meta.json
        └── cookies.txt
```

CLI:

```bash
tr-api profiles list
tr-api profiles add               # import cookies from Chrome (cookie-import)
tr-api login --phone … --pin …    # programmatic login (writes cookies.txt)
tr-api profiles use <phone>       # switch active profile
tr-api profiles remove <phone>    # delete a profile entirely
```

---

## 📦 Module layout

| Module | What it does |
|---|---|
| `tr_api.client` | `TrClient` — authenticated REST session over `api.traderepublic.com`. CSRF-correct headers, raises `SessionExpired` on 401. |
| `tr_api.protocol` | `TrWebSocket` — async client for TR's text-over-WS protocol (subscribe / answer / delta / close / error). Apply-delta logic for streaming subscriptions; one-shot `fetch_one()` for snapshots. |
| `tr_api.cookies` | Read TR cookies from real Chrome (`import_from_chrome`) or load/save Mozilla cookie jars. `REQUIRED_AUTH_COOKIES = {JSESSIONID, tr_refresh, tr_device}`. |
| `tr_api.profiles` | Multi-account: `create`, `load`, `list_all`, `set_active`, `remove`. |
| `tr_api.auth` | Programmatic login: `initiate_login(phone, pin)` → push, `complete_login(process_id, code)` → cookies. Also `login_flow()` as the high-level orchestrator. |
| `tr_api.waf` | `get_waf_token()` — runs the AWS WAF JS challenge with Playwright and returns the token to send as `X-aws-waf-token`. Token cached for ~4h. |
| `tr_api.account` | `summary(client)`, `ping(client)`. Wrappers around `/api/v2/auth/account`. |
| `tr_api.portfolio` | `snapshot(client)` (compact) and `snapshot_full(client)` (positions enriched with names + live prices in one WS connection). |
| `tr_api.transactions` | Paginated timeline fetches: `fetch_all`, `fetch_since(cutoff)`, `fetch_until_id(known_ids)`. Topic: `timelineTransactions`. |
| `tr_api.exceptions` | Hierarchy: `TrApiError` → `CookieError` / `ProfileError` / `AuthError` / `ApiError`. Specific: `MissingSessionCookies`, `SessionExpired`, `RateLimited`, `InvalidCredentials`. |
| `tr_api.cli` | `tr-api …` command-line interface. See [docs/cli-contract.md](docs/cli-contract.md). |

---

## 📚 Docs

| Doc | When to read it |
|---|---|
| **README.md** (this file) | Overview, install, quick-start. |
| [docs/auth-modes.md](docs/auth-modes.md) | Cookie-import vs programmatic-login: trade-offs, when to use which. |
| [docs/websocket-topics.md](docs/websocket-topics.md) | TR's WS topics, what `timelineTransactions` actually returns, and the known `timelineActivityLog` gap. |
| [docs/events.md](docs/events.md) | Vocabulary of `eventType` strings TR emits and what they mean. |
| [docs/troubleshooting.md](docs/troubleshooting.md) | Common error codes (3003, 401, 405, 429) and how to recover. |
| [docs/cli-contract.md](docs/cli-contract.md) | CLI surface: every subcommand, flag, exit code. |

---

## 🐍 Python compatibility

Python 3.10+ (uses PEP 604 `X | None`, `dict[…]` generics, `match`/`case`
in places). Tested on 3.10, 3.11, 3.12.

---

## ⚠️ Status

Beta. Used in production for two downstream apps:

- [`trade-republic-dashboard`](https://github.com/cdamken/trade-republic-dashboard) — local single-user dashboard
- [`trade-republic-owncloud`](https://github.com/cdamken/trade-republic-owncloud) — multi-user ownCloud 10 app

API surface is stable; we may add modules (e.g. `activity_log`) without
breaking the existing ones. Anything in `_private` helpers is fair game
for internal restructuring.

If you hit a bug, an event type we don't decode, or a TR endpoint we
don't cover, open an [issue](https://github.com/cdamken/tr-api/issues)
or a PR.

---

## 📜 License

**Business Source License 1.1** — same family as `pytr`. Free for
personal, non-commercial use; converts to **Apache 2.0** on **May 22, 2030**.
Commercial use before then: open an issue to discuss.

---

## 🙏 Credits

Heavily inspired by [pytr-org/pytr](https://github.com/pytr-org/pytr).
The endpoint understanding, WebSocket message format, and many parsing
patterns were learned by reading pytr's source. The pytr authors did the
hard reverse-engineering work over years.

The novel contributions here are:
- **Cookie-import mode** (don't fight the WAF, befriend the browser).
- **Programmatic login that doesn't fingerprint as a headless bot**
  (Playwright is used only to fetch one WAF token, never to navigate
  the actual login flow).
- **Multi-account by design** (every TR phone gets its own profile dir).
