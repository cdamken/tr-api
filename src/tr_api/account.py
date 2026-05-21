"""Account info via /api/v2/auth/account.

This is the smallest authenticated endpoint TR exposes, which makes it
useful for two things:

1. Reading basic account info (name, jurisdiction, phone).
2. Testing whether the session cookies still work — `ping()` returns a
   bool without raising.

The endpoint returns a JSON object with at least these fields (observed
in real-Chrome traffic; TR doesn't publish a schema, so we treat all
fields as optional):

    userId, userName, phoneNumber, jurisdiction, countryOfResidence,
    language, currencyId, experienceFlowCompleted, …

We never depend on a field being present — `summary()` uses .get() and
returns None for missing values. This way a minor backend change doesn't
crash the library.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .client import TrClient
from .exceptions import SessionExpired, TrApiError

ENDPOINT = "/api/v2/auth/account"


@dataclass
class AccountSummary:
    """Tolerant view of /api/v2/auth/account. Every field is optional."""
    user_id: str | None = None
    user_name: str | None = None
    phone_number: str | None = None
    jurisdiction: str | None = None       # "DE", "AT", …
    country_of_residence: str | None = None
    language: str | None = None           # "en", "de", …
    currency: str | None = None           # "EUR", …
    experience_flow_completed: bool | None = None
    raw: dict[str, Any] | None = None     # the full response, in case caller wants more


def fetch(client: TrClient) -> dict[str, Any]:
    """Raw response from /api/v2/auth/account.

    Use this when you need a field that AccountSummary doesn't expose.
    Raises SessionExpired if cookies are dead, ApiError for other failures.
    """
    return client.get_json(ENDPOINT)


def summary(client: TrClient) -> AccountSummary:
    """Pick out the commonly-useful fields. All fields tolerate absence."""
    data = fetch(client)
    return AccountSummary(
        user_id=data.get("userId"),
        user_name=data.get("userName"),
        phone_number=data.get("phoneNumber"),
        jurisdiction=data.get("jurisdiction"),
        country_of_residence=data.get("countryOfResidence"),
        language=data.get("language"),
        currency=data.get("currencyId") or data.get("currency"),
        experience_flow_completed=data.get("experienceFlowCompleted"),
        raw=data,
    )


def ping(client: TrClient) -> bool:
    """Return True if the cookies are still good, False otherwise.

    Does NOT raise on SessionExpired — instead returns False. Other
    exceptions (network errors, ApiError for non-401) still propagate,
    since they indicate a problem the caller probably wants to surface.

    Typical use in a dashboard:

        if not account.ping(client):
            # show "please re-import cookies" UI
    """
    try:
        client.get(ENDPOINT)
        return True
    except SessionExpired:
        return False
    except TrApiError:
        # Network down, 5xx, etc. — re-raise; that's not "session expired".
        raise
