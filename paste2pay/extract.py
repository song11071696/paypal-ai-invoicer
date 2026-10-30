"""AI extraction layer.

Turns messy text (a chat with a buyer, an emailed order, OCR text of an invoice
photo, a payment notification) into structured data the PayPal layer can use.

Two interchangeable engines share one output schema:

* ``RuleExtractor``  - deterministic regex/heuristic parser. Default, offline, free.
* ``LLMExtractor``   - any OpenAI-compatible chat-completions endpoint
                       (OpenAI, OpenRouter, Groq, a local Ollama ...). Falls back
                       to the rule engine if the model answer is unusable.

Whatever the engine, ``finalize()`` then runs the same self-check: it resolves
"is that a unit price or a line total?" against the stated total, computes the
amounts PayPal will charge, and raises blocking errors / warnings for a human.
"""
from __future__ import annotations

import itertools
import json
import os
import re
import urllib.error
import urllib.request
from decimal import Decimal
from typing import Callable

from .money import RESTRICTED, SUPPORTED_CURRENCIES, ZERO_DECIMAL, fmt, quantize, to_decimal

# --------------------------------------------------------------------------- regex
_CODES = sorted(SUPPORTED_CURRENCIES | {"RMB"})
_SYM = r"(?:US\$|CA\$|C\$|AU\$|A\$|HK\$|S\$|NT\$|NZ\$|R\$|\$|€|£|¥|￥)"
_AMT = r"\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?"
_CODE = r"(?:\b(?:" + "|".join(_CODES) + r")\b|元|円|块|\bdollars?\b|\beuros?\b|\bbucks\b)"
PRICE = rf"(?P<sym>{_SYM})?\s*(?P<price>{_AMT})\s*(?P<code>{_CODE})?"
_EACH = r"(?P<each>\beach\b|\bea\b\.?|\bapiece\b|\ba piece\b|\bper (?:item|unit|piece|one|pc|bag|box)\b|/\s*(?:ea|each|pc|unit|item)\b)"
_NUMWORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
             "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "a dozen": 12}
_QTY = r"(?P<qty>\d{1,4}|a dozen|an|a|one|two|three|four|five|six|seven|eight|nine|ten)"
_NAME = r"(?P<name>[A-Za-z][A-Za-z0-9 '&/\-\.]*?)"
_SEP = r"\s*(?:,|-|–|—|:|=)?\s*"
_KW = r"(?P<kw>@|\bat\b|\bfor\b|\beach at\b|\bpriced at\b|\bcosts?\b)?"

RE_ITEM_QTY_FIRST = re.compile(
    rf"(?<![\w.]){_QTY}\s*(?:x\b|×|\*|pcs?\.?|pieces?|units?|of(?: the)?)?\s+{_NAME}{_SEP}{_KW}\s*{PRICE}\s*{_EACH}?",
    re.I)
RE_ITEM_NAME_FIRST = re.compile(
    rf"{_NAME}\s*(?:x|×|\*)\s*(?P<qty>\d{{1,4}})\b{_SEP}{_KW}\s*{PRICE}\s*{_EACH}?", re.I)
RE_ITEM_ZH = re.compile(
    rf"(?P<name>[\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z0-9]*?)\s*[x×*]?\s*(?P<qty>\d{{1,4}})\s*"
    rf"(?:个|件|份|只|杯|盒|瓶|套|本|条|双)\s*[,，]?\s*(?P<kw>单价|每个|每件|每份|各)?\s*{PRICE}")

RE_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
RE_NAME_LABEL = re.compile(
    r"^\s*(?:customer|client|bill(?:ed)?\s+to|buyer|name|recipient|客户|姓名)\s*[:：]\s*(?P<v>.+)$", re.I | re.M)
RE_NAME_INTRO = re.compile(
    r"\b(?:my name is|this is|i am|i'm)\s+(?P<v>[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)")
RE_SPEAKER = re.compile(
    r"^\s*(?:\[[^\]]{1,40}\]\s*)?(?:\d{1,2}:\d{2}\s*(?:[AP]M)?\s*[-–]?\s*)?"
    r"(?P<sp>[A-Z][A-Za-z.'\- ]{0,30}?)\s*:\s+(?P<msg>\S.*)$")
