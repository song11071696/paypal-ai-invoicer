import pathlib
import unittest

from paste2pay.extract import LLMExtractor, RuleExtractor, finalize, get_extractor

SAMPLES = pathlib.Path(__file__).resolve().parent.parent / "samples"


def run(text):
    return finalize(RuleExtractor().extract(text))


class RuleExtractionTests(unittest.TestCase):
    def test_chat_order_with_email(self):
        ex = run((SAMPLES / "chat_order.txt").read_text())
        self.assertEqual(ex["customer"], {"name": "Maya Chen", "email": "maya.chen@example.com"})
        self.assertEqual(ex["currency"], "USD")
        self.assertEqual([(i["name"], i["quantity"], i["unit_price"]) for i in ex["items"]],
                         [("Speckled Mug", 2, "18.00"), ("Matcha Bowl", 1, "32.00")])
        self.assertEqual(ex["amounts"]["total"], "74.50")
        self.assertTrue(ex["ready"])

    def test_ocr_invoice_line_totals_resolved_by_stated_total(self):
        ex = run((SAMPLES / "ocr_invoice.txt").read_text())
        self.assertEqual(ex["currency"], "EUR")
        self.assertEqual(ex["items"][0]["unit_price"], "45.00")  # 180 / 4
        self.assertEqual(ex["amounts"]["total"], "300.00")
        self.assertEqual(ex["due_days"], 15)
        self.assertTrue(any("line total" in i["msg"] for i in ex["issues"]))

    def test_dm_without_email_and_word_quantities(self):
        ex = run((SAMPLES / "dm_no_email.txt").read_text())
        self.assertIsNone(ex["customer"]["email"])
        self.assertEqual(ex["customer"]["name"], "Leo")
        self.assertEqual(ex["currency"], "GBP")
        self.assertIn(("tea lights", 12, "0.50"), [(i["name"], i["quantity"], i["unit_price"]) for i in ex["items"]])
        self.assertEqual(ex["amounts"]["shipping"], "0.00")

    def test_chinese_order(self):
        ex = run((SAMPLES / "chinese_order.txt").read_text())
        self.assertEqual(ex["customer"]["name"], "王芳")
        self.assertEqual(ex["currency"], "HKD")
        self.assertEqual(ex["amounts"]["total"], "215.00")

    def test_explicit_for_means_line_total(self):
        ex = run("Customer: Ann Lee\n2 tote bags for $30")
        self.assertEqual(ex["items"][0]["unit_price"], "15.00")

    def test_tax_percent(self):
        ex = run("Bill to: Bo\n2 x Widget @ $10 each\nSales tax 10%")
        self.assertEqual(ex["amounts"]["tax"], "2.00")
        self.assertEqual(ex["amounts"]["total"], "22.00")

    def test_stated_total_mismatch_is_flagged(self):
        ex = run("Customer: Bo\n2 x Widget @ $10 each\nTotal: $25")
        self.assertTrue(any("differs" in i["msg"] for i in ex["issues"]))
        self.assertTrue(ex["ready"])  # warning, not blocking

    def test_nothing_billable_blocks(self):
        ex = run("hi, are you open on sunday?")
        self.assertFalse(ex["ready"])
        self.assertTrue(any(i["level"] == "error" for i in ex["issues"]))

    def test_no_currency_defaults_with_warning(self):
        ex = run("Customer: Bo\n3 x Sticker @ 2 each")
        self.assertEqual(ex["currency"], "USD")
        self.assertTrue(any("assumed USD" in i["msg"] for i in ex["issues"]))

    def test_zero_decimal_currency(self):
        ex = run("Customer: Ken\n2 x Tenugui @ ¥1200 each 円")
        self.assertEqual(ex["currency"], "JPY")
        self.assertEqual(ex["items"][0]["unit_price"], "1200")

    def test_payment_notices(self):
        ps = RuleExtractor().extract_payments((SAMPLES / "payments.txt").read_text())
        self.assertEqual(len(ps), 3)
        self.assertEqual(ps[0]["reference"], "INV-0002")
        self.assertEqual(ps[0]["method"], "BANK_TRANSFER")
        self.assertEqual((ps[1]["amount"], ps[1]["currency"], ps[1]["method"]), ("74.50", "USD", "PAYPAL"))


class LLMExtractorTests(unittest.TestCase):
    def test_openai_compatible_call_and_normalisation(self):
        seen = {}

        def transport(url, headers, body):
            seen.update(url=url, headers=headers, body=body)
            return {"choices": [{"message": {"content": '```json\n{"customer":{"name":"Ana","email":"ana@x.io"},'
                                                        '"currency":"eur","items":[{"name":"Lamp","quantity":"2",'
                                                        '"unit_price":"19.9"}],"shipping":"5","stated_total":"44.80"}\n```'}}]}

        llm = LLMExtractor("https://llm.example/v1", "test-model", "sk-test", transport=transport)
        ex = finalize(llm.extract("anything"))
        self.assertEqual(seen["url"], "https://llm.example/v1/chat/completions")
        self.assertEqual(seen["headers"]["Authorization"], "Bearer sk-test")
        self.assertEqual(seen["body"]["model"], "test-model")
        self.assertEqual(ex["engine"], "llm:test-model")
        self.assertEqual(ex["currency"], "EUR")
        self.assertEqual(ex["amounts"]["total"], "44.80")
        self.assertTrue(ex["ready"])

    def test_falls_back_to_rules_when_llm_fails(self):
        def transport(url, headers, body):
            raise ConnectionError("no network")

        llm = LLMExtractor("https://llm.example/v1", "m", transport=transport)
        ex = finalize(llm.extract((SAMPLES / "chat_order.txt").read_text()))
        self.assertIn("fallback", ex["engine"])
        self.assertEqual(ex["amounts"]["total"], "74.50")

    def test_provider_selection(self):
        self.assertEqual(get_extractor({}).name, "rules")
        self.assertEqual(get_extractor({"LLM_PROVIDER": "ollama", "LLM_MODEL": "llama3.1"}).base_url,
                         "http://localhost:11434/v1")


if __name__ == "__main__":
    unittest.main()
