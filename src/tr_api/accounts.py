"""TR multi-account discovery via the ``accountPairs`` WS topic.

Most Trade Republic users have a single (securitiesAccount, cashAccount)
pair with ``productType: "DEFAULT"``. Users with extra products — joint
accounts, private-fund wrappers, tax wrappers (FR), separate crypto
account — have multiple pairs and each ``compactPortfolioByType`` call
must target a specific ``securitiesAccountNumber`` via the
``secAccNo`` argument.

Topic shape::

    {
      "authAccountId": "<uuid>",
      "accounts": [
        {
          "securitiesAccountNumber": "0458381101",
          "cashAccountNumber":       "0458381111",
          "productType":             "DEFAULT",   // or TAX_WRAPPER, CRYPTO, PRIVATE_EQUITY, …
          "currency":                "EUR",
          "accountAccessType":       "OWNER"
        },
        ...
      ]
    }

We expose a typed ``AccountPair`` dataclass and a sync wrapper
``account_pairs(client)`` so downstream code never has to remember the
topic string or the field names.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from .client import TrClient
from .protocol import TrWebSocket

TOPIC = "accountPairs"


@dataclass
class AccountPair:
    """One (securities, cash) account pair plus its product classification."""
    securities_account_number: str
    cash_account_number: str
    product_type: str          # e.g. "DEFAULT", "TAX_WRAPPER", "CRYPTO", "PRIVATE_EQUITY"
    currency: str              # ISO code, e.g. "EUR"
    access_type: str           # "OWNER" / "JOINT" / etc.
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_raw(cls, d: dict[str, Any]) -> "AccountPair":
        return cls(
            securities_account_number=str(d.get("securitiesAccountNumber") or ""),
            cash_account_number=str(d.get("cashAccountNumber") or ""),
            product_type=str(d.get("productType") or ""),
            currency=str(d.get("currency") or ""),
            access_type=str(d.get("accountAccessType") or ""),
            raw=d,
        )


@dataclass
class AccountsResponse:
    """Full ``accountPairs`` answer plus a typed list of pairs."""
    auth_account_id: str
    pairs: list[AccountPair]
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_raw(cls, d: dict[str, Any]) -> "AccountsResponse":
        return cls(
            auth_account_id=str(d.get("authAccountId") or ""),
            pairs=[AccountPair.from_raw(p) for p in (d.get("accounts") or [])],
            raw=d,
        )

    def default_pair(self) -> AccountPair | None:
        """Convenience: the first ``DEFAULT`` pair, or the first pair at all."""
        for p in self.pairs:
            if p.product_type == "DEFAULT":
                return p
        return self.pairs[0] if self.pairs else None


# ---------------------------------------------------------------------------
# Async core
# ---------------------------------------------------------------------------
async def _fetch_async(cookie_jar: Any) -> dict[str, Any]:
    async with TrWebSocket(cookie_jar) as ws:
        return await ws.fetch_one({"type": TOPIC}, timeout=10.0)


# ---------------------------------------------------------------------------
# Public sync wrapper
# ---------------------------------------------------------------------------
def account_pairs(client: TrClient) -> AccountsResponse:
    """List the (securitiesAccount, cashAccount) pairs for this user.

    Single-account users get back a single-pair list with
    ``product_type == "DEFAULT"``. Multi-account users get one entry per
    product. Downstream code that calls ``compactPortfolioByType`` should
    iterate the list and pass each ``securities_account_number`` to that
    topic explicitly.
    """
    raw = asyncio.run(_fetch_async(client.session.cookies))
    return AccountsResponse.from_raw(raw)
