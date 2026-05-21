# tr-api

Minimal Python client for the **Trade Republic** (Germany) backend API.

The big difference vs [`pytr`](https://github.com/pytr-org/pytr): we don't use a headless browser (Playwright) to bypass AWS WAF. Instead, **we reuse the cookies from the user's real browser** — where the AWS WAF challenge has already been solved by the actual user, with a real Chrome fingerprint that TR trusts.

This avoids the rate-limit/blacklist that TR applies to headless-generated WAF tokens.

---

## ✨ Why this exists

Late May 2026: Trade Republic tightened bot detection on `/api/v1/auth/web/login`. Tokens produced by Playwright (used internally by pytr) get rate-limited (HTTP 429) after just a few attempts. Tokens from a real Chrome session work fine.

The fix is conceptually simple: stop trying to be a browser, **be a browser companion** instead. Let the user log in once in their actual Chrome, then hijack their cookies for our API calls.

---

## 🎯 Scope

What this client does:
- 🔐 Read TR session cookies from the user's Chrome via `pycookiecheat`
- 📋 Call authenticated REST endpoints (account info, settings)
- 📊 Subscribe to portfolio data over the TR WebSocket
- 📜 Export transactions
- 🔁 Auto-refresh session via `/auth/web/session`
- 👥 Multi-account: each Trade Republic phone number gets its own profile

What it doesn't do:
- ❌ Solve the AWS WAF challenge itself (the user's browser does that)
- ❌ Handle MFA prompts (the user logs in via the official web UI)
- ❌ Implement device pairing / ECDSA login (deprecated path)
- ❌ Parse every kind of timeline event (only what dashboards typically need)

---

## 📐 Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│  USER ACTION (one-time, manual)                                   │
│  Open app.traderepublic.com in Chrome → log in normally           │
└────────────────────┬─────────────────────────────────────────────┘
                     │
                     │  cookies (incl. aws-waf-token, JSESSIONID,
                     │           tr_refresh, tr_device, tr_claims)
                     ↓
┌──────────────────────────────────────────────────────────────────┐
│  cookies.py — import_from_chrome(profile)                        │
│  Uses pycookiecheat to read Chrome's encrypted cookie DB.        │
│  Writes ~/.tr-api/profiles/<phone>/cookies.txt (Netscape fmt) │
└────────────────────┬─────────────────────────────────────────────┘
                     │
                     ↓
┌──────────────────────────────────────────────────────────────────┐
│  client.py — TradeRepublic class                                  │
│                                                                   │
│   client = TradeRepublic.from_profile("+4912345678")             │
│   client.account()      → dict (name, jurisdiction, secAccNo)    │
│   client.portfolio()    → list[Position]                          │
│   client.transactions(last_days=7) → list[Transaction]            │
│                                                                   │
│  Sends Origin + Referer + Sec-Fetch-* headers (TR requires them). │
│  Auto-refreshes session via /api/v1/auth/web/session.             │
└──────────────────────────────────────────────────────────────────┘
```

---

## 🧠 Multi-account support

Pytr keeps cookies-per-phone but only one `credentials` file. We go full multi-account:

```
~/.tr-api/
├── default               ← symlink to active profile
└── profiles/
    ├── +4912345678/
    │   ├── meta.json     ← phone, jurisdiction, account name (no PIN stored)
    │   └── cookies.txt   ← Mozilla cookie jar imported from Chrome
    ├── +4998765432/
    │   ├── meta.json
    │   └── cookies.txt
    └── +1234567890/
        ├── meta.json
        └── cookies.txt
```

CLI:
```bash
tr-api profiles list
tr-api profiles add        # imports cookies from Chrome for the current TR session
tr-api profiles use <phone>  # switch active profile
tr-api portfolio           # uses active profile
```

We never store the PIN. Authentication comes entirely from session cookies obtained via the real browser.

---

## 📦 Module layout

```
src/tr_api/
├── __init__.py           # re-export public API
├── client.py             # TradeRepublic class (REST + WS calls)
├── cookies.py            # import/export Mozilla cookie jars from Chrome
├── profiles.py           # multi-account profile management
├── auth.py               # session refresh / validation
├── portfolio.py          # WebSocket → portfolio fetch
├── transactions.py       # transaction history
├── account.py            # /api/v2/auth/account
├── exceptions.py         # AuthError, SessionExpired, ApiError, etc.
└── cli.py                # `tr-api ...` command
```

---

## 🚀 Quick start (planned)

```bash
# 1. Install (once we publish)
pipx install tr-api

# 2. Log in to TR in Chrome normally (one-time)
open "https://app.traderepublic.com"

# 3. Import cookies into a new profile
tr-api profiles add

# 4. Use the API
tr-api portfolio
tr-api transactions --last-days 7
```

Programmatic:
```python
from tr_api import TradeRepublic

tr = TradeRepublic.from_profile("+4912345678")
portfolio = tr.portfolio()
for pos in portfolio:
    print(f"{pos.name:<30} €{pos.net_value:>10,.2f}  ({pos.pl_pct:+.1f}%)")
```

---

## 🛣️ Roadmap

| Phase | Deliverable | Status |
| :---: | :--- | :---: |
| 1 | Project scaffold, README, license | 🚧 |
| 2 | `cookies.py` — Chrome import (build on pycookiecheat) | ⏳ |
| 3 | `profiles.py` — multi-account directories | ⏳ |
| 4 | `client.py` — REST session, auth headers, refresh | ⏳ |
| 5 | `account.py` — `/api/v2/auth/account` | ⏳ |
| 6 | `portfolio.py` — WebSocket portfolio | ⏳ |
| 7 | `transactions.py` — `/api/v2/timeline/transactions` | ⏳ |
| 8 | `cli.py` — full command set | ⏳ |
| 9 | Wire into [`trade-republic-dashboard`](https://github.com/cdamken/trade-republic-dashboard) | ⏳ |
| 10 | Tests + GitHub release | ⏳ |

Each phase = small commit, manually tested before moving on.

---

## ⚠️ Status

**Experimental.** Right now (phase 1) only the README and scaffold exist. Use [`pytr`](https://github.com/pytr-org/pytr) until this hits at least phase 6.

---

## 📜 License

**Business Source License 1.1** (same as the dashboard). Free for personal, non-commercial use. Converts to Apache 2.0 in May 2030.

---

## 🙏 Credits

This client is heavily inspired by [pytr-org/pytr](https://github.com/pytr-org/pytr). The endpoint understanding, websocket message format, and many parsing patterns were learned by reading pytr's source. The pytr authors did the hard reverse-engineering work over years.

The novel contribution here is: stop fighting the WAF, befriend the browser.