RE_TOTAL = re.compile(
    rf"(?:(?<![a-z])(?:grand\s+)?total(?:\s+due)?|amount due|合计|总计|共计|总共)[^\d\n$€£¥￥]{{0,20}}?{PRICE}", re.I)
RE_SHIPPING = re.compile(rf"(?P<lbl>shipping|delivery|postage|运费|邮费)(?P<gap>[^\d\n$€£¥￥]{{0,20}}?){PRICE}", re.I)
RE_FREE_SHIP = re.compile(r"free (?:shipping|delivery)|包邮|免运费", re.I)
RE_TAX = re.compile(r"(?:tax|vat|gst|税)[^\d\n]{0,12}?(?P<pct>\d+(?:\.\d+)?)\s*%", re.I)
RE_DUE = re.compile(r"(?:due\s+(?:in\s+)?|within\s+|net\s*-?\s*)(?P<d>\d{1,3})(?:\s*days?)?", re.I)
RE_DUE_NOW = re.compile(r"due (?:on|upon) receipt|payable on receipt|pay (?:right )?now|asap", re.I)
RE_NOTE = re.compile(r"^\s*(?:note|notes|memo|备注)\s*[:：]\s*(?P<v>.+)$", re.I | re.M)
RE_SKIP_ITEM_LINE = re.compile(
    r"total|subtotal|shipping|delivery fee|postage|\btax\b|\bvat\b|合计|总计|共计|运费|邮费|"
    r"received|transfer|paid|refund", re.I)
RE_SPLIT = re.compile(r"\s*(?:;|,\s*(?=\d)|\band\b(?=\s+(?:\d|an?\b|one|two|three|four|five)))\s*", re.I)
_FILLER = re.compile(
    r"^(?:hi|hello|hey|ok|okay|yes|so|also|plus|and|then|please|pls|can i (?:get|have|order)|could i (?:get|have)|"
    r"i'?d like(?: to (?:order|buy|get))?|i would like(?: to (?:order|buy|get))?|i want(?: to (?:order|buy))?|"
    r"i'?ll take|i will take|i need|send me|get me|add|order|buy|the|of)\b[\s,!.]*", re.I)

_LABEL_WORDS = {
    "customer", "client", "bill to", "billed to", "name", "email", "e-mail", "total", "subtotal",
    "shipping", "tax", "note", "notes", "due", "date", "invoice", "order", "item", "items", "qty",
    "price", "address", "phone", "ref", "reference", "currency", "payment", "terms", "buyer", "memo",
    "recipient", "amount", "amount due", "grand total", "delivery", "from", "to", "subject",
}
SELLER_WORDS = {"me", "seller", "shop", "store", "you", "owner", "admin", "support", "us", "we"}
_BUY_CUES = re.compile(r"i'?d like|i want|can i|could i|i'?ll take|order|buy|send me|how much", re.I)

_SYMBOL_CCY = {"US$": "USD", "CA$": "CAD", "C$": "CAD", "AU$": "AUD", "A$": "AUD", "HK$": "HKD",
               "S$": "SGD", "NT$": "TWD", "NZ$": "NZD", "R$": "BRL", "$": "USD", "€": "EUR", "£": "GBP"}


def _code_to_ccy(code: str | None, text: str = "") -> str | None:
    if not code:
        return None
    c = code.strip()
    up = c.upper()
    if up in SUPPORTED_CURRENCIES:
        return up
    if up == "RMB" or c in ("元", "块"):
        return "CNY"
    if c == "円":
        return "JPY"
    low = c.lower()
    if low.startswith("dollar") or low == "bucks":
        return "USD"
    if low.startswith("euro"):
        return "EUR"
    return None


def _sym_to_ccy(sym: str | None, text: str) -> str | None:
    if not sym:
        return None
    if sym in ("¥", "￥"):
        if "円" in text or re.search(r"[\u3040-\u30ff]", text):
            return "JPY"
        return "CNY"
    return _SYMBOL_CCY.get(sym.upper(), _SYMBOL_CCY.get(sym))


def _clean_name(name: str) -> str:
    name = name.strip(" -,.:;")
    prev = None
    while prev != name:
        prev = name
        name = _FILLER.sub("", name).strip(" -,.:;")
    return name


