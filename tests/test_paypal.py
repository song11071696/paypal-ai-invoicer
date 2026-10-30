import base64
import json
import unittest

from paste2pay.paypal import (MockPayPal, PayPalClient, PayPalError, SANDBOX_BASE, build_invoice_body,
                              build_order_body, get_paypal_client)

REC = {"id": "P2P-TEST", "currency": "USD", "customer": {"name": "Maya Chen", "email": "maya@example.com"},
       "items": [{"name": "Mug", "quantity": 2, "unit_price": "18.00"}, {"name": "Bowl", "quantity": 1, "unit_price": "32.00"}],
       "shipping": "6.50", "tax_percent": None, "due_days": 15, "note": "Thanks!"}


class FakeTransport:
    """Records requests and answers like api-m.sandbox.paypal.com."""

    def __init__(self, routes):
        self.routes, self.requests = routes, []

    def __call__(self, method, url, headers, data):
        path = url.replace(SANDBOX_BASE, "")
        self.requests.append({"method": method, "path": path, "headers": headers,
                              "body": json.loads(data) if data and data.startswith(b"{") else data})
        status, body = self.routes[(method, path)]
        return status, json.dumps(body)


TOKEN = (200, {"access_token": "A21TEST", "token_type": "Bearer", "expires_in": 32400})


class BodyBuilderTests(unittest.TestCase):
    def test_order_body_totals_add_up(self):
        b = build_order_body(REC, "http://localhost/r", "http://localhost/c")
        pu = b["purchase_units"][0]
        self.assertEqual(b["intent"], "CAPTURE")
        self.assertEqual(pu["amount"], {"currency_code": "USD", "value": "74.50", "breakdown": {
            "item_total": {"currency_code": "USD", "value": "68.00"},
            "shipping": {"currency_code": "USD", "value": "6.50"}}})
        self.assertEqual(pu["items"][0]["quantity"], "2")
        self.assertEqual(b["payment_source"]["paypal"]["experience_context"]["return_url"], "http://localhost/r")

    def test_order_body_with_tax_is_consistent(self):
        rec = dict(REC, tax_percent="8.25", shipping=None)
        pu = build_order_body(rec, "r", "c")["purchase_units"][0]
        item_tax = sum(float(i["tax"]["value"]) * int(i["quantity"]) for i in pu["items"])
        self.assertAlmostEqual(item_tax, float(pu["amount"]["breakdown"]["tax_total"]["value"]))
        bd = pu["amount"]["breakdown"]
        self.assertAlmostEqual(float(bd["item_total"]["value"]) + float(bd["tax_total"]["value"]),
                               float(pu["amount"]["value"]))

    def test_invoice_body(self):
        b = build_invoice_body(REC, "INV-0007")
        self.assertEqual(b["detail"]["invoice_number"], "INV-0007")
        self.assertEqual(b["detail"]["payment_term"], {"term_type": "NET_15"})
        self.assertEqual(b["primary_recipients"][0]["billing_info"],
                         {"email_address": "maya@example.com", "name": {"given_name": "Maya", "surname": "Chen"}})
        self.assertEqual(b["amount"]["breakdown"]["shipping"]["amount"]["value"], "6.50")
        odd = build_invoice_body(dict(REC, due_days=7), "X")
        self.assertEqual(odd["detail"]["payment_term"]["term_type"], "DUE_ON_DATE_SPECIFIED")


