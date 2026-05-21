"""Exception hierarchy for tr-api.

All errors raised by the library are subclasses of TrApiError, so a caller
can `except TrApiError` to catch everything.
"""
from __future__ import annotations


class TrApiError(Exception):
    """Base class for all tr-api errors."""


# ---------------------------------------------------------------------------
# Cookie / session errors
# ---------------------------------------------------------------------------
class CookieError(TrApiError):
    """Something is wrong with reading or writing cookies."""


class ChromeNotFound(CookieError):
    """Could not locate a Chrome cookies database."""


class KeychainAccessDenied(CookieError):
    """macOS Keychain refused access to Chrome Safe Storage."""


class MissingSessionCookies(CookieError):
    """The user is not logged in to TR in Chrome (no JSESSIONID / tr_refresh).

    Hint: open https://app.traderepublic.com and log in, then retry.
    """


# ---------------------------------------------------------------------------
# Profile errors
# ---------------------------------------------------------------------------
class ProfileError(TrApiError):
    """Profile management failed."""


class ProfileNotFound(ProfileError):
    """Asked for a profile that doesn't exist on disk."""


class NoActiveProfile(ProfileError):
    """No default profile is set. Run `tr-api profiles use <phone>` first."""


# ---------------------------------------------------------------------------
# API / auth errors
# ---------------------------------------------------------------------------
class AuthError(TrApiError):
    """Trade Republic refused our authenticated request."""


class SessionExpired(AuthError):
    """TR returned 401 — cookies are no longer valid.

    Caller should prompt the user to re-import cookies from a fresh browser
    session (re-login on app.traderepublic.com).
    """


class ApiError(TrApiError):
    """TR returned an unexpected response."""

    def __init__(self, message: str, status_code: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body
