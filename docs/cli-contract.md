# tr-api CLI contract

This document fixes the contract between the `tr-api` CLI and any caller
(humans, shell scripts, the trade-republic-dashboard process). Stable
exit codes and JSON shapes are what let the dashboard call us as a
subprocess without parsing English error messages.

## Output mode

Every command supports two output modes:

- **Default (human)** — pretty-formatted, may use color when stdout is a TTY.
  Designed to be read by a person at a terminal.
- **`--json`** — one JSON document on stdout, suitable for `jq`/scripts.
  This is what the dashboard always uses.

The dashboard always invokes us with `--json`.

## Exit codes

Every exit code maps to one exception class. Callers should rely on the
numeric code, not on parsing stderr.

| Code | Symbol                | Cause                                                                  |
| ---- | --------------------- | ---------------------------------------------------------------------- |
| 0    | OK                    | Command succeeded                                                      |
| 1    | GENERIC_ERROR         | Unexpected exception (bug — should be rare)                            |
| 2    | USAGE_ERROR           | Bad CLI arguments / flags                                              |
| 10   | NO_ACTIVE_PROFILE     | `NoActiveProfile`: no `~/.tr-api/active`                               |
| 11   | PROFILE_NOT_FOUND     | `ProfileNotFound`: asked for a phone that has no profile dir           |
| 20   | MISSING_COOKIES       | `MissingSessionCookies`: cookies file absent or missing required names |
| 21   | CHROME_NOT_FOUND      | `ChromeNotFound`: couldn't locate Chrome's cookie DB                   |
| 22   | KEYCHAIN_DENIED       | `KeychainAccessDenied`: macOS Keychain refused decryption              |
| 30   | SESSION_EXPIRED       | `SessionExpired`: TR returned 401, cookies are dead                    |
| 31   | API_ERROR             | `ApiError`: TR returned non-2xx (5xx, etc.)                            |
| 40   | INVALID_CREDENTIALS   | `InvalidCredentials`: PIN, phone, or 4-digit code rejected             |
| 41   | RATE_LIMITED          | `RateLimited`: TR account cooldown after failed login attempts         |
| 42   | WAF_TOKEN_FAILED      | `WafTokenError`: couldn't get a WAF token (Playwright missing/broken)  |

Codes 10–29 are "user fixable by running another tr-api command".
Codes 30–39 are "user must re-authenticate".
Codes 40–49 are "login/credential problems".

## Error JSON shape (stderr, exit ≠ 0)

```json
{
  "ok": false,
  "error": "MissingSessionCookies",
  "exit_code": 20,
  "message": "Cookies file ~/.tr-api/profiles/+49…/cookies.txt is missing required auth cookies …",
  "hint": "Run: tr-api auth import --phone +49…"
}
```

Notes:
- `error` is the exception class name (matches what Python would print).
- `message` is the full one-line human description.
- `hint` is the suggested next command when there is one. Optional.
- `exit_code` mirrors the process exit code so a caller can read it from
  the JSON alone if they want to.

## Success JSON shape (stdout, exit 0)

Every successful command returns a JSON object with at least:

```json
{ "ok": true, "data": <command-specific payload> }
```

The dashboard reads `result.ok` first, then `result.data`. Wrapping in
`{ok, data}` keeps the contract uniform across commands and gives us
room to add `warnings: []` or `meta: {…}` later without breaking callers.

## Command surface (planned)

Subcommands and their `data` payload shapes. Items marked TODO are
not yet implemented.

### `tr-api profiles list`

```json
{ "ok": true, "data": {
    "active": "+49…",
    "profiles": [
      { "phone": "+49…", "jurisdiction": "DE", "name": "Personal",
        "created_at": "2026-05-21T19:00:00+00:00", "has_cookies": true }
    ]
}}
```

### `tr-api profiles use <phone>`

Sets the active profile. Payload echoes the new active profile metadata.

### `tr-api profiles add <phone> [--name=…] [--jurisdiction=DE]`

Creates an empty profile (no cookies yet). Caller then runs `auth import`.

### `tr-api profiles remove <phone>`

Deletes the profile directory.

### `tr-api auth login [--phone=…] [--pin=…] [--code=…] [--name=…] [--jurisdiction=DE]`