class RealClientTests(unittest.TestCase):
    def test_oauth_and_orders_flow(self):
        t = FakeTransport({
            ("POST", "/v1/oauth2/token"): TOKEN,
            ("POST", "/v2/checkout/orders"): (200, {"id": "5O190127TN364715T", "status": "PAYER_ACTION_REQUIRED",
                                                  "links": [{"rel": "payer-action", "href": "https://www.sandbox.paypal.com/checkoutnow?token=5O190127TN364715T"}]}),
            ("GET", "/v2/checkout/orders/5O190127TN364715T"): (200, {"id": "5O190127TN364715T", "status": "APPROVED"}),
            ("POST", "/v2/checkout/orders/5O190127TN364715T/capture"): (201, {"id": "5O190127TN364715T", "status": "COMPLETED"}),
        })
        c = PayPalClient("cid", "secret", transport=t)
        body = build_order_body(REC, "r", "c")
        o = c.create_order(body, request_id="P2P-TEST")
        c.get_order(o["id"])
        c.capture_order(o["id"])
        auth = t.requests[0]
        self.assertEqual(auth["path"], "/v1/oauth2/token")
        self.assertEqual(auth["headers"]["Authorization"], "Basic " + base64.b64encode(b"cid:secret").decode())
        self.assertEqual(auth["body"], b"grant_type=client_credentials")
        self.assertEqual(sum(r["path"] == "/v1/oauth2/token" for r in t.requests), 1)  # token cached
        create = t.requests[1]
        self.assertEqual(create["headers"]["Authorization"], "Bearer A21TEST")
        self.assertEqual(create["headers"]["PayPal-Request-Id"], "P2P-TEST")
        self.assertEqual(create["body"], body)
        self.assertEqual([c_["status"] for c_ in c.calls], [200, 200, 200, 201])

    def test_invoicing_flow(self):
        t = FakeTransport({
            ("POST", "/v1/oauth2/token"): TOKEN,
            ("POST", "/v2/invoicing/generate-next-invoice-number"): (200, {"invoice_number": "0042"}),
            ("POST", "/v2/invoicing/invoices"): (201, {"rel": "self", "method": "GET",
                                                      "href": f"{SANDBOX_BASE}/v2/invoicing/invoices/INV2-AAAA"}),
            ("GET", "/v2/invoicing/invoices/INV2-AAAA"): (200, {"id": "INV2-AAAA", "status": "DRAFT"}),
            ("POST", "/v2/invoicing/invoices/INV2-AAAA/send"): (200, {"rel": "payer-view", "href": "https://pay/x"}),
            ("POST", "/v2/invoicing/invoices/INV2-AAAA/payments"): (200, {"payment_id": "EXTR-1"}),
        })
        c = PayPalClient("cid", "secret", transport=t)
        self.assertEqual(c.next_invoice_number(), "0042")
        inv = c.create_invoice(build_invoice_body(REC, "0042"))
        self.assertEqual(inv["id"], "INV2-AAAA")  # resolved from href
        self.assertEqual(c.send_invoice("INV2-AAAA")["href"], "https://pay/x")
        c.record_invoice_payment("INV2-AAAA", "74.5", "USD", "BANK_TRANSFER")
        pay = t.requests[-1]["body"]
        self.assertEqual(pay["amount"], {"currency_code": "USD", "value": "74.50"})
        self.assertEqual(pay["method"], "BANK_TRANSFER")

    def test_errors_surface_debug_id(self):
        t = FakeTransport({("POST", "/v1/oauth2/token"): TOKEN,
                           ("POST", "/v2/checkout/orders"): (422, {"name": "UNPROCESSABLE_ENTITY", "message": "bad",
                                                                 "debug_id": "dbg123", "details": [{"issue": "AMOUNT_MISMATCH"}]})})
        with self.assertRaises(PayPalError) as cm:
            PayPalClient("a", "b", transport=t).create_order({}, "x")
        self.assertEqual(cm.exception.debug_id, "dbg123")
        self.assertIn("AMOUNT_MISMATCH", str(cm.exception))

    def test_bad_credentials(self):
        t = FakeTransport({("POST", "/v1/oauth2/token"): (401, {"error": "invalid_client",
                                                              "error_description": "Client Authentication failed"})})
        with self.assertRaises(PayPalError):
            PayPalClient("a", "b", transport=t).token()

    def test_mode_selection(self):
        self.assertIsInstance(get_paypal_client({}), MockPayPal)
        self.assertIsInstance(get_paypal_client({"PAYPAL_CLIENT_ID": "x", "PAYPAL_CLIENT_SECRET": "y"}), PayPalClient)
        self.assertIsInstance(get_paypal_client({"PAYPAL_CLIENT_ID": "x", "PAYPAL_CLIENT_SECRET": "y",
                                                 "PAYPAL_MODE": "mock"}), MockPayPal)


class MockTests(unittest.TestCase):
    def test_capture_requires_approval(self):
        m = MockPayPal()
        o = m.create_order(build_order_body(REC, "r", "c"), "rid")
        with self.assertRaises(PayPalError):
            m.capture_order(o["id"])
        m.simulate_approval(o["id"])
        self.assertEqual(m.capture_order(o["id"])["status"], "COMPLETED")

    def test_idempotent_create(self):
        m = MockPayPal()
        a = m.create_order(build_order_body(REC, "r", "c"), "same")
        b = m.create_order(build_order_body(REC, "r", "c"), "same")
        self.assertEqual(a["id"], b["id"])


if __name__ == "__main__":
    unittest.main()
