"""PayPal layer: real sandbox REST client + an offline mock with the same interface.

Real mode (``PAYPAL_CLIENT_ID`` and ``PAYPAL_CLIENT_SECRET`` set):
  * OAuth 2.0 client_credentials  POST /v1/oauth2/token  (token cached until expiry)
  * Orders v2                     POST/GET /v2/checkout/orders, POST .../{id}/capture
  * Invoicing v2                  POST /v2/invoicing/generate-next-invoice-number
                                  POST /v2/invoicing/invoices, POST .../{id}/send,
                                  GET .../{id}, POST .../{id}/payments
Mock mode (no credentials): ``MockPayPal`` stores everything in memory and answers
with the same JSON shapes, so the demo and the tests run with zero setup.

Both clients build request bodies with the same functions (``build_order_body``,
``build_invoice_body``), so the mock exercises exactly what is sent to PayPal.
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import os
import time
import urllib.error
import urllib.request
import uuid
from decimal import Decimal
from typing import Callable

from .money import fmt, money, quantize, to_decimal

SANDBOX_BASE = "https://api-m.sandbox.paypal.com"
NET_TERMS = {10, 15, 30, 45, 60, 90}


class PayPalError(Exception):
    def __init__(self, status: int, name: str, message: str, debug_id: str | None = None, details=None):
        super().__init__(f"PayPal {status} {name}: {message}" + (f" (debug_id {debug_id})" if debug_id else ""))
        self.status, self.name, self.message, self.debug_id, self.details = status, name, message, debug_id, details


# --------------------------------------------------------------------------- request bodies
def _items_and_totals(rec: dict):
    ccy = rec["currency"]
    tax_pct = to_decimal(rec.get("tax_percent"))
    items, item_total, tax_total = [], Decimal("0"), Decimal("0")
    for it in rec["items"]:
        unit = to_decimal(it["unit_price"])
        item = {"name": it["name"][:127], "quantity": str(it["quantity"]),
                "unit_amount": money(unit, ccy), "category": "PHYSICAL_GOODS"}
        item_total += quantize(unit, ccy) * it["quantity"]
        if tax_pct:
            t = quantize(unit * tax_pct / 100, ccy)
            item["tax"] = money(t, ccy)
            tax_total += t * it["quantity"]
        items.append(item)
    shipping = to_decimal(rec.get("shipping")) or Decimal("0")
    total = item_total + tax_total + shipping
    return items, item_total, tax_total, shipping, total


def build_order_body(rec: dict, return_url: str, cancel_url: str, brand_name: str | None = None) -> dict:
    ccy = rec["currency"]
    items, item_total, tax_total, shipping, total = _items_and_totals(rec)
    breakdown = {"item_total": money(item_total, ccy)}
    if shipping:
        breakdown["shipping"] = money(shipping, ccy)
    if tax_total:
        breakdown["tax_total"] = money(tax_total, ccy)
    ctx = {"user_action": "PAY_NOW", "shipping_preference": "NO_SHIPPING",
           "return_url": return_url, "cancel_url": cancel_url}
    if brand_name:
        ctx["brand_name"] = brand_name[:127]
    return {
        "intent": "CAPTURE",
        "purchase_units": [{
            "reference_id": rec["id"],
            "custom_id": rec["id"],
            "invoice_id": rec["id"],
            "description": (rec.get("description") or f"Order for {rec['customer'].get('name') or 'customer'}")[:127],
            "amount": {"currency_code": ccy, "value": fmt(total, ccy), "breakdown": breakdown},
            "items": items,
        }],
        "payment_source": {"paypal": {"experience_context": ctx}},
    }


def build_invoice_body(rec: dict, invoice_number: str, today: dt.date | None = None,
                       business_name: str | None = None) -> dict:
    ccy = rec["currency"]
    today = today or dt.date.today()
    tax_pct = to_decimal(rec.get("tax_percent"))
    days = rec.get("due_days")
    if days is None:
        term = {"term_type": "NET_10"}
    elif days == 0:
        term = {"term_type": "DUE_ON_RECEIPT"}
    elif days in NET_TERMS:
        term = {"term_type": f"NET_{days}"}
    else:
        term = {"term_type": "DUE_ON_DATE_SPECIFIED", "due_date": (today + dt.timedelta(days=days)).isoformat()}
    detail = {"invoice_number": invoice_number, "invoice_date": today.isoformat(), "currency_code": ccy,
              "payment_term": term}
    if rec.get("note"):
        detail["note"] = rec["note"][:4000]
    if rec.get("memo"):
        detail["memo"] = rec["memo"][:500]
    name = (rec["customer"].get("name") or "").strip()
    billing = {"email_address": rec["customer"]["email"]}
    if name:
        given, _, surname = name.partition(" ")
        billing["name"] = {"given_name": given[:140], **({"surname": surname[:140]} if surname else {})}
    items = []
    for it in rec["items"]:
        item = {"name": it["name"][:200], "quantity": str(it["quantity"]),
                "unit_amount": money(it["unit_price"], ccy), "unit_of_measure": "QUANTITY"}
        if tax_pct:
            item["tax"] = {"name": "Tax", "percent": str(tax_pct)}
        items.append(item)
    body = {"detail": detail, "primary_recipients": [{"billing_info": billing}], "items": items}
    shipping = to_decimal(rec.get("shipping"))
    if shipping:
        body["amount"] = {"breakdown": {"shipping": {"amount": money(shipping, ccy)}}}
    if business_name:
        body["invoicer"] = {"business_name": business_name[:300]}
    return body


def link(resource: dict, *rels) -> str | None:
    for l in resource.get("links", []) or []:
        if l.get("rel") in rels:
            return l.get("href")
    return None


# --------------------------------------------------------------------------- real client
class PayPalClient:
    mode = "sandbox"

    def __init__(self, client_id: str, client_secret: str, base_url: str = SANDBOX_BASE,
                 transport: Callable | None = None, timeout: float = 30):
        if not client_id or not client_secret:
            raise ValueError("client_id and client_secret are required")
        self.client_id, self.client_secret = client_id, client_secret
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.transport = transport or self._http
        self._token: str | None = None
        self._token_exp = 0.0
        self.calls: list[dict] = []  # audit log shown in the UI

    # transport(method, url, headers, data_bytes) -> (status, body_text)
    def _http(self, method, url, headers, data):
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(errors="replace")

    def _log(self, method, path, status, t0, debug_id=None):
        self.calls.append({"method": method, "path": path, "status": status,
                           "ms": int((time.time() - t0) * 1000), "debug_id": debug_id})

    def token(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        t0 = time.time()
        status, text = self.transport("POST", f"{self.base_url}/v1/oauth2/token",
                                      {"Authorization": f"Basic {basic}", "Accept": "application/json",
                                       "Content-Type": "application/x-www-form-urlencoded"},
                                      b"grant_type=client_credentials")
        self._log("POST", "/v1/oauth2/token", status, t0)
        data = _json(text)
        if status != 200 or "access_token" not in data:
            raise PayPalError(status, data.get("error", "AUTH_FAILED"),
                              data.get("error_description", "could not get access token"))
        self._token = data["access_token"]
        self._token_exp = time.time() + int(data.get("expires_in", 300))
        return self._token

    def request(self, method: str, path: str, body: dict | None = None, idempotency_key: str | None = None,
                prefer_representation: bool = True) -> dict:
        headers = {"Authorization": f"Bearer {self.token()}", "Content-Type": "application/json",
                   "Accept": "application/json"}
        if prefer_representation:
            headers["Prefer"] = "return=representation"
        if idempotency_key:
            headers["PayPal-Request-Id"] = idempotency_key
        data = json.dumps(body).encode() if body is not None else (b"{}" if method == "POST" else None)
        t0 = time.time()
        status, text = self.transport(method, f"{self.base_url}{path}", headers, data)
        payload = _json(text)
        self._log(method, path, status, t0, payload.get("debug_id") if isinstance(payload, dict) else None)
        if status >= 400:
            details = payload.get("details") if isinstance(payload, dict) else None
            msg = payload.get("message", text[:200]) if isinstance(payload, dict) else text[:200]
            if details:
                msg += " | " + "; ".join(f"{d.get('field', '')} {d.get('issue', '')} {d.get('description', '')}".strip()
                                         for d in details[:3])
            raise PayPalError(status, payload.get("name", "ERROR") if isinstance(payload, dict) else "ERROR",
                              msg, payload.get("debug_id") if isinstance(payload, dict) else None, details)
        return payload if isinstance(payload, dict) else {}

    # ---- Orders v2
    def create_order(self, body: dict, request_id: str) -> dict:
        return self.request("POST", "/v2/checkout/orders", body, idempotency_key=request_id)

    def get_order(self, order_id: str) -> dict:
        return self.request("GET", f"/v2/checkout/orders/{order_id}")

    def capture_order(self, order_id: str, request_id: str | None = None) -> dict:
        return self.request("POST", f"/v2/checkout/orders/{order_id}/capture",
                            idempotency_key=request_id or f"cap-{order_id}")

    # ---- Invoicing v2
    def next_invoice_number(self) -> str:
        return self.request("POST", "/v2/invoicing/generate-next-invoice-number")["invoice_number"]

    def create_invoice(self, body: dict) -> dict:
        res = self.request("POST", "/v2/invoicing/invoices", body)
        if "id" not in res and res.get("href"):  # older behaviour: returns a link only
            res = self.get_invoice(res["href"].rstrip("/").rsplit("/", 1)[-1])
        return res

    def send_invoice(self, invoice_id: str) -> dict:
        return self.request("POST", f"/v2/invoicing/invoices/{invoice_id}/send",
                            {"send_to_invoicer": True, "send_to_recipient": True},
                            idempotency_key=f"send-{invoice_id}", prefer_representation=False)

    def get_invoice(self, invoice_id: str) -> dict:
        return self.request("GET", f"/v2/invoicing/invoices/{invoice_id}")

    def record_invoice_payment(self, invoice_id: str, amount: str, currency: str, method: str = "BANK_TRANSFER",
                               note: str | None = None, date: str | None = None) -> dict:
        body = {"method": method, "payment_date": date or dt.date.today().isoformat(),
                "amount": {"currency_code": currency, "value": fmt(amount, currency)}}
        if note:
            body["note"] = note[:150]
        return self.request("POST", f"/v2/invoicing/invoices/{invoice_id}/payments", body,
                            prefer_representation=False)


def _json(text: str):
    try:
        return json.loads(text) if text else {}
    except ValueError:
        return {"message": text[:300]}


# --------------------------------------------------------------------------- mock
class MockPayPal:
    """In-memory stand-in that answers with PayPal-shaped JSON. No network, no keys."""
    mode = "mock"
    base = "https://www.sandbox.paypal.com"

    def __init__(self):
        self.orders: dict[str, dict] = {}
        self.invoices: dict[str, dict] = {}
        self._n = 0
        self.calls: list[dict] = []
        self._authed = False

    def _log(self, method, path, status=200):
        if not self._authed:  # mirror the real client: first call of a session fetches an OAuth token
            self._authed = True
            self.calls.append({"method": "POST", "path": "/v1/oauth2/token", "status": 200, "ms": 0,
                               "debug_id": None, "mock": True})
        self.calls.append({"method": method, "path": path, "status": status, "ms": 0, "debug_id": None,
                           "mock": True})

    def _id(self, prefix):
        return prefix + uuid.uuid4().hex[:15].upper()

    def token(self):
        return "MOCK-TOKEN"

    def create_order(self, body, request_id):
        for o in self.orders.values():  # honour PayPal-Request-Id idempotency
            if o["_request_id"] == request_id:
                self._log("POST", "/v2/checkout/orders", 200)
                return _public(o)
        oid = self._id("MOCK")
        o = {"id": oid, "status": "PAYER_ACTION_REQUIRED", "intent": body["intent"],
             "purchase_units": json.loads(json.dumps(body["purchase_units"])), "_request_id": request_id,
             "links": [{"href": f"{self.base}/checkoutnow?token={oid}", "rel": "payer-action", "method": "GET"},
                       {"href": f"/v2/checkout/orders/{oid}", "rel": "self", "method": "GET"}]}
        self.orders[oid] = o
        self._log("POST", "/v2/checkout/orders", 200)
        return _public(o)

    def get_order(self, order_id):
        self._log("GET", f"/v2/checkout/orders/{order_id}")
        if order_id not in self.orders:
            raise PayPalError(404, "RESOURCE_NOT_FOUND", "order not found")
        return _public(self.orders[order_id])

    def capture_order(self, order_id, request_id=None):
        o = self.orders.get(order_id)
        if not o:
            raise PayPalError(404, "RESOURCE_NOT_FOUND", "order not found")
        if o["status"] == "COMPLETED":
            self._log("POST", f"/v2/checkout/orders/{order_id}/capture", 422)
            raise PayPalError(422, "UNPROCESSABLE_ENTITY", "ORDER_ALREADY_CAPTURED")
        if o["status"] != "APPROVED":
            self._log("POST", f"/v2/checkout/orders/{order_id}/capture", 422)
            raise PayPalError(422, "UNPROCESSABLE_ENTITY", "ORDER_NOT_APPROVED")
        pu = o["purchase_units"][0]
        paid = o.pop("_paid_value", None) or pu["amount"]["value"]
        pu["payments"] = {"captures": [{"id": self._id("CAP"), "status": "COMPLETED",
                                        "amount": {"currency_code": pu["amount"]["currency_code"], "value": paid}}]}
        o["status"] = "COMPLETED"
        self._log("POST", f"/v2/checkout/orders/{order_id}/capture", 201)
        return _public(o)

    def simulate_approval(self, order_id, paid_value: str | None = None):
        """What a buyer clicking 'Pay Now' with a sandbox personal account does."""
        o = self.orders[order_id]
        o["status"] = "APPROVED"
        if paid_value:
            o["_paid_value"] = paid_value

    def next_invoice_number(self):
        self._n += 1
        self._log("POST", "/v2/invoicing/generate-next-invoice-number")
        return f"INV-{self._n:04d}"

    def create_invoice(self, body):
        iid = self._id("INV2-")
        ccy = body["detail"]["currency_code"]
        sub = sum((to_decimal(i["unit_amount"]["value"]) * int(i["quantity"]) for i in body["items"]), Decimal("0"))
        tax = sum((quantize(to_decimal(i["unit_amount"]["value"]) * int(i["quantity"]) *
                            to_decimal(i["tax"]["percent"]) / 100, ccy) for i in body["items"] if i.get("tax")),
                  Decimal("0"))
        ship = to_decimal(body.get("amount", {}).get("breakdown", {}).get("shipping", {}).get("amount", {}).get("value")) or 0
        inv = {"id": iid, "status": "DRAFT", "detail": json.loads(json.dumps(body["detail"])),
               "primary_recipients": body["primary_recipients"], "items": body["items"],
               "amount": {"currency_code": ccy, "value": fmt(sub + tax + ship, ccy)},
               "due_amount": {"currency_code": ccy, "value": fmt(sub + tax + ship, ccy)},
               "payments": {"paid_amount": {"currency_code": ccy, "value": fmt(0, ccy)}, "transactions": []},
               "links": [{"href": f"/v2/invoicing/invoices/{iid}", "rel": "self", "method": "GET"}]}
        inv["detail"]["metadata"] = {"recipient_view_url": f"{self.base}/invoice/p/#{iid}"}
        self.invoices[iid] = inv
        self._log("POST", "/v2/invoicing/invoices", 201)
        return json.loads(json.dumps(inv))

    def send_invoice(self, invoice_id):
        inv = self._inv(invoice_id)
        inv["status"] = "SENT"
        self._log("POST", f"/v2/invoicing/invoices/{invoice_id}/send")
        return {"rel": "payer-view", "href": inv["detail"]["metadata"]["recipient_view_url"], "method": "GET"}

    def get_invoice(self, invoice_id):
        self._log("GET", f"/v2/invoicing/invoices/{invoice_id}")
        return json.loads(json.dumps(self._inv(invoice_id)))

    def record_invoice_payment(self, invoice_id, amount, currency, method="BANK_TRANSFER", note=None, date=None):
        inv = self._inv(invoice_id)
        self._apply_invoice_payment(inv, amount, method, external=True)
        self._log("POST", f"/v2/invoicing/invoices/{invoice_id}/payments", 200)
        return {"payment_id": self._id("EXTR-")}

    def simulate_invoice_paid(self, invoice_id, amount: str | None = None):
        """What the recipient paying the emailed invoice through PayPal does."""
        inv = self._inv(invoice_id)
        self._apply_invoice_payment(inv, amount or inv["due_amount"]["value"], "PAYPAL", external=False)

    def _apply_invoice_payment(self, inv, amount, method, external):
        ccy = inv["amount"]["currency_code"]
        paid = to_decimal(inv["payments"]["paid_amount"]["value"]) + to_decimal(amount)
        total = to_decimal(inv["amount"]["value"])
        inv["payments"]["paid_amount"]["value"] = fmt(paid, ccy)
        inv["payments"]["transactions"].append({"method": method, "amount": money(amount, ccy)})
        inv["due_amount"]["value"] = fmt(max(total - paid, Decimal("0")), ccy)
        if paid >= total:
            inv["status"] = "MARKED_AS_PAID" if external else "PAID"
        else:
            inv["status"] = "PARTIALLY_PAID"

    def _inv(self, invoice_id):
        if invoice_id not in self.invoices:
            raise PayPalError(404, "RESOURCE_NOT_FOUND", "invoice not found")
        return self.invoices[invoice_id]


def _public(o: dict) -> dict:
    return json.loads(json.dumps({k: v for k, v in o.items() if not k.startswith("_")}))


def get_paypal_client(env: dict | None = None):
    env = os.environ if env is None else env
    cid, secret = env.get("PAYPAL_CLIENT_ID"), env.get("PAYPAL_CLIENT_SECRET")
    if cid and secret and env.get("PAYPAL_MODE", "").lower() != "mock":
        return PayPalClient(cid, secret, env.get("PAYPAL_BASE_URL") or SANDBOX_BASE)
    return MockPayPal()