def _qty(q: str) -> int:
    q = q.strip().lower()
    return int(q) if q.isdigit() else _NUMWORDS.get(q, 1)


def empty_extraction(engine: str) -> dict:
    return {"customer": {"name": None, "email": None}, "currency": None, "currency_source": None,
            "items": [], "shipping": None, "tax_percent": None, "stated_total": None,
            "due_days": None, "note": None, "engine": engine, "warnings": []}


# --------------------------------------------------------------------------- rules engine
class RuleExtractor:
    name = "rules"

    def extract(self, text: str) -> dict:
        ex = empty_extraction("rules")
        text = text.replace("\r\n", "\n")
        ex["currency"], ex["currency_source"] = self._currency(text)
        ex["customer"] = self._customer(text)
        ex["items"] = self._items(text)
        ex["shipping"] = self._shipping(text)
        m = RE_TAX.search(text)
        if m:
            ex["tax_percent"] = m.group("pct")
        totals = [m for m in RE_TOTAL.finditer(text) if not re.search(r"sub\s*$", text[max(0, m.start() - 4):m.start()], re.I)]
        if totals:
            ex["stated_total"] = str(to_decimal(totals[-1].group("price")))
        if RE_DUE_NOW.search(text):
            ex["due_days"] = 0
        else:
            m = RE_DUE.search(text)
            if m:
                ex["due_days"] = int(m.group("d"))
        m = RE_NOTE.search(text)
        if m:
            ex["note"] = m.group("v").strip()[:200]
        return ex

    # -- pieces
    def _currency(self, text: str):
        votes: dict[str, int] = {}
        for m in re.finditer(rf"{_SYM}|{_CODE}", text, re.I):
            tok = m.group(0)
            ccy = _sym_to_ccy(tok, text) if re.fullmatch(_SYM, tok) else _code_to_ccy(tok, text)
            if ccy:
                votes[ccy] = votes.get(ccy, 0) + (2 if len(tok) == 3 and tok.isalpha() else 1)
        if not votes:
            return "USD", "default"
        return max(votes.items(), key=lambda kv: kv[1])[0], "detected"

    def _customer(self, text: str) -> dict:
        name = email = None
        m = RE_NAME_LABEL.search(text)
        if m:
            v = RE_EMAIL.sub("", m.group("v")).strip(" <>()[],")
            name = v or None
        emails = RE_EMAIL.findall(text)
        for line in text.splitlines():
            if re.match(r"^\s*(?:e-?mail|customer|client|bill)", line, re.I):
                e = RE_EMAIL.search(line)
                if e:
                    email = e.group(0)
                    break
        if not email and emails:
            email = emails[0]
        if not name:
            m = RE_NAME_INTRO.search(text)
            if m:
                name = m.group("v")
        if not name:
            name = self._chat_customer(text)
        return {"name": name, "email": email}

    def _chat_customer(self, text: str) -> str | None:
        scores: dict[str, list] = {}
        order = []
        for line in text.splitlines():
            m = RE_SPEAKER.match(line)
            if not m:
                continue
            sp = m.group("sp").strip()
            if sp.lower() in _LABEL_WORDS or sp.lower() in SELLER_WORDS:
                continue
            if sp not in scores:
                scores[sp] = [0, len(order)]
                order.append(sp)
            scores[sp][0] += len(_BUY_CUES.findall(m.group("msg")))
        if not scores:
            return None
        return sorted(scores.items(), key=lambda kv: (-kv[1][0], kv[1][1]))[0][0]

    def _items(self, text: str) -> list:
        items = []
        for raw in text.splitlines():
            line = raw
            sm = RE_SPEAKER.match(line)
            if sm and sm.group("sp").strip().lower() not in _LABEL_WORDS:
                line = sm.group("msg")
            for seg in RE_SPLIT.split(line):
                if RE_SKIP_ITEM_LINE.search(seg):
                    continue
                it = self._item_from_segment(seg, text)
                if it:
                    items.append(it)
        return items

    def _item_from_segment(self, seg: str, text: str) -> dict | None:
        for rx in (RE_ITEM_ZH, RE_ITEM_QTY_FIRST, RE_ITEM_NAME_FIRST):
            m = rx.search(seg)
            if not m:
                continue
            gd = m.groupdict()
            qty_word = not gd["qty"].isdigit()
            if not gd.get("sym") and not gd.get("code") and (qty_word or not gd.get("kw") and not gd.get("each")):
                # "a discount at 10" / "2 Blue Mugs 12" without any money marker: too weak unless it's a
                # bare invoice-style line "2 x Mug 12.50"
                if qty_word or not re.search(r"\d\s*(?:x|×|\*)\s", seg, re.I) and rx is not RE_ITEM_NAME_FIRST:
                    continue
            name = _clean_name(gd["name"]) if rx is not RE_ITEM_ZH else gd["name"]
            if not name or len(name) < 2:
                continue
            kw = (gd.get("kw") or "").lower()
            if gd.get("each") or kw in ("@", "at", "each at", "priced at", "单价", "每个", "每件", "每份", "各"):
                basis = "unit"
            elif kw == "for":
                basis = "line"
            else:
                basis = "ambiguous"
            qty = _qty(gd["qty"])
            if qty == 1:
                basis = "unit"
            return {"name": name[:120], "quantity": qty, "unit_price": str(to_decimal(gd["price"])),
                    "price_basis": basis}
        return None

    def _shipping(self, text: str) -> str | None:
        if RE_FREE_SHIP.search(text):
            return "0"
        for m in RE_SHIPPING.finditer(text):
            if m.group("sym") or m.group("code") or ":" in m.group("gap") or "：" in m.group("gap"):
                return str(to_decimal(m.group("price")))
        return None

    # -- payment notifications (reconciliation input)
    def extract_payments(self, text: str) -> list:
        out = []
        for line in text.replace("\r\n", "\n").splitlines():
            line = line.strip()
            if not line:
                continue
            m = re.search(PRICE, line)
            found = None
            for m in re.finditer(PRICE, line, re.I):
                if m.group("sym") or m.group("code"):
                    found = m
                    break
            if not found:
                continue
            ccy = _sym_to_ccy(found.group("sym"), text) or _code_to_ccy(found.group("code"), text)
            payer = None
            pm = re.search(r"\bfrom\s+(?P<p>[A-Z][A-Za-z.'\-]*(?:\s+[A-Z][A-Za-z.'\-]*){0,3})", line)
            if pm:
                payer = pm.group("p").strip()
            ref = None
            rm = re.search(r"\b(?:ref(?:erence)?|invoice|inv|order)\s*(?:#|no\.?|number)?\s*[:#]?\s*"
                           r"(?P<r>[A-Z0-9][A-Z0-9\-]{2,})", line, re.I)
            if rm and any(ch.isdigit() for ch in rm.group("r")):
                ref = rm.group("r")
            low = line.lower()
            method = ("BANK_TRANSFER" if re.search(r"bank|wire|transfer|deposit", low) else
                      "CASH" if "cash" in low else "CHECK" if re.search(r"check|cheque", low) else
                      "PAYPAL" if "paypal" in low else "OTHER")
            out.append({"payer": payer, "amount": str(to_decimal(found.group("price"))),
                        "currency": ccy or "USD", "reference": ref, "method": method, "raw": line[:200]})
        return out


