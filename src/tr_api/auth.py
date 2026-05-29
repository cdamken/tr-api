"""Programmatic login flow for Trade Republic — no manual browser needed.

The two-step flow (matches what TR's web frontend does internally):

  1. **Initiate** — POST /api/v1/auth/web/login with {phoneNumber, pin}
     - Headers MUST include `X-aws-waf-token` (fresh, from waf.get_waf_token())
     - Response: {processId, countdownInSeconds, ...}
     - TR sends a 4-digit code as a push notification to the user's
       Trade Republic mobile app (or, for older sessions, an SMS).

  2. **Complete** — POST /api/v1/auth/web/login/{processId}/{code}
     - No body; the code is in the URL path.
     - Response sets Set-Cookie with tr_session + tr_refresh + tr_device.
     - We harvest those cookies and save them to the profile.

After login, the saved cookies authenticate every other tr-api call
(account, portfolio, transactions, …).

The high-level entry point is `login_flow()`, which orchestrates both
steps and accepts a callback to obtain the 4-digit code from the user.
The CLI provides an interactive prompt; programmatic callers (e.g. a
dashboard) can supply their own.

Failure modes worth knowing about:
  - TOO_MANY_REQUESTS (429): account is in cooldown after failed
    attempts. Response includes `nextAttemptTimestamp` — we expose it so
    the caller can wait or surface it to the user.
  - PIN_INVALID / NUMBER_INVALID: bad creds.
  - 405 (empty body): the WAF token was rejected. We retry once with a
    fresh token (waf.force_refresh()).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

import requests

from . import cookies as _cookies
from . import waf
from .client import API_BASE, APP_ORIGIN, DEFAULT_USER_AGENT
from .exceptions import ApiError, TrApiError
from .profiles import Profile

LOGIN_ENDPOINT = "/api/v1/auth/web/login"

# Keep-alive endpoint. A periodic GET here causes TR to rotate the
# session cookies (JSESSIONID + tr_session) without a new login. pytr
# has used this for years at a ~290 s cadence (just under the ~5 min
# server-side session TTL). Reference:
# https://github.com/pytr-org/pytr/blob/master/pytr/api.py
SESSION_ENDPOINT = "/api/v1/auth/web/session"

# How often to refresh proactively. 290s sits just under TR's ~5 min
# session timeout; if you call right at the edge you risk a race.
SESSION_REFRESH_INTERVAL_SEC = 290

# Default headers for the login round-trip. We deliberately don't reuse
# TrClient here — TrClient expects valid cookies to instantiate, and
# during login we don't have them yet.
def _login_headers(waf_token: str) -> dict[str, str]:
    return {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Content-Type": "application/json",
        "Origin": APP_ORIGIN,
        "Referer": APP_ORIGIN + "/",
        "Sec-Fetch-Site": "same-site",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "X-aws-waf-token": waf_token,
    }


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class LoginError(TrApiError):
    """Generic login failure."""


class InvalidCredentials(LoginError):
    """Phone or PIN was rejected (PIN_INVALID, NUMBER_INVALID, etc.)."""


class RateLimited(LoginError):
    """TR returned 429 TOO_MANY_REQUESTS with a retry window.

    `next_attempt_at` is when TR will accept another attempt for this
    account; `wait_seconds` is the duration from now.
    """
    def __init__(
        self,
        message: str,
        *,
        next_attempt_at: datetime | None = None,
        wait_seconds: int | None = None,
    ):
        super().__init__(message)
        self.next_attempt_at = next_attempt_at
        self.wait_seconds = wait_seconds


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------
@dataclass
class InitiateResult:
    """Result of step 1 (POST /auth/web/login)."""
    process_id: str
    countdown_seconds: int              # how long until the user can request a resend
    two_factor_method: str | None = None  # "APP", "SMS", or None if not provided
    raw: dict[str, Any] | None = None


@dataclass
class CompleteResult:
    """Result of step 2 (POST /auth/web/login/{processId}/{code})."""
    cookies: dict[str, str]   # name → value, harvested from Set-Cookie
    raw_body: str | None = None


# ---------------------------------------------------------------------------
# Step 1: initiate
# ---------------------------------------------------------------------------
def initiate_login(phone: str, pin: str, *, session: requests.Session | None = None) -> InitiateResult:
    """POST /api/v1/auth/web/login and return the processId + 2FA hint.

    Gets a fresh WAF token via Playwright if one isn't already cached.
    On 405 (WAF-token rejected), retries once with a force-refreshed token.

    Raises:
        InvalidCredentials   if TR says the phone/PIN are wrong.
        RateLimited          if TR is in cooldown (with retry window attached).
        LoginError           for any other 4xx/5xx.
    """
    s = session if session is not None else requests.Session()
    body = {"phoneNumber": phone, "pin": pin}

    # First attempt with whatever WAF token we have cached (or fresh if none).
    token = waf.get_waf_token().value
    r = s.post(API_BASE + LOGIN_ENDPOINT, json=body, headers=_login_headers(token), timeout=20)

    # 405 with empty body = AWS WAF rejected the token. Refresh and retry once.
    if r.status_code == 405 and not r.text:
        token = waf.get_waf_token(force_refresh=True).value
        r = s.post(API_BASE + LOGIN_ENDPOINT, json=body, headers=_login_headers(token), timeout=20)

    return _parse_initiate_response(r)


def _parse_initiate_response(r: requests.Response) -> InitiateResult:
    # Happy path
    if r.status_code == 200:
        try:
            j = r.json()
        except ValueError as e:
            raise LoginError(f"Initiate succeeded but body wasn't JSON: {r.text[:200]}") from e
        pid = j.get("processId")
        if not pid:
            raise LoginError(f"Initiate returned 200 but no processId: {j}")
        return InitiateResult(
            process_id=pid,
            countdown_seconds=int(j.get("countdownInSeconds") or 0),
            two_factor_method=j.get("twoFactorMethod") or j.get("twoFactor") or None,
            raw=j,
        )

    # Error path — TR returns structured JSON for 4xx
    try:
        j = r.json()
    except ValueError:
        j = None

    err_code = None
    err_meta: dict[str, Any] | None = None
    if j and isinstance(j.get("errors"), list) and j["errors"]:
        err_code = j["errors"][0].get("errorCode")
        err_meta = j["errors"][0].get("meta")

    if r.status_code == 429 or err_code == "TOO_MANY_REQUESTS":
        next_at: datetime | None = None
        wait_s: int | None = None
        if err_meta:
            wait_s = err_meta.get("nextAttemptInSeconds")
            ts = err_meta.get("nextAttemptTimestamp")
            if ts:
                try:
                    if ts.endswith("Z"):
                        ts = ts[:-1] + "+00:00"
                    next_at = datetime.fromisoformat(ts)
                except (TypeError, ValueError):
                    pass
        msg = "Trade Republic is rate-limiting login attempts for this account"
        if wait_s:
            msg += f" (retry in {wait_s}s ≈ {wait_s // 60} min)"
        if next_at:
            msg += f"; next attempt allowed at {next_at.isoformat()}"
        raise RateLimited(msg, next_attempt_at=next_at, wait_seconds=wait_s)

    if err_code in ("PIN_INVALID", "NUMBER_INVALID", "USER_NOT_FOUND"):
        raise InvalidCredentials(f"Trade Republic rejected credentials: {err_code}")

    if r.status_code == 405 and not r.text:
        raise LoginError(
            "AWS WAF rejected the request even after token refresh. "
            "This usually means the WAF challenge JS has changed, or your "
            "IP/fingerprint has been flagged. Try again later."
        )

    raise LoginError(
        f"Initiate failed: status={r.status_code} body={r.text[:300]}"
    )


# ---------------------------------------------------------------------------
# Step 2: complete
# ---------------------------------------------------------------------------
def complete_login(
    process_id: str,
    code: str,
    *,
    session: requests.Session | None = None,
) -> CompleteResult:
    """POST /api/v1/auth/web/login/{processId}/{code}.

    Returns the cookies set by TR (tr_session, tr_refresh, tr_device, …).

    Note: this also needs the WAF token header — TR enforces it on every
    /auth/web/login* endpoint.
    """
    if not process_id:
        raise LoginError("process_id is required")
    if not code or not code.strip():
        raise LoginError("code is required")
    code = code.strip()

    s = session if session is not None else requests.Session()
    token = waf.get_waf_token().value
    url = f"{API_BASE}{LOGIN_ENDPOINT}/{process_id}/{code}"
    r = s.post(url, headers=_login_headers(token), timeout=20)

    if r.status_code == 405 and not r.text:
        token = waf.get_waf_token(force_refresh=True).value
        r = s.post(url, headers=_login_headers(token), timeout=20)

    if r.status_code == 200:
        # Cookies are in s.cookies thanks to requests.Session.
        out_cookies = {c.name: c.value for c in s.cookies if c.domain.endswith("traderepublic.com")}
        if not out_cookies:
            raise LoginError(
                "Complete returned 200 but no traderepublic cookies were set."
            )
        return CompleteResult(cookies=out_cookies, raw_body=r.text)

    # Error path
    try:
        j = r.json()
    except ValueError:
        j = None

    err_code = None
    if j and isinstance(j.get("errors"), list) and j["errors"]:
        err_code = j["errors"][0].get("errorCode")

    if err_code in ("AUTHENTICATION_ERROR", "OTP_INVALID", "TAN_INVALID"):
        raise InvalidCredentials(
            f"Trade Republic rejected the 4-digit code ({err_code}). "
            "Check the code from the TR mobile app push and try again."
        )

    raise LoginError(
        f"Complete failed: status={r.status_code} body={r.text[:300]}"
    )


# ---------------------------------------------------------------------------
# Session keepalive — the pytr-style way to keep cookies fresh
# ---------------------------------------------------------------------------
@dataclass
class RefreshResult:
    """Outcome of a single refresh_session() call."""
    ok: bool                           # True if TR returned 200 + rotated cookies
    status_code: int
    cookies_changed: list[str]         # names of cookies whose values changed
    error: str | None = None


def refresh_session(profile: Profile) -> RefreshResult:
    """Refresh the TR session by hitting /api/v1/auth/web/session.

    This is the trick pytr has used for years to keep long-running
    processes alive: TR rotates the session cookies (JSESSIONID,
    tr_session) on every successful GET to this endpoint. Call it
    every ~290s before the server-side session expires (~5 min idle).

    Loads cookies from the profile, GETs the endpoint with a fresh
    WAF token, persists the rotated cookies back to disk.

    Returns a RefreshResult. If `ok` is False (401 typically), the
    user must re-login — the refresh token chain itself has expired
    or been invalidated.
    """
    if not profile.cookies_file.is_file():
        return RefreshResult(
            ok=False, status_code=0, cookies_changed=[],
            error=f"No cookies file at {profile.cookies_file}; run `tr-api auth login` first.",
        )

    sess = requests.Session()
    sess.cookies = _cookies.load_from_file(profile.cookies_file)
    before = {c.name: c.value for c in sess.cookies}

    try:
        waf_token = waf.get_waf_token().value
    except Exception as e:
        return RefreshResult(
            ok=False, status_code=0, cookies_changed=[],
            error=f"WAF token unavailable: {e}",
        )

    url = f"{API_BASE}{SESSION_ENDPOINT}"
    try:
        r = sess.get(url, headers=_login_headers(waf_token), timeout=15)
    except requests.RequestException as e:
        return RefreshResult(
            ok=False, status_code=0, cookies_changed=[],
            error=f"network error: {type(e).__name__}: {e}",
        )

    if r.status_code != 200:
        return RefreshResult(
            ok=False,
            status_code=r.status_code,
            cookies_changed=[],
            error=f"refresh rejected: status={r.status_code} body={r.text[:200]}",
        )

    # TR returned 200 — its Set-Cookie headers have already updated
    # sess.cookies via requests. Persist back to disk.
    after = {c.name: c.value for c in sess.cookies if c.domain.endswith("traderepublic.com")}
    changed = sorted(name for name, val in after.items() if before.get(name) != val)
    _cookies.save_to_file(after, profile.cookies_file)
    return RefreshResult(
        ok=True,
        status_code=200,
        cookies_changed=changed,
    )


# ---------------------------------------------------------------------------
# High-level orchestration
# ---------------------------------------------------------------------------
CodeProvider = Callable[[InitiateResult], str]


def login_flow(
    profile: Profile,
    pin: str,
    code_provider: CodeProvider,
) -> dict[str, Any]:
    """Full login: initiate → wait for user to provide code → complete → save.

    `code_provider(initiate_result)` is called after step 1 succeeds. It
    receives the InitiateResult (so it can show countdown/2FA method to
    the user) and must return the 4-digit code as a string.

    On success, cookies are written to `profile.cookies_file`. Returns a
    dict summary suitable for the CLI's JSON envelope.
    """
    sess = requests.Session()
    init = initiate_login(profile.phone, pin, session=sess)
    code = code_provider(init)
    done = complete_login(init.process_id, code, session=sess)

    n = _cookies.save_to_file(done.cookies, profile.cookies_file)
    return {
        "phone": profile.phone,
        "process_id": init.process_id,
        "two_factor_method": init.two_factor_method,
        "cookies_saved": n,
        "cookies_file": profile.cookies_file,
        "summary": _cookies.summarize(done.cookies),
    }
