"""The receivables agent: text -> AI extraction -> decision -> PayPal -> tracking -> reconciliation.

Every step is written to a trace (what the AI read, what it decided and why, and
each PayPal API call with its HTTP status), so a merchant - or a judge - can see
exactly what happened.
"""
from __future__ import annotations

import datetime as dt
import difflib
import time
import uuid
from decimal import Decimal

from .extract import finalize
from .money import fmt, to_decimal
from .paypal import MockPayPal, PayPalError, build_invoice_body, build_order_body, link

OPEN = {"AWAITING_PAYMENT", "APPROVED", "PARTIALLY_PAID", "DRAFT"}

ORDER_STATUS = {"CREATED": "AWAITING_PAYMENT", "SAVED": "AWAITING_PAYMENT",
                "PAYER_ACTION_REQUIRED": "AWAITING_PAYMENT", "APPROVED": "APPROVED",
                "COMPLETED": "PAID", "VOIDED": "CANCELLED"}
INVOICE_STATUS = {"DRAFT": "DRAFT", "SENT": "AWAITING_PAYMENT", "SCHEDULED": "AWAITING_PAYMENT",
                  "UNPAID": "AWAITING_PAYMENT", "PAYMENT_PENDING": "AWAITING_PAYMENT",
                  "PAID": "PAID", "MARKED_AS_PAID": "PAID", "PARTIALLY_PAID": "PARTIALLY_PAID",
                  "CANCELLED": "CANCELLED", "REFUNDED": "REFUNDED", "PARTIALLY_REFUNDED": "REFUNDED",
                  "MARKED_AS_REFUNDED": "REFUNDED"}


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


