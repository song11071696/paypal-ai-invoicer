# Devpost submission draft: Paste2Pay

**Tagline:** Paste a chat or invoice, then let AI and PayPal bill it, track it and reconcile it.

## Inspiration
A huge share of small-business commerce never touches a storefront. Ceramicists, florists, bakers and resellers take orders in WhatsApp, Instagram DMs, WeChat and email. Every order gets retyped into an invoice by hand. Every evening goes to scrolling notifications to work out who has actually paid, and fake "payment sent" screenshots are a well-known scam against these sellers. We wanted the shortest possible path from a messy conversation to money received, with PayPal doing the money part.

## What it does
Paste2Pay is an AI receivables agent:
- **Reads anything.** You paste a buyer chat, an emailed order or the OCR text of a paper invoice. The AI extracts the customer, email, line items, quantities, unit prices, currency, shipping, tax, payment terms and notes.
- **Checks itself.** It works out whether "x4 – 180.00" is a unit price or a line total by testing both readings against the stated total. It recomputes amounts exactly as PayPal will and flags currency restrictions. It refuses to bill when a price is missing.
- **Decides and acts on PayPal.** If there is an email, it creates and sends a PayPal invoice (Invoicing v2, with PayPal-generated invoice numbers, payment terms, tax and shipping). If the buyer is only in a chat, it creates a PayPal Orders v2 checkout link to paste back into the conversation.
- **Tracks to paid.** It syncs status from PayPal, captures approved orders automatically when the buyer returns, flags underpayments and drafts a friendly reminder that includes the pay link.
- **Reconciles.** You paste bank-statement lines or payment notices. The AI matches each one to an open bill by reference, amount and payer name. It records off-PayPal payments on the PayPal invoice through the API, detects duplicates and flags **"payment received" messages that PayPal's API contradicts**.
- **Shows its work.** Every AI step and every PayPal API call, with its HTTP status and debug_id, appears in a live agent trace.

## How we built it
- **PayPal:** REST APIs in the sandbox.
  - OAuth 2.0 client credentials, with the token cached until it expires
  - Orders v2: create with itemised breakdown, `payment_source.paypal.experience_context`, `PayPal-Request-Id` idempotency, get and capture
  - Invoicing v2: generate-next-invoice-number, create, send, get and record payment
  - The client has a mock twin that returns identical JSON shapes, so judges can run the project with zero setup. Both share the same request-body builders.
- **AI:** A pluggable extraction layer.
  - A deterministic engine (works offline and needs no key) plus an adapter for any OpenAI-compatible chat-completions model: OpenAI, OpenRouter, or a free local model through Ollama.
  - If the model fails, it falls back to the deterministic engine.
  - A shared self-check validates every result before any money request is created.
- **App:** Python standard library only: `http.server` and a single-page vanilla-JS UI, plus a CLI demo.
- **Tests:** 36 unit and integration tests. They cover the exact PayPal request bodies, auth headers, idempotency, error handling, the agent flows and the HTTP API.

## Challenges we ran into
- **Unit price vs line total.** Real invoices are ambiguous about whether a price is per unit or per line. We solved it by searching over interpretations and keeping the one that matches the stated total.
- **Matching PayPal's arithmetic.** PayPal validates `item_total`, `tax_total` and the per-item tax, so our totals have to be computed the same way PayPal computes them.
- **Mock that matches the sandbox.** We had to keep the mock behaviour faithful to the sandbox: status lifecycles, capture-only-after-approval and idempotent creates.

## Accomplishments we're proud of
- From a paste to a sent PayPal invoice in one click, with every step explained.
- The fake-payment-notice check, which never trusts a pasted message over the PayPal API.
- It runs anywhere in one command, with no dependencies and no keys needed to try it.

## What we learned
- How Orders v2 and Invoicing v2 complement each other: invoices suit known customers, checkout links suit chat buyers.
- Keeping AI output behind a deterministic validator makes an agent safe to let near money.

## What's next for Paste2Pay
- PayPal webhooks instead of polling.
- A WhatsApp/Telegram bot front-end.
- Direct image OCR.
- PayPal Transaction Search for automatic statement import.
- Multi-merchant hosting.

## Built with
`paypal-orders-v2` · `paypal-invoicing-v2` · `paypal-oauth2` · `paypal-sandbox` · `python` · `openai-compatible-llm` · `ollama` · `javascript` · `html5`

## Testing instructions for judges
1. Run `git clone <repo> && cd <repo> && python3 -m paste2pay demo`. This works offline with no keys and prints every step.
2. Run `python3 -m paste2pay serve` and open http://localhost:8000. Click a sample, then **Read with AI**, then **Create in PayPal**. Use **Simulate buyer pays**, then **Sync**, then **Match payments**.
3. For real PayPal sandbox calls, put a sandbox REST app's `PAYPAL_CLIENT_ID` and `PAYPAL_CLIENT_SECRET` in `.env`. See the README section "Real PayPal sandbox".
4. Run the tests with `python3 -m unittest discover -s tests`.

## Links
- Code: `<public GitHub URL>`
- Video: `<YouTube URL>`
