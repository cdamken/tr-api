"""Authenticated HTTP client for Trade Republic's REST API.

Design principles (the whole reason this library exists):

1. **We do NOT log in.** /api/v2/auth/web/login triggers WAF rate-limiting
   on headless/Playwright fingerprints. Real Chrome works fine. So the user
   logs in via real Chrome, and we inherit the cookies.

2. **Cookie-only auth.** No PIN, no MFA, no /auth/web/login round-trip.
   Just a MozillaCookieJar loaded from the active profile's cookies.txt.

3. **CSRF-correct headers.** TR rejects requests that don't look like they
   came from app.traderepublic.com. We send Origin, Referer, Sec-Fetch-*,
   and a real-Chrome User-Agent.

4. **Honest failure on 401.** When TR says "your session is dead", we
   raise SessionExpired and tell the user to re-import from Chrome. No
   silent token refresh that might mask a real auth problem.

Usage:

    from tr_api import TrClient
    c = TrClient.from_active()
    data = c.get_json("/api/v2/auth/account")
"""
from __future__ import annotations

from typing import Any

import requests

from . import cookies as _cookies
from . import profiles
from .exceptions import ApiError, MissingSessionCookies, SessionExpired
from .profiles import Profile

API_BASE = "https://api.traderepublic.com"
APP_ORIGIN = "https://app.traderepublic.com"

# A real Chrome-on-macOS UA. We match the format TR's frontend sends so we
# don't show up as obviously-different from a normal browser session.
# Bump this occasionally as Chrome major versions advance.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

# Default per-request timeout in seconds. TR is usually <1s for account
# endpoints but the timeline endpoint can be slower.
DEFAULT_TIMEOUT = 20.0


class TrClient:
    """REST client for api.traderepublic.com using cookies from a profile.

    Construct via one of the classmethods rather than calling __init__ directly:

        TrClient.from_active()        # uses ~/.tr-api/active
        TrClient.from_phone("+49…")   # specific profile
        TrClient.from_profile(prof)   # already-loaded Profile

    The client is a thin wrapper over requests.Session — you can reach in
    via `.session` if you need to do something custom.
    """

    def __init__(self, profile: Profile, *, timeout: float = DEFAULT_TIMEOUT):
        self.profile = profile
        self.timeout = timeout

        self.session = requests.Session()
        self._load_cookies()
        self.session.headers.update(self._default_headers())

    # -----------------------------------------------------------------
    # Constructors
    # -----------------------------------------------------------------
    @classmethod
    def from_active(cls, **kw: Any) -> TrClient:
        """Build a client for the currently-active profile."""
        return cls(profiles.get_active(), **kw)

    @classmethod
    def from_phone(cls, phone: str, **kw: Any) -> TrClient:
        """Build a client for a specific profile by phone number."""
        return cls(profiles.load(phone), **kw)

    @classmethod
    def from_profile(cls, profile: Profile, **kw: Any) -> TrClient:
        """Build a client from an already-loaded Profile."""
        return cls(profile, **kw)

    # -----------------------------------------------------------------
    # Setup
    # -----------------------------------------------------------------
    def _load_cookies(self) -> None:
        """Load cookies from the profile's cookies.txt into the session.

        Raises MissingSessionCookies if the file doesn't exist or doesn't
        contain the required auth cookies. (We validate eagerly so the
        caller sees the problem before making a request.)
        """
        path = self.profile.cookies_file
        if not path.is_file():
            raise MissingSessionCookies(
                f"No cookies file at {path}.\n"
                f"Run: tr-api auth import --phone {self.profile.phone}"
            )
        jar = _cookies.load_from_file(path)
        self.session.cookies = jar

        # Validate while we're at it.
        names = {c.name for c in jar}
        missing = _cookies.REQUIRED_AUTH_COOKIES - names
        if missing:
            raise MissingSessionCookies(
                f"Cookies file {path} is missing required auth cookies: "
                f"{sorted(missing)}. Re-import from Chrome."
            )

    def _default_headers(self) -> dict[str, str]:
        """Headers we attach to every request.

        The Sec-Fetch-* and Origin/Referer trio is what tells TR's WAF
        that we're a same-site XHR from app.traderepublic.com (which is
        what their real frontend sends). Without these you'll get blocked.
        """
        return {
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": APP_ORIGIN,
            "Referer": APP_ORIGIN + "/",
            "Sec-Fetch-Site": "same-site",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
            # Modern Chrome client hints — TR doesn't strictly require these
            # but real browsers send them and they're cheap to include.
            "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not=A?Brand";v="24"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"macOS"',
        }

    # -----------------------------------------------------------------
    # Request helpers
    # -----------------------------------------------------------------
    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
        data: Any | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> requests.Response:
        """Make an authenticated request and translate TR errors.

        `path` may be an absolute URL or a path like "/api/v2/auth/account".

        Raises:
            SessionExpired: TR returned 401 — cookies are dead.
            ApiError:       Any other non-2xx response, or a network failure.
        """
        url = path if path.startswith("http") else API_BASE + path
        try:
            resp = self.session.request(
                method,
                url,
                params=params,
                json=json,
                data=data,
                headers=headers,
                timeout=timeout if timeout is not None else self.timeout,
            )
        except requests.RequestException as e:
            raise ApiError(f"Network error talking to {url}: {e}") from e

        if resp.status_code == 401:
            raise SessionExpired(
                "Trade Republic returned 401 — your session cookies are no "
                "longer valid. Re-import them from Chrome:\n"
                f"  tr-api auth import --phone {self.profile.phone}\n"
                "(First open https://app.traderepublic.com in Chrome and "
                "make sure you're logged in.)"
            )

        if resp.status_code == 429:
            raise ApiError(
                "Trade Republic returned 429 (rate limited). This usually "
                "means the WAF flagged the request fingerprint. Wait a few "
                "minutes and try again; if it persists, re-import cookies "
                "from a fresh Chrome session.",
                status_code=429,
                body=resp.text[:1000],
            )

        if not (200 <= resp.status_code < 300):
            raise ApiError(
                f"Trade Republic returned {resp.status_code} for "
                f"{method} {url}",
                status_code=resp.status_code,
                body=resp.text[:1000],
            )

        return resp

    def get(self, path: str, **kw: Any) -> requests.Response:
        return self.request("GET", path, **kw)

    def post(self, path: str, **kw: Any) -> requests.Response:
        return self.request("POST", path, **kw)

    def get_json(self, path: str, **kw: Any) -> Any:
        """GET and parse the response body as JSON.

        Raises ApiError if the body isn't valid JSON.
        """
        resp = self.get(path, **kw)
        try:
            return resp.json()
        except ValueError as e:
            raise ApiError(
                f"Expected JSON from {path}, got non-JSON body",
                status_code=resp.status_code,
                body=resp.text[:1000],
            ) from e

    def post_json(self, path: str, **kw: Any) -> Any:
        resp = self.post(path, **kw)
        try:
            return resp.json()
        except ValueError as e:
            raise ApiError(
                f"Expected JSON from {path}, got non-JSON body",
                status_code=resp.status_code,
                body=resp.text[:1000],
            ) from e

    # -----------------------------------------------------------------
    # Misc
    # -----------------------------------------------------------------
    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> TrClient:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"TrClient(profile={self.profile.phone!r})"