class Agent:
    def __init__(self, extractor, paypal, ledger, public_url: str = "http://localhost:8000",
                 brand_name: str | None = None, auto_capture: bool = True):
        self.extractor, self.paypal, self.ledger = extractor, paypal, ledger
        self.public_url = public_url.rstrip("/")
        self.brand_name = brand_name
        self.auto_capture = auto_capture

    # ------------------------------------------------------------------ helpers
    @property
    def mode(self):
        return self.paypal.mode

    def _calls_since(self, n):
        return [dict(c) for c in self.paypal.calls[n:]]

    def _event(self, rec, msg):
        rec.setdefault("events", []).append({"at": _now(), "msg": msg})
        rec["updated_at"] = _now()

    # ------------------------------------------------------------------ AI step
    def extract(self, text: str) -> dict:
        t0 = time.time()
        ex = finalize(self.extractor.extract(text))
        ex["ms"] = int((time.time() - t0) * 1000)
        return ex

    # ------------------------------------------------------------------ main flow
    def process(self, text: str, mode: str = "auto", extraction: dict | None = None) -> dict:
        trace = []
        if extraction is None:
            ex = self.extract(text)
            trace.append({"step": "AI extract", "engine": ex["engine"], "ms": ex.get("ms"),
                          "detail": f"{len(ex['items'])} item(s), customer={ex['customer'].get('name')!r}, "
                                    f"currency={ex['currency']}, confidence={ex['confidence']}"})
        else:  # human edited the AI draft in the UI
            ex = finalize(extraction)
            trace.append({"step": "AI extract", "engine": ex.get("engine"), "detail": "reviewed/edited by merchant"})
        trace.append({"step": "AI self-check", "detail": "; ".join(i["msg"] for i in ex["issues"]) or "no issues"})

        rid = "P2P-" + uuid.uuid4().hex[:10].upper()
        rec = {"id": rid, "created_at": _now(), "updated_at": _now(), "source_text": text[:4000],
               "customer": ex["customer"], "currency": ex["currency"], "items": ex["items"],
               "shipping": ex.get("shipping"), "tax_percent": ex.get("tax_percent"), "due_days": ex.get("due_days"),
               "note": ex.get("note"), "total": (ex.get("amounts") or {}).get("total"), "paid_amount": None,
               "kind": None, "status": None, "paypal_status": None, "paypal_id": None, "invoice_number": None,
               "pay_link": None, "mode": self.mode,
               "ai": {"engine": ex["engine"], "confidence": ex["confidence"], "issues": ex["issues"]},
               "trace": trace, "events": []}

        if not ex["ready"]:
            rec["status"] = "NEEDS_REVIEW"
            trace.append({"step": "decide", "detail": "Blocking issues - nothing sent to PayPal. Fix and resubmit."})
            self._event(rec, "Held for review")
            return self.ledger.save(rec)

        kind = mode if mode in ("invoice", "order") else ("invoice" if ex["customer"].get("email") else "order")
        if kind == "invoice" and not ex["customer"].get("email"):
            kind = "order"
            trace.append({"step": "decide", "detail": "Invoice requested but no email found -> checkout link instead"})
        else:
            why = ("customer email found -> PayPal emails a professional invoice and tracks it" if kind == "invoice"
                   else "no email (chat/DM buyer) -> PayPal checkout link to paste back into the chat")
            trace.append({"step": "decide", "detail": f"{kind.upper()}: {why}" if mode == "auto"
                          else f"{kind.upper()} (chosen by merchant)"})
        rec["kind"] = kind
        n = len(self.paypal.calls)
        try:
            if kind == "invoice":
                self._create_invoice(rec)
            else:
                self._create_order(rec)
        except PayPalError as e:
            rec["status"] = "FAILED"
            rec["error"] = str(e)
            trace.append({"step": "PayPal", "detail": f"failed: {e}", "calls": self._calls_since(n)})
            self._event(rec, f"PayPal error: {e}")
            return self.ledger.save(rec)
        trace.append({"step": "PayPal", "detail": f"{kind} {rec['paypal_id']} -> {rec['paypal_status']}",
                      "calls": self._calls_since(n)})
        return self.ledger.save(rec)

    def _create_invoice(self, rec):
        number = self.paypal.next_invoice_number()
        body = build_invoice_body(rec, number, business_name=self.brand_name)
        inv = self.paypal.create_invoice(body)
        rec.update(paypal_id=inv["id"], invoice_number=number)
        rec["total"] = (inv.get("amount") or {}).get("value") or rec["total"]
        sent = self.paypal.send_invoice(inv["id"])
        inv = self.paypal.get_invoice(inv["id"])
        rec["pay_link"] = (sent or {}).get("href") or inv.get("detail", {}).get("metadata", {}).get("recipient_view_url")
        self._apply_invoice(rec, inv)
        self._event(rec, f"Invoice {number} created and emailed to {rec['customer']['email']}")

    def _create_order(self, rec):
        body = build_order_body(rec, f"{self.public_url}/paypal/return", f"{self.public_url}/paypal/cancel",
                                brand_name=self.brand_name)
        order = self.paypal.create_order(body, request_id=rec["id"])
        rec["paypal_id"] = order["id"]
        rec["total"] = order["purchase_units"][0]["amount"]["value"] if order.get("purchase_units") else rec["total"]
        rec["pay_link"] = link(order, "payer-action", "approve")
        self._apply_order(rec, order)
        self._event(rec, "Checkout link created")

    # ------------------------------------------------------------------ status mapping
    def _apply_order(self, rec, order):
        rec["paypal_status"] = order.get("status")
        rec["status"] = ORDER_STATUS.get(order.get("status"), "AWAITING_PAYMENT")
        self._clear_flag(rec)
        caps = [c for pu in order.get("purchase_units", []) for c in pu.get("payments", {}).get("captures", [])
                if c.get("status") == "COMPLETED"]
        if caps:
            paid = sum((to_decimal(c["amount"]["value"]) for c in caps), Decimal("0"))
            rec["paid_amount"] = fmt(paid, rec["currency"])
            rec["capture_id"] = caps[0].get("id")
            if rec.get("total") and paid != to_decimal(rec["total"]):
                rec["status"] = "MISMATCH"

    def _apply_invoice(self, rec, inv):
        rec["paypal_status"] = inv.get("status")
        rec["status"] = INVOICE_STATUS.get(inv.get("status"), "AWAITING_PAYMENT")
        self._clear_flag(rec)
        paid = ((inv.get("payments") or {}).get("paid_amount") or {}).get("value")
        if paid is not None:
            rec["paid_amount"] = paid
        due = (inv.get("detail") or {}).get("payment_term", {}).get("due_date")
        if due:
            rec["due_date"] = due

    def _clear_flag(self, rec):
        if rec.get("status_flag") == "SUSPICIOUS_NOTICE" and rec["status"] == "PAID":
            rec["status_flag"] = None
            self._event(rec, "PayPal now confirms payment; suspicious-notice flag cleared")

    # ------------------------------------------------------------------ tracking
    def sync(self) -> dict:
        changes = []
        for rec in self.ledger.all():
            if rec["status"] not in OPEN or not rec.get("paypal_id"):
                continue
            before = rec["status"]
            n = len(self.paypal.calls)
            try:
                if rec["kind"] == "order":
                    order = self.paypal.get_order(rec["paypal_id"])
                    self._apply_order(rec, order)
                    if rec["status"] == "APPROVED" and self.auto_capture:
                        order = self.paypal.capture_order(rec["paypal_id"], request_id=f"cap-{rec['id']}")
                        self._apply_order(rec, order)
                        self._event(rec, f"Buyer approved; agent captured {rec['paid_amount']} {rec['currency']}")
                else:
                    self._apply_invoice(rec, self.paypal.get_invoice(rec["paypal_id"]))
            except PayPalError as e:
                self._event(rec, f"Sync error: {e}")
            if rec["status"] != before:
                self._event(rec, f"Status {before} -> {rec['status']} (PayPal: {rec['paypal_status']})")
                changes.append({"id": rec["id"], "from": before, "to": rec["status"],
                                "calls": self._calls_since(n)})
            self.ledger.save(rec)
        return {"changes": changes, "summary": self.summary()}

    def handle_return(self, order_id: str) -> dict | None:
        """Buyer came back from PayPal approval (return_url) -> capture right away."""
        rec = self.ledger.find(paypal_id=order_id)
        if not rec:
            return None
        try:
            order = self.paypal.get_order(order_id)
            self._apply_order(rec, order)
            if rec["status"] == "APPROVED":
                order = self.paypal.capture_order(order_id, request_id=f"cap-{rec['id']}")
                self._apply_order(rec, order)
                self._event(rec, f"Captured {rec['paid_amount']} {rec['currency']} on buyer return")
        except PayPalError as e:
            self._event(rec, f"Capture error: {e}")
        return self.ledger.save(rec)

    def simulate_payment(self, rid: str, amount: str | None = None) -> dict:
        if not isinstance(self.paypal, MockPayPal):
            raise RuntimeError("simulate is only available in mock mode; in sandbox pay with a sandbox buyer account")
        rec = self.ledger.get(rid)
        if rec["kind"] == "order":
            self.paypal.simulate_approval(rec["paypal_id"], amount)
        else:
            self.paypal.simulate_invoice_paid(rec["paypal_id"], amount)
        self._event(rec, "Buyer paid (simulated in mock mode)")
        self.ledger.save(rec)
        return rec

    # ------------------------------------------------------------------ reconciliation
    def match_payments(self, text: str) -> dict:
        payments = self.extractor.extract_payments(text)
        results = []
        for p in payments:
            best, score, why = self._best_match(p)
            res = {"payment": p, "record_id": None, "score": score, "reason": why, "action": None}
            if best and score >= 60:
                res["record_id"] = best["id"]
                res["action"] = self._apply_payment(best, p)
            else:
                res["action"] = "unmatched - needs a human" + (f" (closest: {best['id']}, {why})" if best else "")
            results.append(res)
        return {"results": results, "summary": self.summary()}

    def _best_match(self, p):
        best, best_score, best_why = None, 0, ""
        amt = to_decimal(p["amount"])
        for rec in self.ledger.all():
            if rec.get("status") in ("CANCELLED", "NEEDS_REVIEW", "FAILED") or rec.get("currency") != p["currency"]:
                continue
            score, why = 0, []
            ref = (p.get("reference") or "").upper()
            if ref and ref in {(rec.get("invoice_number") or "").upper(), rec["id"].upper(),
                               (rec.get("paypal_id") or "").upper()}:
                score += 100
                why.append(f"reference {ref}")
            total = to_decimal(rec.get("total")) or Decimal("0")
            outstanding = total - (to_decimal(rec.get("paid_amount")) or Decimal("0"))
            if rec["status"] == "PAID":
                outstanding = total
            if amt == outstanding:
                score += 50
                why.append("exact amount")
            elif amt and Decimal("0") < amt < outstanding:
                score += 15
                why.append("partial amount")
            sim = _name_similarity(p.get("payer"), rec["customer"].get("name"))
            if sim >= 0.5:
                score += int(30 * sim)
                why.append(f"payer name {sim:.0%} similar")
            if score > best_score:
                best, best_score, best_why = rec, score, ", ".join(why)
        return best, best_score, best_why

    def _apply_payment(self, rec, p) -> str:
        n = len(self.paypal.calls)
        if p["method"] == "PAYPAL":
            # Never trust a pasted "you received" message: ask PayPal what really happened.
            self.sync_one(rec)
            if rec["status"] == "PAID":
                msg = f"confirmed by PayPal API ({rec['paypal_status']})"
            else:
                msg = (f"WARNING: notification says paid, but PayPal API says {rec['paypal_status']} - "
                       f"possible fake payment screenshot/email; do not ship yet")
                rec["status_flag"] = "SUSPICIOUS_NOTICE"
            self._event(rec, f"Payment notice {p['amount']} {p['currency']}: {msg}")
        elif rec["status"] == "PAID":
            msg = "record already paid - possible DUPLICATE payment, review refund"
            self._event(rec, f"{p['method']} {p['amount']} {p['currency']}: {msg}")
        elif rec["kind"] == "invoice":
            self.paypal.record_invoice_payment(rec["paypal_id"], p["amount"], p["currency"], p["method"],
                                               note=f"Auto-reconciled by Paste2Pay: {p.get('raw') or ''}"[:150])
            self._apply_invoice(rec, self.paypal.get_invoice(rec["paypal_id"]))
            msg = f"recorded {p['method']} payment on PayPal invoice -> {rec['paypal_status']}"
            self._event(rec, msg)
        else:
            paid = (to_decimal(rec.get("paid_amount")) or Decimal("0")) + to_decimal(p["amount"])
            rec["paid_amount"] = fmt(paid, rec["currency"])
            rec["status"] = "PAID" if paid >= to_decimal(rec["total"]) else "PARTIALLY_PAID"
            msg = f"marked {rec['status']} (paid outside PayPal via {p['method']})"
            self._event(rec, msg)
        rec.setdefault("trace", []).append({"step": "reconcile", "detail": msg, "calls": self._calls_since(n)})
        self.ledger.save(rec)
        return msg

    def sync_one(self, rec):
        if rec["kind"] == "order":
            order = self.paypal.get_order(rec["paypal_id"])
            self._apply_order(rec, order)
            if rec["status"] == "APPROVED" and self.auto_capture:
                self._apply_order(rec, self.paypal.capture_order(rec["paypal_id"], request_id=f"cap-{rec['id']}"))
        else:
            self._apply_invoice(rec, self.paypal.get_invoice(rec["paypal_id"]))

    # ------------------------------------------------------------------ follow-up
    def reminder(self, rid: str) -> str:
        rec = self.ledger.get(rid)
        name = (rec["customer"].get("name") or "there").split(" ")[0]
        items = ", ".join(f"{i['quantity']} x {i['name']}" for i in rec["items"])
        return (f"Hi {name}! Friendly reminder about your order ({items}) - {rec['total']} {rec['currency']}. "
                f"You can pay securely with PayPal here: {rec['pay_link']} . Thank you!")

    def summary(self) -> dict:
        s = {"count": 0, "by_status": {}, "outstanding": {}, "collected": {}, "exceptions": 0}
        for r in self.ledger.all():
            s["count"] += 1
            s["by_status"][r["status"]] = s["by_status"].get(r["status"], 0) + 1
            ccy = r.get("currency") or "USD"
            total = to_decimal(r.get("total")) or Decimal("0")
            paid = to_decimal(r.get("paid_amount")) or Decimal("0")
            if r["status"] in OPEN:
                s["outstanding"][ccy] = fmt((to_decimal(s["outstanding"].get(ccy)) or 0) + total - paid, ccy)
            if paid:
                s["collected"][ccy] = fmt((to_decimal(s["collected"].get(ccy)) or 0) + paid, ccy)
            if r["status"] in ("MISMATCH", "NEEDS_REVIEW", "FAILED") or r.get("status_flag"):
                s["exceptions"] += 1
        return s


def _name_similarity(a: str | None, b: str | None) -> float:
    if not a or not b:
        return 0.0
    a, b = a.lower().replace(".", "").strip(), b.lower().replace(".", "").strip()
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    at, bt = a.split(), b.split()
    if len(at) >= 2 and len(bt) >= 2 and at[-1] == bt[-1] and at[0][0] == bt[0][0]:
        ratio = max(ratio, 0.9)  # "D. Okafor" vs "Daniel Okafor"
    return ratio
