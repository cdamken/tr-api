# Troubleshooting

Common things that go wrong with `tr-api` and how to recover. Issues
roughly grouped by where they show up: cookies, login, runtime auth, and
WebSocket.

---

## Cookies

### `MissingSessionCookies: JSESSIONID / tr_refresh / tr_device missing`

**What it means**: the loaded cookie jar doesn't include the cookies TR
needs for authenticated calls.

**Fix**:

- Cookie-import mode: open `https://app.traderepublic.com` in Chrome,
  log in, then run `tr-api profiles add` again. The HttpOnly cookies are
  set by `api.traderepublic.com` after a successful login — you need a
  fresh, signed-in session.
- Programmatic login: re-run `tr-api login --phone … --pin …`.

### `ChromeNotFound: could not locate Chrome cookies database`

**What it means**: `pycookiecheat` can't find Chrome's profile. This
typically happens on:

- A non-default Chrome profile (Profile 2, Profile 3, …).
- Chromium / Brave / Edge instead of Chrome (try `import_from_chrome("brave")`).
- A server / VM where Chrome isn't installed at all. Use programmatic
  login mode instead.

### `KeychainAccessDenied` (macOS)

**What it means**: the macOS Keychain prompt was dismissed or denied.

**Fix**: re-run, and when "Chrome wants to use the 'Chrome Safe
Storage' confidential information" appears, click **Always Allow**.
You'll only see it once per OS user.

---

## Login

### `InvalidCredentials: PIN_INVALID` (or `NUMBER_INVALID`)

**What it means**: TR rejected the phone+PIN combo.

**Fix**: double-check the phone number is the one registered with TR
(E.164 format, e.g. `+491701234567`) and the PIN matches what you'd
type into the official app. Note: TR locks the account after a small
number of failures — see `RateLimited` below.

### `RateLimited: TOO_MANY_REQUESTS`

```
RateLimited: TR returned 429.
  next_attempt_at: 2026-05-26T15:42:00+00:00
  wait_seconds: 837
```

**What it means**: TR is in cooldown for this account after too many
failed login attempts (yours or someone else's against the same
number).

**Fix**: wait. The exception carries `next_attempt_at` and
`wait_seconds` for you to surface in the UI. Don't retry sooner —
hammering during cooldown extends it.

### `LoginError` with 405 and an empty body

**What it means**: TR rejected the `X-aws-waf-token` header. Either the
token expired (rare, they last ~4h) or TR has invalidated all tokens
from a particular IP / fingerprint.

**Fix**: `tr_api.auth.initiate_login` already retries once with
`waf.force_refresh()`. If you see this exception raised, the second
attempt also failed. Wait a few minutes (TR cools down its
fingerprint-blocking heuristic) and retry. Persistent 405s are usually
a sign that Chrome's UA fingerprint has shifted ahead of `tr-api`'s
hardcoded one in `client.py:DEFAULT_USER_AGENT` — bump it.

### MFA code never arrives on the phone

**What it means**: TR's push to the mobile app failed. Possible reasons:

- The TR mobile app is logged out / not installed → TR may fall back
  to SMS automatically (look at `InitiateResult.two_factor_method`).
- The phone is offline / push notifications are disabled.
- TR's push pipeline is having a bad day (rare but not unheard of).

**Fix**: wait ~60 seconds, then call `initiate_login` again — the
second push usually goes through. If TR consistently routes via SMS
instead of the app, that's expected for older accounts; just type in
the SMS code.

### MFA code "expired" almost immediately

The `process_id` from `initiate_login` is only good for ~60 seconds.
If your UI presents the code prompt asynchronously and the user takes
their time, you'll see TR reject the code with an "expired" error.
Initiate again; the previous code becomes worthless when a new
`process_id` is issued.

---

## Runtime auth

### `SessionExpired: 401 from /api/v2/auth/account` (or any endpoint)

**What it means**: TR's session cookies died. Could be from a long
absence, an explicit logout in the official app, or the user logging
in on another device.

**Fix**:

