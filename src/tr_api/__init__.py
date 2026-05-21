"""tr-api — minimal Python client for Trade Republic, backed by browser cookies.

Public surface:

    from tr_api import TrClient, Profile
    from tr_api import TrApiError, SessionExpired, MissingSessionCookies

    c = TrClient.from_active()
    account = c.get_json("/api/v2/auth/account")

Sub-modules also re-exported for convenience:

    from tr_api import cookies, profiles

See README.md for the overall architecture.
"""
from __future__ import annotations

from . import cookies, profiles
from .client import API_BASE, APP_ORIGIN, TrClient
from .exceptions import (
    ApiError,
    AuthError,
    ChromeNotFound,
    CookieError,
    KeychainAccessDenied,
    MissingSessionCookies,
    NoActiveProfile,
    ProfileError,
    ProfileNotFound,
    SessionExpired,
    TrApiError,
)
from .profiles import Profile

__version__ = "0.1.0"

__all__ = [
    # Client
    "TrClient",
    "API_BASE",
    "APP_ORIGIN",
    # Profile
    "Profile",
    # Sub-modules
    "cookies",
    "profiles",
    # Exceptions
    "TrApiError",
    "CookieError",
    "ChromeNotFound",
    "KeychainAccessDenied",
    "MissingSessionCookies",
    "ProfileError",
    "ProfileNotFound",
    "NoActiveProfile",
    "AuthError",
    "SessionExpired",
    "ApiError",
    # Meta
    "__version__",
]
