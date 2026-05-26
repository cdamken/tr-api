# Auth modes — cookie-import vs programmatic login

`tr-api` supports two ways to obtain the TR session cookies that
authenticate every API call. Pick one. The end result of both is the
same file on disk:
`~/.tr-api/profiles/<phone>/cookies.txt`.

| | Cookie-import | Programmatic login |
|---|---|---|
| When | Workstation with Chrome | Headless server, CI, container |
| User interaction | Log in to TR in Chrome | Enter 4-digit code TR pushes to mobile app |
| Playwright needed? | **No** — only `pycookiecheat` | **Yes** — to fetch one WAF token |
| Renewal | Re-log-in in Chrome, re-import | `tr-api login` again |
| Risk profile | Lowest — TR sees a real Chrome session | Higher — TR sees a Playwright-launched Chromium, but only for the WAF JS challenge, not the actual login |
| Setup time | ~10 seconds | ~30 seconds (incl. WAF challenge) |
| Module | `tr_api.cookies` | `tr_api.auth` + `tr_api.waf` |

---

## Cookie-import mode

**Use this on your laptop / desktop** where you're already logged into
Trade Republic in Chrome.

### How it works

1. You log in to `https://app.traderepublic.com` in Chrome normally.
   The real-browser AWS WAF challenge runs and TR sets HttpOnly cookies
   for `api.traderepublic.com` (`JSESSIONID`, `tr_refresh`, `tr_device`).
2. `tr_api.cookies.import_from_chrome()` reads Chrome's encrypted cookie
   database via [`pycookiecheat`](https://github.com/n8henrie/pycookiecheat)
   (which handles the platform-specific decryption: macOS Keychain /
   Windows DPAPI / Linux libsecret).
3. We write a Mozilla cookie jar to
   `~/.tr-api/profiles/<phone>/cookies.txt`. From that point on,
   `TrClient` and all WS subscriptions use those cookies.

### CLI

```bash
tr-api profiles add        # interactive: detects which TR account
                           # is logged into Chrome, imports those cookies
```

### Programmatic

```python
from tr_api import cookies, profiles
prof = profiles.create("+491701234567")
cookie_dict = cookies.import_from_chrome()      # {name: value}
cookies.save_to_file(cookie_dict, prof.cookies_file)
profiles.set_active(prof.phone)
```

### Renewal

Chrome's TR cookies expire (HttpOnly session cookies have a finite
server-side lifetime). When `TrClient` raises `SessionExpired`:

```python
from tr_api import cookies, profiles
prof = profiles.get_active()
cookies.save_to_file(cookies.import_from_chrome(), prof.cookies_file)
```

Or simply rerun `tr-api profiles add` for the same phone.

> ⚠️ Cookie-import requires Chrome's cookies to be readable. On macOS,
> the first run will prompt for Keychain access ("Chrome wants to use
> the 'Chrome Safe Storage' confidential information"). Click **Always
> Allow** — pycookiecheat won't ask again.

---

## Programmatic login

**Use this on a headless server** (no desktop browser available).
Examples: ownCloud, a cron job on a NAS, a CI runner that needs
authenticated TR access.

### How it works

The login flow mirrors what TR's own web frontend does internally,
adapted to be driven from Python:

```
1. waf.get_waf_token()
     └─ Playwright launches Chromium, opens app.traderepublic.com,
        waits for AwsWafIntegration.getToken() to resolve, returns
        the token string. Cached ~4h.

2. auth.initiate_login(phone, pin)
     POST /api/v1/auth/web/login
     Headers: X-aws-waf-token, real-Chrome User-Agent, Sec-Fetch-*, ...
     Body:    {"phoneNumber": phone, "pin": pin}
     Returns: InitiateResult(process_id, countdown_seconds, two_factor_method)
     → TR pushes a 4-digit code to the user's TR mobile app (or SMS).

3. <prompt user for the 4-digit code>

4. auth.complete_login(process_id, code)
     POST /api/v1/auth/web/login/{process_id}/{code}
     Returns: CompleteResult(cookies={...})
     → Harvest cookies, save them to the profile.
```

### CLI

```bash
tr-api login --phone +491701234567 --pin 1234
# (interactive prompt for the 4-digit code)
```

### Programmatic

```python
from tr_api import auth, profiles, cookies

# Step 1+2: ask TR to push a code
init = auth.initiate_login("+491701234567", "1234")
print(f"Code valid for {init.countdown_seconds}s. Via: {init.two_factor_method}")

# Step 3: collect from your own UI / stdin / web modal
code = input("4-digit code: ").strip()

# Step 4: complete and save
result = auth.complete_login(init.process_id, code)
prof = profiles.create("+491701234567")
cookies.save_to_file(result.cookies, prof.cookies_file)
profiles.set_active(prof.phone)
```

### Renewal

When cookies die (typically after a few days/weeks of inactivity, or
when TR invalidates them after another login on the same number — see
[troubleshooting](troubleshooting.md) on close code `3003 (registered)`),
just call `initiate_login` / `complete_login` again. The pair-of-calls
should be done in succession: the `process_id` from step 1 only stays
valid for ~60 seconds.

> 💡 For long-running services (dashboards), don't initiate a new login
> on every request — cache the `process_id` between the "no code yet"
> and "code received" round-trips. See
> [`trade-republic-owncloud`](https://github.com/cdamken/trade-republic-owncloud)
> for a reference implementation (`.pending_login.json` with a 5 min TTL).

### Why this isn't just "Playwright clicks through the login"

The whole reason `tr-api` exists is that TR's WAF rate-limits
Playwright-driven Chromium when it sees a *full* login flow happen in
headless mode. By:

- Using Playwright **only** to run `AwsWafIntegration.getToken()` (a few
  hundred milliseconds of page load + JS execution), and
- Driving the actual login round-trip with `requests` (with a real-Chrome
  UA),

we keep the bot-detection surface area to a minimum. So far this passes
where pytr's full Playwright-driven flow does not.

---

## Cookie inventory

Both modes end up writing the same set of cookies into the profile's
`cookies.txt`. The ones that matter:

| Cookie | Where it's set | Purpose |
|---|---|---|
| `JSESSIONID` | `api.traderepublic.com` (HttpOnly) | Session id. Most important. |
| `tr_refresh` | `api.traderepublic.com` (HttpOnly) | Refresh token for `/auth/web/session`. |
| `tr_device` | `api.traderepublic.com` (HttpOnly) | Device fingerprint TR uses to recognise repeat logins. |
| `tr_claims` | `app.traderepublic.com` | JWT-ish payload with sessionId + jurisdiction. Nice for debugging, not strictly required for auth. |
| `aws-waf-token` | `app.traderepublic.com` | The WAF-bypass token. Real Chrome refreshes it automatically; tr-api's `waf.get_waf_token` re-runs the challenge for programmatic login. |

`tr_api.cookies.validate(cookies)` checks that
`{JSESSIONID, tr_refresh, tr_device}` are present and raises
`MissingSessionCookies` otherwise.