# --------------------------------------------------------------------------- LLM engine
LLM_SYSTEM = """You extract sales orders for a small merchant from messy text (chat logs, emails,
OCR of invoices, any language). Reply with ONLY a JSON object with these keys:
{"customer":{"name":string|null,"email":string|null},
 "currency": ISO-4217 code or null,
 "items":[{"name":string,"quantity":integer,"unit_price":string decimal|null}],
 "shipping": string decimal|null, "tax_percent": string decimal|null,
 "stated_total": string decimal|null  (the total written in the text, if any),
 "due_days": integer|null, "note": string|null}
Rules: unit_price is the price of ONE unit (divide line totals by quantity). The seller is
"me"/the shop; the customer is the buyer. Never invent values that are not in the text."""

LLM_PAYMENTS = """Extract incoming payments from payment notifications or bank statement lines.
Reply ONLY with JSON: {"payments":[{"payer":string|null,"amount":string decimal,"currency":ISO code,
"reference":string|null,"method":"PAYPAL"|"BANK_TRANSFER"|"CASH"|"CHECK"|"OTHER"}]}"""


class LLMExtractor:
    """OpenAI-compatible chat completions. ``transport`` is injectable for tests."""

    def __init__(self, base_url: str, model: str, api_key: str | None = None,
                 transport: Callable | None = None, timeout: float = 45):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.transport = transport or self._http
        self.fallback = RuleExtractor()
        self.name = f"llm:{model}"

    def _http(self, url: str, headers: dict, body: dict) -> dict:
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read().decode())

    def _chat(self, system: str, user: str) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        body = {"model": self.model, "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        resp = self.transport(f"{self.base_url}/chat/completions", headers, body)
        content = resp["choices"][0]["message"]["content"]
        m = re.search(r"\{.*\}", content, re.S)
        return json.loads(m.group(0) if m else content)

    def extract(self, text: str) -> dict:
        try:
            data = self._chat(LLM_SYSTEM, text)
            return normalize_llm(data, self.name)
        except Exception as e:  # network, quota, bad JSON -> never break the demo
            ex = self.fallback.extract(text)
            ex["engine"] = f"rules (fallback: {self.name} failed: {type(e).__name__})"
            ex["warnings"].append(f"LLM call failed ({type(e).__name__}: {str(e)[:120]}); used rule engine")
            return ex

    def extract_payments(self, text: str) -> list:
        try:
            data = self._chat(LLM_PAYMENTS, text)
            out = []
            for p in data.get("payments", []):
                amt = to_decimal(p.get("amount"))
                if amt is None:
                    continue
                out.append({"payer": p.get("payer"), "amount": str(amt),
                            "currency": (p.get("currency") or "USD").upper(),
                            "reference": p.get("reference"), "method": p.get("method") or "OTHER",
                            "raw": None})
            return out
        except Exception:
            return self.fallback.extract_payments(text)


def normalize_llm(data: dict, engine: str) -> dict:
    ex = empty_extraction(engine)
    cust = data.get("customer") or {}
    ex["customer"] = {"name": cust.get("name") or None, "email": cust.get("email") or None}
    ccy = (data.get("currency") or "").upper() or None
    ex["currency"], ex["currency_source"] = (ccy, "detected") if ccy else ("USD", "default")
    for it in data.get("items") or []:
        try:
            qty = int(it.get("quantity") or 1)
        except (TypeError, ValueError):
            qty = 1
        price = to_decimal(it.get("unit_price"))
        ex["items"].append({"name": str(it.get("name") or "Item")[:120], "quantity": max(qty, 1),
                            "unit_price": str(price) if price is not None else None,
                            "price_basis": "unit" if price is not None else "unknown"})
    for k in ("shipping", "tax_percent", "stated_total"):
        d = to_decimal(data.get(k))
        ex[k] = str(d) if d is not None else None
    try:
        ex["due_days"] = int(data["due_days"]) if data.get("due_days") is not None else None
    except (TypeError, ValueError):
        ex["due_days"] = None
    ex["note"] = data.get("note") or None
    return ex


def get_extractor(env: dict | None = None):
    env = os.environ if env is None else env
    provider = (env.get("LLM_PROVIDER") or "rules").lower()
    if provider in ("openai", "llm", "openrouter", "ollama"):
        default_base = {"openrouter": "https://openrouter.ai/api/v1",
                        "ollama": "http://localhost:11434/v1"}.get(provider, "https://api.openai.com/v1")
        return LLMExtractor(env.get("LLM_BASE_URL") or default_base,
                            env.get("LLM_MODEL") or "gpt-4o-mini", env.get("LLM_API_KEY"))
    return RuleExtractor()


# --------------------------------------------------------------------------- self-check
def _line_totals(items, currency, tax_pct):
    subtotal = Decimal("0")
    tax = Decimal("0")
    for it in items:
        unit = to_decimal(it["unit_price"]) or Decimal("0")
        subtotal += unit * it["quantity"]
        if tax_pct:
            tax += quantize(unit * tax_pct / 100, currency) * it["quantity"]
    return quantize(subtotal, currency), quantize(tax, currency)


def finalize(ex: dict) -> dict:
    """Resolve ambiguity, compute amounts and produce issues for human review."""
    ex = json.loads(json.dumps(ex))  # deep copy
    issues = [{"level": "warning", "msg": w} for w in ex.get("warnings", [])]
    ccy = (ex.get("currency") or "USD").upper()
    ex["currency"] = ccy
    if ccy not in SUPPORTED_CURRENCIES:
        issues.append({"level": "error", "msg": f"Currency {ccy} is not supported by PayPal"})
    elif ccy in RESTRICTED:
        issues.append({"level": "warning", "msg": RESTRICTED[ccy]})
    if ex.get("currency_source") == "default":
        issues.append({"level": "warning", "msg": "No currency found in text; assumed USD"})
    tax_pct = to_decimal(ex.get("tax_percent"))
    shipping = to_decimal(ex.get("shipping")) or Decimal("0")
    stated = to_decimal(ex.get("stated_total"))
    items = ex.get("items") or []

    if not items:
        issues.append({"level": "error", "msg": "No line items with a price were found"})
    for it in items:
        if it.get("unit_price") in (None, "None"):
            issues.append({"level": "error", "msg": f"No price found for '{it['name']}'"})
        if ccy in ZERO_DECIMAL and it.get("unit_price") and to_decimal(it["unit_price"]) % 1:
            issues.append({"level": "warning", "msg": f"{ccy} has no decimals; '{it['name']}' price will be rounded"})

    priced = all(it.get("unit_price") not in (None, "None") for it in items)
    for it in items:  # explicit "2 bags for $30" -> unit price 15
        if priced and it.get("price_basis") == "line":
            it["unit_price"] = str(quantize(to_decimal(it["unit_price"]) / it["quantity"], ccy))
            it["price_basis"] = "unit"
    ambiguous = [i for i, it in enumerate(items) if it.get("price_basis") == "ambiguous"]
    if priced and ambiguous:
        chosen = None
        if stated is not None and len(ambiguous) <= 8:
            for combo in itertools.product(("unit", "line"), repeat=len(ambiguous)):
                trial = json.loads(json.dumps(items))
                for idx, basis in zip(ambiguous, combo):
                    if basis == "line":
                        trial[idx]["unit_price"] = str(quantize(
                            to_decimal(trial[idx]["unit_price"]) / trial[idx]["quantity"], ccy))
                sub, tax = _line_totals(trial, ccy, tax_pct)
                if quantize(sub + tax + shipping, ccy) == quantize(stated, ccy):
                    chosen = (trial, combo)
                    break
        if chosen:
            items = chosen[0]
            for idx, basis in zip(ambiguous, chosen[1]):
                if basis == "line":
                    issues.append({"level": "info", "msg": f"'{items[idx]['name']}': price read as a line "
                                                         f"total because that matches the stated total"})
        else:
            issues.append({"level": "warning",
                           "msg": "Some prices could be unit prices or line totals; assumed unit prices"})
        for idx in ambiguous:
            items[idx]["price_basis"] = "unit"
    for it in items:
        if it.get("unit_price") not in (None, "None"):
            it["unit_price"] = fmt(it["unit_price"], ccy)

    ex["items"] = items
    if priced and items:
        sub, tax = _line_totals(items, ccy, tax_pct)
        total = quantize(sub + tax + shipping, ccy)
        ex["amounts"] = {"subtotal": fmt(sub, ccy), "tax": fmt(tax, ccy), "shipping": fmt(shipping, ccy),
                         "total": fmt(total, ccy)}
        if stated is not None and quantize(stated, ccy) != total:
            issues.append({"level": "warning", "msg": f"Stated total {fmt(stated, ccy)} {ccy} differs from computed "
                                                     f"{fmt(total, ccy)} {ccy}; please double-check"})
    else:
        ex["amounts"] = None
    cust = ex.get("customer") or {}
    if not cust.get("email"):
        issues.append({"level": "info", "msg": "No customer email: will create a PayPal checkout link to paste back into the chat"})
    if not cust.get("name"):
        issues.append({"level": "warning", "msg": "Customer name not found"})

    errors = sum(1 for i in issues if i["level"] == "error")
    warns = sum(1 for i in issues if i["level"] == "warning")
    ex["issues"] = issues
    ex["confidence"] = round(max(0.0, 1.0 - 0.5 * errors - 0.12 * warns), 2)
    ex["ready"] = errors == 0
    return ex