- Cookie-import: log in to TR in Chrome and re-import (`tr-api
  profiles add`).
- Programmatic: re-run `tr-api login --phone … --pin …`.

`tr-api` deliberately doesn't try to auto-refresh on every 401 — that
would hide real auth problems. If `/auth/web/session` could recover
without the user, we'd use it; in practice it can't recover from the
"another session took over" case.

---

## WebSocket

### `ConnectionClosedError: received 3003 (registered); then sent 3003 (registered)`

**What it means**: TR's WebSocket closed the connection because
another session has "registered" with the same device/account. This is
how TR enforces "one active session per registration". When you log
in fresh on `tr-api` (or anywhere else with the same phone), any
previously-active WS sessions get evicted with `3003 (registered)`.

**Common causes**:

- You logged in on the official TR app while a `tr-api` WS was still
  running.
- Two `tr-api` callers — e.g. the local Dashboard and the ownCloud port
  — both logged in with the same phone within a short window. The
  newer login invalidates the older one.
- You re-ran a login on the same machine and the previous session is
  still cached somewhere (RAM, another process).

**Fix**:

1. Don't run two `tr-api` callers against the same TR account
   concurrently. Pick one.
2. If you need to recover: re-run `tr-api login` (or
   `auth.initiate_login` / `complete_login`) to issue a new
   `process_id` and get fresh cookies. The newly issued cookies will
   be the "active" registration; the previously evicted session stays
   dead.
3. For long-running services, consider gating logins behind a lock
   (only one login at a time across all your TR-touching processes).

### `ConnectionRefused` / `getaddrinfo failed` / generic socket errors

**What it means**: network problem. TR's API is up, but `tr-api` can't
reach it.

**Fix**:

- Check `curl https://api.traderepublic.com` from the same machine.
- Check for corporate proxies / VPN policies that block
  WebSockets upgrade requests.
- TR has had brief outages historically; if everything else works
  but TR is down, just retry in 5 minutes.

---

## Pagination

### `fetch_all` returns suspiciously few items

**For the trade-gap case**: see
[docs/websocket-topics.md](websocket-topics.md). Trades and dividends
live on a *different* topic that `tr-api` doesn't yet wrap.

**For genuine pagination cut-offs**: bump `max_pages`:

```python
items = transactions.fetch_all(client, max_pages=500)
```

The default cap is 200 pages — generous for most accounts (pages are
~20–30 items each, so 200 × 25 = 5,000 items). Accounts with many
years of card spending may need more.

### `fetch_since(cutoff)` keeps re-fetching the same recent items

`fetch_since` includes items it can't parse a timestamp on (the
defensive default — better to over-fetch than miss). If TR emits one
"timestampless" item somewhere mid-history, pagination doesn't stop.
Switch to `fetch_until_id(known_ids)` which doesn't rely on timestamp
parsing.

---

## Playwright (programmatic-login only)

### `Executable doesn't exist at .../chrome-linux64/chrome`

**What it means**: Playwright is installed but Chromium isn't.

**Fix**: `playwright install chromium` (in the same virtualenv where
`tr-api[browser]` lives).

### `error while loading shared libraries: libatk-bridge-2.0.so.0`

**What it means**: Chromium is installed but system libraries it
depends on aren't. Common on minimal server installs.

**Fix**: `playwright install-deps chromium` (run as root or with
`sudo`; it apt-installs the missing libs).

### Playwright tries to download Chromium on every fetch

**What it means**: `HOME` is pointing at a fresh directory each time,
so Playwright never finds its previously-installed Chromium.

**Fix**: set `PLAYWRIGHT_BROWSERS_PATH=/some/shared/path` in the
environment that launches `tr-api`. Then run `playwright install
chromium` once with that env var, and every subsequent
process — even with different `HOME` — finds Chromium there.

This is what
[`trade-republic-owncloud`](https://github.com/cdamken/trade-republic-owncloud)
does: it stashes Chromium at `/var/cache/tr-playwright` (mode `0750`,
`root:www-data`) and passes the env var when spawning the Python
subprocess.
