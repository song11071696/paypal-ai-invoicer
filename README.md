# Paste2Pay - an AI receivables agent built on PayPal

**Paste a buyer chat, an emailed order or the OCR text of an invoice photo. Paste2Pay's AI reads it, PayPal bills it, and the agent tracks the payment and reconciles it.**

Millions of small sellers take orders in WhatsApp, Instagram DMs, WeChat or email, and then retype every order into an invoice. Later they scroll through notifications to check who actually paid. Paste2Pay turns that into one paste:

1. **AI reads the mess.** It pulls out the customer, the line items, quantities, unit prices, currency, shipping, tax, terms and notes. It then checks its own work. For example, "is `x4 - 180.00` a unit price or a line total?" is settled by testing both readings against the stated total.
2. **The agent decides and calls PayPal.** If the text has an email address, the agent creates and sends a **PayPal invoice** (Invoicing v2). If the buyer is only in a chat, it creates a **PayPal checkout link** (Orders v2) to paste back into the conversation.
3. **It tracks every bill to "paid".** It syncs status from PayPal and captures approved orders automatically. It flags underpayments and drafts a reminder message with the pay link.
4. **It reconciles.** Paste bank-statement lines or "you received $X" notices. The AI matches each one to an open bill by reference, amount and payer name. It records off-PayPal payments on the PayPal invoice through the API. It **flags fake "payment received" messages** when PayPal's API says the money never arrived.

Every step appears in an agent trace, from the AI's reading and decision to each PayPal API call with its HTTP status.

---

## Where PayPal is used

| PayPal API (sandbox) | What Paste2Pay does with it | Code |
|---|---|---|
| OAuth 2.0 `POST /v1/oauth2/token` (client_credentials) | Gets the access token and caches it until it expires | `paste2pay/paypal.py` `PayPalClient.token` |
| Orders v2 `POST /v2/checkout/orders` | Checkout link for chat buyers, with itemised `purchase_units`, an item/shipping/tax breakdown, `PayPal-Request-Id` idempotency and a `payer-action` link | `create_order` |
| Orders v2 `GET /v2/checkout/orders/{id}` + `POST .../capture` | Tracks status. The agent captures automatically on buyer return (`/paypal/return`) or during sync | `get_order`, `capture_order` |
| Invoicing v2 `POST /v2/invoicing/generate-next-invoice-number` | Gets the next invoice number from PayPal | `next_invoice_number` |
| Invoicing v2 `POST /v2/invoicing/invoices` + `POST .../send` | Creates the draft invoice (recipient, items, tax, shipping, payment terms) and emails it | `create_invoice`, `send_invoice` |
| Invoicing v2 `GET /v2/invoicing/invoices/{id}` | Tracks status: SENT → PAID / PARTIALLY_PAID / MARKED_AS_PAID | `get_invoice` |
| Invoicing v2 `POST /v2/invoicing/invoices/{id}/payments` | Records a bank transfer or cash payment found during reconciliation, so PayPal stays the single source of truth | `record_invoice_payment` |

Credentials come only from the environment variables `PAYPAL_CLIENT_ID` and `PAYPAL_CLIENT_SECRET`. Nothing is committed. **With no credentials, the app switches to a mock PayPal automatically.** The mock returns the same JSON shapes, so judges can run everything offline.

## Where AI is used

| AI piece | What it does | Code |
|---|---|---|
| Order extraction | Turns free text (chat, email, OCR, English or Chinese) into customer, items, currency, shipping, tax, terms and notes | `paste2pay/extract.py` |
| Self-check | Decides unit price vs line total against the stated total, recomputes totals the way PayPal will, and flags currency limits (JPY has no decimals; CNY works only for in-country accounts). Missing prices are blocking errors and stop anything from reaching PayPal | `finalize()` |
| Agent decision | Chooses invoice or checkout link, captures approved orders and flags mismatches | `paste2pay/agent.py` |
| Payment matching | Reads payment notices and statement lines, then fuzzy-matches them to open bills by reference, amount and payer name. It detects duplicates and fake notices | `Agent.match_payments` |

The AI engine is pluggable:
- `rules` (default) is a deterministic offline parser. It needs no key and costs nothing.
- `LLM_PROVIDER=openai|openrouter|ollama` works with **any OpenAI-compatible chat-completions model**, including a free local Ollama model. If the model fails or returns bad JSON, Paste2Pay falls back to the rule engine and says so in the trace.

---

## Run it

Requirements: **Python 3.10+. No pip install. Zero third-party dependencies** (standard library only).

### 1. Terminal demo (offline, 10 seconds)

```bash
git clone <this repo> && cd paste2pay
python3 -m paste2pay demo
```

**You will see:**
- Three pasted orders. The AI extracts each one, then the agent decides: two become **PayPal invoices** (`POST /v2/invoicing/invoices`, `.../send`) and one becomes a **PayPal checkout link** (`POST /v2/checkout/orders`). Every PayPal call is printed with its status.
- A reconciliation step where a bank transfer is recorded on the PayPal invoice (`MARKED_AS_PAID`). A "You received $74.50" notice is flagged **possible fake**, because PayPal still says `SENT`. An unknown deposit is left for a human.
- Buyers paying (simulated in mock mode), then `sync` moving bills to `PAID`, and a ledger summary.

### 2. Web app

```bash
python3 -m paste2pay serve          # http://localhost:8000
```

