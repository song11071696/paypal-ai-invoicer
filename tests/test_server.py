import json
import unittest
import urllib.request

# talk to localhost directly even if HTTP(S)_PROXY is set
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

from paste2pay.agent import Agent
from paste2pay.extract import RuleExtractor
from paste2pay.paypal import MockPayPal
from paste2pay.server import serve
from paste2pay.store import Ledger


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.agent = Agent(RuleExtractor(), MockPayPal(), Ledger(None))
        cls.httpd = serve(port=0, agent=cls.agent, block=False)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def call(self, path, body=None):
        req = urllib.request.Request(self.base + path, method="POST" if body is not None else "GET",
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
        with OPENER.open(req) as r:
            return json.loads(r.read())

    def test_end_to_end_over_http(self):
        self.assertEqual(self.call("/api/status")["paypal_mode"], "mock")
        samples = self.call("/api/samples")
        ex = self.call("/api/extract", {"text": samples["dm_no_email"]})
        self.assertTrue(ex["ready"])
        ex["items"][0]["quantity"] = 4  # merchant edits the AI draft
        rec = self.call("/api/process", {"text": samples["dm_no_email"], "extraction": ex})
        self.assertEqual(rec["total"], "38.00")
        self.call(f"/api/simulate/{rec['id']}", {})
        self.assertEqual(self.call("/api/sync", {})["changes"][0]["to"], "PAID")
        recs = self.call("/api/records")
        self.assertEqual(recs["summary"]["collected"]["GBP"], "38.00")

    def test_index_served(self):
        with OPENER.open(self.base + "/") as r:
            self.assertIn(b"Paste2Pay", r.read())


if __name__ == "__main__":
    unittest.main()
