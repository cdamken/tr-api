"""Read TR session cookies from the user's real Chrome browser.

We rely on `pycookiecheat`, which handles the platform-specific encryption
(macOS Keychain / Windows DPAPI / Linux libsecret) and SQLite parsing.

The critical bit: TR's important auth cookies are HttpOnly and scoped to
`api.traderepublic.com`, not `app.traderepublic.com`. We fetch both and
merge them.

Public API:
    import_from_chrome()          -> dict[str, str]
    save_to_file(cookies, path)   -> None
    load_from_file(path)          -> MozillaCookieJar
    validate(cookies)             -> raises MissingSessionCookies if bad
"""
from __future__ import annotations

import os
import time
from http.cookiejar import Cookie, MozillaCookieJar
from pathlib import Path

from .exceptions import (
    ChromeNotFound,
    KeychainAccessDenied,
    MissingSessionCookies,
)

# Cookies that MUST be present for an authenticated TR session. These are the
# HttpOnly ones set by api.traderepublic.com after a successful login.
REQUIRED_AUTH_COOKIES = frozenset({"JSESSIONID", "tr_refresh", "tr_device"})

# Additional helpful cookies (not strictly required but include them when present).
USEFUL_COOKIES = frozenset({
    "aws-waf-token",      # the real WAF token from the user's Chrome (not headless)
    "tr_claims",          # JWT with session info (sessionId, jurisdiction)
    "tr_external_id",
    "tr_user_exp_id",
    "tr_appearance",
    "tr_test_d",
    "i18n_redirected",
    "web-trading_consent",
})


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------
def import_from_chrome(browser: str = "chrome") -> dict[str, str]:
    """Pull all relevant TR cookies from the user's Chrome.

    Walks both api.traderepublic.com and app.traderepublic.com. Returns a
    flat dict {name: value} merged (api values take precedence for shared
    names since that's where the auth cookies live).

    Raises:
        ChromeNotFound: pycookiecheat failed to locate Chrome's cookie store
        KeychainAccessDenied: macOS Keychain refused decryption permission
        MissingSessionCookies: user isn't logged in (no JSESSIONID/tr_refresh)
    """
    try:
        from pycookiecheat import chrome_cookies
    except ImportError as e:
        raise ChromeNotFound(
            "pycookiecheat is not installed. Run: pip install pycookiecheat"
        ) from e

    merged: dict[str, str] = {}
    # Order matters: api.* last so its values overwrite the app.* shared cookies
    for url in ("https://app.traderepublic.com/", "https://api.traderepublic.com/"):
        try:
            got = chrome_cookies(url, browser=browser)
        except UnicodeDecodeError as e:
            # Decryption produced garbage bytes (e.g. 0xb0 at position 0)
            # that then fail UTF-8 decoding. On Linux this almost always
            # means pycookiecheat decrypted Chrome's cookie values with the
            # WRONG key — Chrome stores the Safe Storage key in the desktop
            # keyring (gnome-keyring / kwallet); headless or non-GNOME
            # systems fall back to the hardcoded 'peanuts' password, which
            # only matches if Chrome ALSO used basic protection.
            # GitHub issue cdamken/tr-api#3 tracks this on Debian.
            raise ChromeNotFound(
                "Chrome cookie decryption produced invalid bytes "
                f"({e}).\n"
                "On Linux this usually means the Chrome 'Safe Storage' key in "
                "your keyring doesn't match what pycookiecheat used. Fixes:\n"
                "  1. Make sure gnome-keyring / kwallet is running and unlocked\n"
                "  2. Or launch Chrome once with --password-store=basic, "
                "re-login to TR, and retry the import\n"
                "  3. Or use programmatic login instead: tr-api auth login"
            ) from e
        except Exception as e:
            msg = str(e).lower()
            if "keychain" in msg or "decrypt" in msg or "permission" in msg:
                raise KeychainAccessDenied(
                    "macOS Keychain refused access to Chrome Safe Storage.\n"
                    "Look for a dialog on screen (it may be hidden behind windows) "
                    "and click 'Always Allow' (or 'Allow') after entering your "
                    "Mac password."
                ) from e
            if "no chrome cookies" in msg or "not found" in msg:
                raise ChromeNotFound(f"Couldn't read Chrome's cookies DB: {e}") from e
            raise
        merged.update(got)

    validate(merged)
    return merged


def validate(cookies: dict[str, str]) -> None:
    """Raise MissingSessionCookies if the required auth cookies aren't there."""
    have = set(cookies)
    missing = REQUIRED_AUTH_COOKIES - have
    if missing:
        raise MissingSessionCookies(
            f"Required cookies missing from Chrome: {sorted(missing)}.\n"
            "You probably aren't logged in to Trade Republic in Chrome.\n"
            "Open https://app.traderepublic.com, log in, and try again."
        )


# ---------------------------------------------------------------------------
# Save / load (Mozilla cookie jar format — compatible with `requests`)
# ---------------------------------------------------------------------------
def save_to_file(cookies: dict[str, str], path: Path | str) -> int:
    """Write cookies to a Netscape/Mozilla cookie jar file.

    The format is what `requests.Session.cookies` (MozillaCookieJar) reads.
    All cookies get domain `.traderepublic.com` so they apply to both api.*
    and app.* subdomains.

    Returns the number of cookies written.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    jar = MozillaCookieJar(str(path))
    # Use a long-future expiry — pytr-style. The real expiry is enforced
    # server-side via session refresh anyway.
    expires = int(time.time()) + 365 * 24 * 3600

    for name, value in cookies.items():
        jar.set_cookie(
            Cookie(
                version=0,
                name=name,
                value=value,
                port=None,
                port_specified=False,
                domain=".traderepublic.com",
                domain_specified=True,
                domain_initial_dot=True,
                path="/",
                path_specified=True,
                secure=True,
                expires=expires,
                discard=False,
                comment=None,
                comment_url=None,
                rest={},
                rfc2109=False,
            )
        )

    # Atomic write: cookies are session secrets, so create the real file with
    # 0600 already in place (write to a sibling .tmp, chmod, then rename).
    # Chmod-after-write on the final path leaves a umask-race window where the
    # file is briefly world-readable on a shared host.
    tmp = path.with_name(path.name + ".tmp")
    jar.save(str(tmp), ignore_discard=True, ignore_expires=True)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return len(jar)


def load_from_file(path: Path | str) -> MozillaCookieJar:
    """Load a previously-saved cookies file into a MozillaCookieJar.

    Caller can assign this to `requests.Session().cookies`.
    """
    path = Path(path)
    jar = MozillaCookieJar(str(path))
    jar.load(ignore_discard=True, ignore_expires=True)
    return jar


# ---------------------------------------------------------------------------
# Inspection helpers (mostly for CLI / debugging)
# ---------------------------------------------------------------------------
def summarize(cookies: dict[str, str]) -> dict[str, object]:
    """Return a small dict describing what's in the cookie set, safe to print
    (does not include cookie values).
    """
    have = set(cookies)
    return {
        "total": len(cookies),
        "required_present": sorted(REQUIRED_AUTH_COOKIES & have),
        "required_missing": sorted(REQUIRED_AUTH_COOKIES - have),
        "useful_present":   sorted(USEFUL_COOKIES & have),
        "extras":           sorted(have - REQUIRED_AUTH_COOKIES - USEFUL_COOKIES),
    }