**Then:**
1. Click the sample **"Chat order (has email)"**. You will see the **AI draft**: customer, items, totals, confidence and any issues. All fields are editable.
2. Click **Create in PayPal**. The **Agent trace** shows the AI steps and each PayPal API call (`POST /v1/oauth2/token`, `POST /v2/invoicing/invoices`, ...). A new row appears in the ledger with its pay link.
3. Click the sample **"DM, no email"** and then **Create in PayPal**. This time the agent makes a **checkout link** (Orders v2).
4. In mock mode, click **Simulate buyer pays** and then **Sync with PayPal**. The status changes to `PAID` and the *Collected* total goes up.
5. In section 5, click **Load sample notices** and then **Match payments**. You will see each payment's match, its score and the action taken, including the fake-notice warning.

### 3. Real PayPal sandbox

1. At [developer.paypal.com](https://developer.paypal.com), open **Apps & Credentials**, choose **Sandbox** and create an app (free). Copy the Client ID and Secret.
2. Run `cp .env.example .env` and fill in `PAYPAL_CLIENT_ID` and `PAYPAL_CLIENT_SECRET`. You can also `export` them instead.
3. Run `python3 -m paste2pay serve`. The header now says **PayPal: sandbox REST (live calls)**.
4. **Checkout link:** create an order, open the pay link and log in with your **sandbox Personal account** (Developer Dashboard → Sandbox → Accounts). Click Pay. PayPal redirects to `/paypal/return` and the agent captures the order. The row turns `PAID` with the captured amount.
5. **Invoice:** paste an order that contains your sandbox Personal account's email. PayPal sends the sandbox invoice. Pay it from the sandbox account, then click **Sync with PayPal**.

Errors from PayPal appear in the trace with the PayPal `debug_id`.

### 4. Optional: use an LLM

```bash
export LLM_PROVIDER=openai LLM_API_KEY=sk-... LLM_MODEL=gpt-4o-mini
# or completely free and local:
export LLM_PROVIDER=ollama LLM_MODEL=llama3.1      # needs `ollama serve`
python3 -m paste2pay serve
```

The header shows `AI: llm:<model>`.

### 5. Tests

```bash
python3 -m unittest discover -s tests -v
```

**Expected:** `Ran 36 tests ... OK`. The tests cover:
- extraction of every sample, including Chinese, JPY and tax
- the LLM adapter, using a fake endpoint and the fallback path
- the exact PayPal request bodies, OAuth Basic auth, token caching, idempotency headers and error/`debug_id` handling, against a fake sandbox transport
- the full agent flows: invoice and order to paid, return-URL capture, underpayment, blocked input, reconciliation, duplicates and fake notices
- the HTTP API end to end

---

## Architecture

```
 paste (chat / email / OCR)          paste (payment notices / statement)
            │                                       │
   ┌────────▼─────────┐                     ┌───────▼────────┐
   │  AI extractor    │ rules | any LLM     │  AI payment    │
   │  + self-check    │────────────────┐    │  parser+match  │
   └────────┬─────────┘                │    └───────┬────────┘
            │ structured order         │            │
   ┌────────▼─────────┐  decide        │    ┌───────▼────────┐
   │      Agent       │ invoice/order  └───►│  Reconciler    │
   └────────┬─────────┘                     └───────┬────────┘
            │ OAuth · Orders v2 · Invoicing v2      │ record payment / verify
   ┌────────▼───────────────────────────────────────▼────────┐
   │   PayPalClient (api-m.sandbox.paypal.com)  |  MockPayPal │
   └────────┬───────────────────────────────────────────────┘
            ▼
     ledger (data/ledger.json) → web UI / CLI
```

| File | Role |
|---|---|
| `paste2pay/extract.py` | AI engines (rules / LLM) and the self-check |
| `paste2pay/paypal.py` | Sandbox REST client, request-body builders and the mock |
| `paste2pay/agent.py` | Decision, tracking, auto-capture, reconciliation and reminders |
| `paste2pay/server.py`, `static/index.html` | Zero-dependency web app |
| `paste2pay/cli.py` | `serve`, `demo` and `extract` commands |
| `samples/` | Demo inputs |
| `tests/` | Unit and integration tests |

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `PAYPAL_CLIENT_ID` / `PAYPAL_CLIENT_SECRET` | empty → mock | Sandbox REST app credentials |
| `PAYPAL_MODE=mock` | - | Forces mock mode even when keys are set |
| `PAYPAL_BASE_URL` | `https://api-m.sandbox.paypal.com` | API host |
| `PUBLIC_URL` | `http://localhost:<port>` | Base for the Orders `return_url` / `cancel_url` |
| `BRAND_NAME` | - | Brand on the checkout page and invoice |
| `LLM_PROVIDER` | `rules` | `rules`, `openai`, `openrouter` or `ollama` |
| `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY` | per provider | Any OpenAI-compatible endpoint |
| `LEDGER_PATH` | `data/ledger.json` | Local ledger file |

## Honest limits

- This is a prototype running on the **PayPal sandbox**. It is not for production money.
- The rule engine covers common English and Chinese order phrasings. Use an LLM for arbitrary language.
- Image OCR is out of scope. Paste the text from any OCR tool, such as the phone's built-in text recognition.
- Tracking works by polling (`Sync`). Webhooks would be the next step.

## License

MIT. See [LICENSE](LICENSE).
