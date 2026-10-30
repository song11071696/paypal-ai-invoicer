import pathlib
import unittest

from paste2pay.agent import Agent
from paste2pay.extract import RuleExtractor
from paste2pay.paypal import MockPayPal
from paste2pay.store import Ledger

SAMPLES = pathlib.Path(__file__).resolve().parent.parent / "samples"


def sample(name):
    return (SAMPLES / f"{name}.txt").read_text(encoding="utf-8")


class AgentFlowTests(unittest.TestCase):
    def setUp(self):
        self.pp = MockPayPal()
        self.agent = Agent(RuleExtractor(), self.pp, Ledger(None))

    def test_email_means_invoice_and_tracks_to_paid(self):
        rec = self.agent.process(sample("chat_order"))
        self.assertEqual(rec["kind"], "invoice")
        self.assertEqual(rec["status"], "AWAITING_PAYMENT")
        self.assertEqual(rec["total"], "74.50")
        paths = [c["path"] for s in rec["trace"] for c in s.get("calls", [])]
        self.assertIn("/v2/invoicing/invoices", paths)
        self.agent.simulate_payment(rec["id"])
        changes = self.agent.sync()["changes"]
        self.assertEqual(changes[0]["to"], "PAID")

    def test_no_email_means_checkout_link_then_auto_capture(self):
        rec = self.agent.process(sample("dm_no_email"))
        self.assertEqual(rec["kind"], "order")
        self.assertIn("checkoutnow?token=", rec["pay_link"])
        self.agent.sync()  # buyer has not paid: nothing changes
        self.assertEqual(self.agent.ledger.get(rec["id"])["status"], "AWAITING_PAYMENT")
        self.agent.simulate_payment(rec["id"])
        self.agent.sync()
        rec = self.agent.ledger.get(rec["id"])
        self.assertEqual((rec["status"], rec["paid_amount"]), ("PAID", "30.00"))

    def test_buyer_return_url_captures(self):
        rec = self.agent.process(sample("dm_no_email"))
        self.pp.simulate_approval(rec["paypal_id"])
        rec = self.agent.handle_return(rec["paypal_id"])
        self.assertEqual(rec["status"], "PAID")

    def test_underpayment_is_a_mismatch(self):
        rec = self.agent.process(sample("dm_no_email"))
        self.agent.simulate_payment(rec["id"], amount="25.00")
        self.agent.sync()
        self.assertEqual(self.agent.ledger.get(rec["id"])["status"], "MISMATCH")
        self.assertEqual(self.agent.summary()["exceptions"], 1)

    def test_blocking_issues_never_reach_paypal(self):
        rec = self.agent.process("hello, do you ship to Canada?")
        self.assertEqual(rec["status"], "NEEDS_REVIEW")
        self.assertEqual(self.pp.calls, [])

    def test_forced_invoice_without_email_falls_back_to_order(self):
        rec = self.agent.process(sample("dm_no_email"), mode="invoice")
        self.assertEqual(rec["kind"], "order")

    def test_reconcile_bank_transfer_fake_notice_and_unmatched(self):
        maya = self.agent.process(sample("chat_order"))
        dan = self.agent.process(sample("ocr_invoice"))
        res = self.agent.match_payments(sample("payments"))["results"]
        bank, paypal_notice, unknown = res
        self.assertEqual(bank["record_id"], dan["id"])
        self.assertEqual(self.agent.ledger.get(dan["id"])["paypal_status"], "MARKED_AS_PAID")
        self.assertEqual(paypal_notice["record_id"], maya["id"])
        self.assertIn("possible fake", paypal_notice["action"])
        self.assertEqual(self.agent.ledger.get(maya["id"])["status_flag"], "SUSPICIOUS_NOTICE")
        self.assertIsNone(unknown["record_id"])
        # once PayPal really has the money the flag clears
        self.agent.simulate_payment(maya["id"])
        self.agent.sync()
        self.assertIsNone(self.agent.ledger.get(maya["id"])["status_flag"])
        s = self.agent.summary()
        self.assertEqual(s["collected"], {"USD": "74.50", "EUR": "300.00"})
        self.assertEqual(s["exceptions"], 0)

    def test_duplicate_payment_detected(self):
        dan = self.agent.process(sample("ocr_invoice"))
        line = "Bank transfer from Daniel Okafor 300.00 EUR"
        self.agent.match_payments(line)
        again = self.agent.match_payments(line)["results"][0]
        self.assertEqual(again["record_id"], dan["id"])
        self.assertIn("DUPLICATE", again["action"])

    def test_reminder_contains_link(self):
        rec = self.agent.process(sample("dm_no_email"))
        self.assertIn(rec["pay_link"], self.agent.reminder(rec["id"]))

    def test_ledger_persists(self):
        import tempfile, os
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "l.json")
            a = Agent(RuleExtractor(), MockPayPal(), Ledger(path))
            rec = a.process(sample("chat_order"))
            self.assertEqual(Ledger(path).get(rec["id"])["total"], "74.50")


if __name__ == "__main__":
    unittest.main()
