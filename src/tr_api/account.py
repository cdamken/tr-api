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
    """Tolerant view of /api/v2/auth/account. Every field is optional.

    Field mapping verified against the live response (May 2026):
      personId           → person_id
      name {first,last}  → user_name (joined)
      phoneNumber        → phone_number
      jurisdiction       → jurisdiction
      postalAddress.country → country_of_residence
      mainNationality    → nationality
      cashAccount.iban   → iban
      securitiesAccountNumber → securities_account_number
      birthdate          → birthdate
      email.address      → email
    Many other fields (taxInformation, experience, …) live in `raw`.
    """
    person_id: str | None = None
    user_name: str | None = None
    phone_number: str | None = None
    jurisdiction: str | None = None
    country_of_residence: str | None = None
    nationality: str | None = None
    iban: str | None = None
    securities_account_number: str | None = None
    birthdate: str | None = None
    email: str | None = None
    raw: dict[str, Any] | None = None


def fetch(client: TrClient) -> dict[str, Any]:
    """Raw response from /api/v2/auth/account.

    Use this when you need a field that AccountSummary doesn't expose.
    Raises SessionExpired if cookies are dead, ApiError for other failures.
    """
    return client.get_json(ENDPOINT)


def summary(client: TrClient) -> AccountSummary:
    """Pick out the commonly-useful fields. Every field tolerates absence."""
    data = fetch(client)

    name_obj = data.get("name") or {}
    if isinstance(name_obj, dict):
        parts = [name_obj.get("first"), name_obj.get("last")]
        user_name = " ".join(p for p in parts if p) or None
    elif isinstance(name_obj, str):
        user_name = name_obj or None
    else:
        user_name = None

    addr = data.get("postalAddress") or {}
    country = addr.get("country") if isinstance(addr, dict) else None

    cash_acc = data.get("cashAccount") or {}
    iban = cash_acc.get("iban") if isinstance(cash_acc, dict) else None

    email_obj = data.get("email") or {}
    email = email_obj.get("address") if isinstance(email_obj, dict) else None

    return AccountSummary(
        person_id=data.get("personId") or data.get("userId"),
        user_name=user_name,
        phone_number=data.get("phoneNumber"),
        jurisdiction=data.get("jurisdiction"),
        country_of_residence=country,
        nationality=data.get("mainNationality"),
        iban=iban,
        securities_account_number=data.get("securitiesAccountNumber"),
        birthdate=data.get("birthdate"),
        email=email,
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
