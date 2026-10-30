"""Money helpers. All amounts are Decimal internally and strings on the wire."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

# Currencies PayPal REST APIs accept (developer.paypal.com/api/rest/reference/currency-codes/).
SUPPORTED_CURRENCIES = {
    "AUD", "BRL", "CAD", "CNY", "CZK", "DKK", "EUR", "HKD", "HUF", "ILS", "JPY",
    "MYR", "MXN", "TWD", "NZD", "NOK", "PHP", "PLN", "GBP", "SGD", "SEK", "CHF",
    "THB", "USD",
}
# PayPal does not allow decimals for these.
ZERO_DECIMAL = {"JPY", "HUF", "TWD"}
# Supported only for in-country accounts; we warn instead of failing.
RESTRICTED = {"CNY": "CNY is only supported for in-country (China) PayPal accounts",
              "BRL": "BRL is only supported for in-country (Brazil) PayPal accounts",
              "MYR": "MYR is only supported for in-country (Malaysia) PayPal accounts"}


def to_decimal(value) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None


def quantize(amount: Decimal, currency: str) -> Decimal:
    exp = Decimal("1") if currency in ZERO_DECIMAL else Decimal("0.01")
    return amount.quantize(exp, rounding=ROUND_HALF_UP)


def fmt(amount, currency: str) -> str:
    """Format for the PayPal API: '12.50' or '1250' for zero-decimal currencies."""
    d = to_decimal(amount)
    if d is None:
        d = Decimal("0")
    return str(quantize(d, currency))


def money(amount, currency: str) -> dict:
    return {"currency_code": currency, "value": fmt(amount, currency)}