**Programmatic login — recommended path.** Launches headless Chromium
under our control to get a fresh AWS WAF token, then runs the standard
`/api/v1/auth/web/login` flow. The user never has to open Chrome
themselves.

Flow:
  1. Resolve / create profile.
  2. Resolve PIN (`--pin`, `TR_API_PIN` env, or interactive prompt).
  3. POST `/api/v1/auth/web/login` → `processId`. TR sends a 4-digit
     code as a push notification to the user's TR mobile app.
  4. Resolve code (`--code`, `TR_API_CODE` env, or interactive prompt).
  5. POST `/api/v1/auth/web/login/{processId}/{code}` → session cookies.
  6. Save cookies, set profile active if none was.

Payload on success:
```json
{
  "phone": "+49…",
  "process_id": "…",
  "two_factor_method": "APP" | "SMS" | null,
  "cookies_saved": <n>,
  "cookies_file": "/Users/.../cookies.txt",
  "summary": <cookies.summarize result>,
  "set_active": true
}
```

Common error exits: 40 (bad PIN / wrong code), 41 (rate-limited — also
includes `next_attempt_at` in stderr), 42 (Playwright missing).

### `tr-api auth import [--phone=…] [--browser=chrome]`

**Legacy path.** Reads cookies from a Chrome session the user logged
into manually. Use this only if `auth login` is unavailable or you
want to reuse an existing browser session. Payload is `cookies.summarize(...)`.

### `tr-api auth status [--phone=…]`

Reports whether the saved cookies look complete. Does NOT contact TR.
Payload: `cookies.summarize(...)` plus `cookies_file_mtime`.

### `tr-api account [--phone=…]`  *(TODO Phase 5)*

GET `/api/v2/auth/account`. Payload is the raw TR response.

### `tr-api portfolio [--phone=…]`  *(TODO Phase 6)*

WebSocket portfolio fetch. Payload is the merged portfolio snapshot.

### `tr-api transactions [--since=YYYY-MM-DD] [--phone=…]`  *(TODO Phase 7)*

Timeline transactions, optionally bounded. Payload is a list.

### `tr-api timeline-detail <event_id> [<event_id> ...] [--with-documents] [--phone=…]`

Fetch the `timelineDetailV2` page for one or more event IDs. With a
single id, returns the raw detail. With multiple ids, returns a map
`{event_id: detail}` and any per-id failures appear as
`{"error": "..."}` rather than failing the whole call.

`--with-documents` additionally extracts and returns the document refs
(`id`, `title`, `url`) from the detail payload.

### `tr-api docs list [--since=YYYY-MM-DD] [--kinds=trades,dividends,…] [--phone=…]`

Walk both timeline topics on one WebSocket, fetch every
`timelineDetailV2`, return all document refs without downloading.
Useful for "what would `docs download` do?" inspection.

Data shape:

```json
{
  "count": 487,
  "by_kind":  {"trades": 312, "dividends": 41, "tax": 6, ...},
  "by_year":  {"2024": 180, "2025": 220, "2026": 87},
  "items": [
    {"event_id": "...", "event_type": "TRADING_TRADE_EXECUTED",
     "event_date": "2025-03-15T...", "kind": "trades",
     "title": "Abrechnung", "url": "https://documents.tr.com/..."}
  ]
}
```

### `tr-api docs download --out=DIR [--since=YYYY-MM-DD] [--kinds=...] [--concurrency=N] [--dry-run] [--phone=…]`

Download every PDF into `<out>/<YYYY>/<kind>/<filename>.pdf`. Writes a
`manifest.json` at the root. Idempotent: re-running skips files already
on disk.

See [`documents.md`](documents.md) for the full layout description.

Data shape:

```json
{
  "out_dir": "/home/user/tr-docs",
  "counts":  {"downloaded": 487, "skipped_existing": 12, "total": 499},
  "manifest": "/home/user/tr-docs/manifest.json"
}
```

## Stability guarantees

- Exit codes 0–39 are part of the public contract — they will not be
  renumbered without a major-version bump.
- The `{ok, data}` / `{ok:false, error, exit_code, message}` envelope is
  stable across all commands.
- The shape of `data` for each command may grow (new keys) but existing
  keys won't be removed or renamed in a minor release.
