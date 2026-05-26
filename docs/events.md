# Event types

Every item TR returns from `timelineTransactions` (and the soon-to-be-wrapped
`timelineActivityLog`) is tagged with an `eventType` string. This doc is
the catalogue: what each one means and where it shows up.

Verified against live TR responses through **May 2026**. TR adds and
deprecates these over time; the list below covers what dashboards
typically care about. If you see an `eventType` that's not here, open a
PR adding a row.

---

## Topic membership

| `eventType` | Topic where it appears | Notes |
|---|---|---|
| `INCOMING_TRANSFER` | `timelineTransactions` | SEPA / bank transfer in. |
| `INCOMING_TRANSFER_DELEGATION` | `timelineTransactions` | Inbound via the "delegation" rails (e.g. salary auto-deposit). |
| `OUTGOING_TRANSFER` | `timelineTransactions` | SEPA out to a non-TR account. |
| `OUTGOING_TRANSFER_DELEGATION` | `timelineTransactions` | Outbound via the delegation rails. |
| `PAYMENT_INBOUND` | `timelineTransactions` | Generic payment in (rare; usually inbound has more specific type). |
| `PAYMENT_INBOUND_SEPA_DIRECT_DEBIT` | `timelineTransactions` | SEPA direct debit *to* the user. |
| `PAYMENT_OUTBOUND` | `timelineTransactions` | Generic payment out. |
| `CARD_TRANSACTION` | `timelineTransactions` | Card purchase (the current event type as of May 2026). |
| `card_successful_transaction` | `timelineTransactions` | Older form of the same event. Both sometimes appear in the same account's history. |
| `card_refund` | `timelineTransactions` | Refund credited back to the card. |
| `CARD_REFUND` | `timelineTransactions` | Newer form. |
| `INTEREST_PAYOUT` | `timelineTransactions` | Interest on uninvested cash. |
| `INTEREST_PAYOUT_CREATED` | `timelineTransactions` | The "interest will be paid" pre-credit notification. Same amount may also appear as `INTEREST_PAYOUT` on the actual settlement date. |
| `TAX_REFUND` | `timelineTransactions` | Refund of withheld tax (e.g. corrected dividend withholding). |
| `ssp_tax_correction_invoice` | `timelineTransactions` | Tax correction document (usually accompanied by a `TAX_REFUND`). |
| `TRADE_INVOICE` | **`timelineActivityLog`** | Stock / ETF / crypto buy or sell. Amount sign tells you direction. |
| `ORDER_EXECUTED` | **`timelineActivityLog`** | Same as above with a different code path; some accounts get one, some get the other. |
| `DIVIDEND` | **`timelineActivityLog`** | Cash dividend payment. |
| `CREDIT` | **`timelineActivityLog`** | Generic credit (typically a dividend or coupon — TR uses this for some issuers). |
| `ssp_corporate_action_invoice_cash` | **`timelineActivityLog`** | Cash from a corporate action (e.g. partial redemption, spinoff cash component). |

**Note on the gap**: see [docs/websocket-topics.md](websocket-topics.md) —
`tr-api` currently only wraps `timelineTransactions`. Until
`activity_log.py` ships, anything in the "topic = `timelineActivityLog`"
rows above is **not** returned by `tr_api.transactions.fetch_*`.

---

## Common payload fields

```python
{
    "id":         "abcd-1234-…",         # opaque, unique per timeline item
    "timestamp":  "2026-05-20T14:32:11.000Z",
    "eventType":  "CARD_TRANSACTION",
    "title":      "Aldi",                # human-readable summary
    "subtitle":   "Card Payment - Aldi",
    "amount":     {"value": -30.38, "currency": "EUR"},
    "status":     "EXECUTED",            # or PENDING, REJECTED, ...
    "icon":       "logos/CARD/v2",       # also includes ISIN for instrument events:
                                         #   logos/<ISIN>/v2  for trades/dividends
    "action": {                          # optional — present when there's a detail page
        "type": "timelineDetail",
        "payload": "<timeline-item-id>"
    },
    "eventTime":  "2026-05-20T14:32:11.000Z"  # sometimes present as well as timestamp
}
```

