"""Fetch a fresh AWS WAF token using Playwright.

Trade Republic's WAF (AWS WAF Bot Control + Captcha) issues a token via a
JavaScript challenge served from `app.traderepublic.com`. The token must
then be sent as the **`X-aws-waf-token` HTTP header** on subsequent
requests — the cookie alone is not enough for the auth endpoints.

We can't generate this token in pure Python; we have to actually run
the challenge JS. Playwright launches a headless Chromium that:

  1. Navigates to https://app.traderepublic.com
  2. Lets the page load (waits for `window.AwsWafIntegration` to appear)
  3. Calls `AwsWafIntegration.getToken()` — Promise-returning function the
     TR frontend uses internally.

The token is reusable for many requests until WAF rejects it (typically
the 405-with-empty-body response). When that happens, call again with
`force_refresh=True`.

This is the same approach NightOwl07/trade-republic-api uses in TS, and
similar in spirit to what pytr does with playwright — except we use the
JS-API getToken() rather than scraping the cookie afterwards (the cookie
and the token returned by getToken() are the same string, but using the
JS API is more robust to TR changing where the token lives).

Lazy-import Playwright so users who only do cookie imports (no login)
don't need to install it.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

from .exceptions import TrApiError

# Reasonable default. The WAF token typically lasts 4–6 hours; we err on
# the conservative side. force_refresh bypasses this anyway.
DEFAULT_TOKEN_MAX_AGE = 4 * 3600  # 4 hours

WAF_NAV_URL = "https://app.traderepublic.com"

# Mirror what NightOwl07 sends (Chrome on Windows). The exact UA matters
# less than having ONE that doesn't scream "headless". The Chrome version
# advances every ~4 weeks; bump occasionally.
PLAYWRIGHT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


class WafTokenError(TrApiError):
    """Could not obtain a WAF token from Playwright."""


@dataclass
class WafToken:
    """A WAF token plus the wall-clock time we got it (for staleness checks)."""
    value: str
    obtained_at: float  # time.time() when fetched

    def age_seconds(self) -> float:
        return time.time() - self.obtained_at

    def is_stale(self, max_age: float = DEFAULT_TOKEN_MAX_AGE) -> bool:
        return self.age_seconds() > max_age


# Module-level cache so multiple operations in one CLI invocation reuse
# the token (one Playwright launch is ~3s — not free).
_cached: WafToken | None = None
# Serializes the cache check + expensive Playwright fetch so two
# concurrent first calls don't both launch a browser.
_cache_lock = threading.Lock()


def get_waf_token(
    *,
    force_refresh: bool = False,
    max_age: float = DEFAULT_TOKEN_MAX_AGE,
    headless: bool = True,
    timeout_ms: int = 30000,
) -> WafToken:
    """Return a WAF token. Uses an in-process cache unless force_refresh=True.

    Thread-safe: concurrent first calls serialize on a lock so only one
    thread pays the ~3 s Playwright launch; the rest reuse its result.

    Raises WafTokenError if Playwright isn't installed or the JS challenge
    fails (network down, TR's WAF JS missing, etc.).
    """
    global _cached
    with _cache_lock:
        if (
            not force_refresh
            and _cached is not None
            and not _cached.is_stale(max_age)
        ):
            return _cached

        token_value = _fetch_via_playwright(headless=headless, timeout_ms=timeout_ms)
        _cached = WafToken(value=token_value, obtained_at=time.time())
        return _cached


def _fetch_via_playwright(*, headless: bool, timeout_ms: int) -> str:
    """Actually launch the browser and run the WAF challenge."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise WafTokenError(
            "Playwright is not installed. Run:\n"
            "  pip install 'tr-api[browser]'\n"
            "or:\n"
            "  pip install playwright && playwright install chromium"
        ) from e

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=headless,
                args=["--no-sandbox", "--disable-setuid-sandbox"],
            )
            try:
                ctx = browser.new_context(
                    user_agent=PLAYWRIGHT_USER_AGENT,
                    viewport={"width": 1280, "height": 800},
                )
                page = ctx.new_page()
                # `networkidle` never fires on TR's SPA (persistent WS).
                # `domcontentloaded` is plenty — the WAF challenge JS is
                # loaded synchronously by the main HTML.
                page.goto(WAF_NAV_URL, wait_until="domcontentloaded", timeout=timeout_ms)
                page.wait_for_function(
                    "() => window.AwsWafIntegration !== undefined",
                    timeout=timeout_ms,
                )
                token: Any = page.evaluate(
                    "async () => await window.AwsWafIntegration.getToken()"
                )
                if not token or not isinstance(token, str):
                    raise WafTokenError(
                        f"AwsWafIntegration.getToken() returned {token!r}"
                    )
                return token
            finally:
                browser.close()
    except WafTokenError:
        raise
    except Exception as e:
        raise WafTokenError(f"Failed to obtain WAF token via Playwright: {e}") from e


def force_refresh() -> WafToken:
    """Force-refresh the cached token. Equivalent to get_waf_token(force_refresh=True)."""
    return get_waf_token(force_refresh=True)


def clear_cache() -> None:
    """Drop the in-process WAF token cache. Mostly useful in tests."""
    global _cached
    _cached = None