Notes:

- `amount.value` is signed. Negative = money left the account, positive
  = money came in. Use the sign (not the `eventType`) to classify
  buy-vs-sell on `TRADE_INVOICE` / `ORDER_EXECUTED`.
- `timestamp` is always present on real events. Some pre-credited
  notifications (e.g. `INTEREST_PAYOUT_CREATED`) carry `eventTime`
  instead. `tr_api.transactions._parse_ts()` tolerates both.
- `icon` is the most reliable place to extract an ISIN for
  instrument-bound events. The path looks like `logos/<ISIN>/v2` where
  `<ISIN>` is the 12-char ISO 6166 identifier. The TR backend also
  shows `Instrument` data via the `timelineDetailV2` topic if you need
  more.

---

## Classification helpers

When mapping to dashboard rows, you usually want a coarser category:

```python
EVENT_TYPE_MAP = {
    # Cash in
    "INCOMING_TRANSFER":            "Deposit",
    "INCOMING_TRANSFER_DELEGATION": "Deposit",
    "PAYMENT_INBOUND":              "Deposit",
    "PAYMENT_INBOUND_SEPA_DIRECT_DEBIT": "Deposit",
    "card_refund":                  "Deposit",
    "CARD_REFUND":                  "Deposit",
    # Cash out
    "CARD_TRANSACTION":             "Removal",
    "card_successful_transaction":  "Removal",
    "OUTGOING_TRANSFER":            "Removal",
    "OUTGOING_TRANSFER_DELEGATION": "Removal",
    "PAYMENT_OUTBOUND":             "Removal",
    # Tax flows
    "ssp_tax_correction_invoice":   "Tax Refund",
    "TAX_REFUND":                   "Tax Refund",
    # Trading — needs amount-sign check to split Buy vs Sell
    "TRADE_INVOICE":                "Trade",
    "ORDER_EXECUTED":               "Trade",
    # Income
    "CREDIT":                       "Dividend",
    "DIVIDEND":                     "Dividend",
    "ssp_corporate_action_invoice_cash": "Dividend",
    "INTEREST_PAYOUT":              "Interest",
    "INTEREST_PAYOUT_CREATED":      "Interest",
}

def classify_trade(event):
    """For TRADE_INVOICE / ORDER_EXECUTED: 'Buy' if money left, 'Sell' if it came in."""
    amount = event.get("amount") or {}
    val = amount.get("value")
    if isinstance(val, (int, float)):
        return "Buy" if val < 0 else "Sell"
    return None
```

`trade-republic-dashboard` and `trade-republic-owncloud` both use this
exact map.

---

## Less common types — open questions

These show up occasionally and `tr-api` doesn't have a stable opinion
yet. Patches welcome.

| `eventType` | Topic | Observed meaning |
|---|---|---|
| `SAVINGS_PLAN_EXECUTED` | `timelineActivityLog` (probably) | A recurring buy plan fired. |
| `CRYPTO_INVOICE` | `timelineActivityLog` (probably) | A crypto buy/sell. |
| `BENEFITS_SPARE_CHANGE_EXECUTION` | `timelineTransactions` | The "round-up" feature buying small fractional shares. |
| `BENEFITS_SAVEBACK_EXECUTION` | `timelineTransactions` | Saveback cashback applied as a buy. |
| `card_failed_transaction` | `timelineTransactions` | A declined card transaction. |
| `card_order_billed` | `timelineTransactions` | Card-issuance fee billed (one-time). |

If you spot one of these in your account and want it categorised, open
an issue with a sanitised example (id + eventType + amount + title is
enough; we don't need the rest of the payload).
