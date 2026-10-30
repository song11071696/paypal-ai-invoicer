# Paste2Pay: 3-minute demo video script

**Target length:** 2:45, safely under the 3:00 limit.

**Before recording:**
- Run in **sandbox mode** (`.env` has the sandbox keys), so the PayPal calls are real.
- Have the sandbox Personal (buyer) account logged in on a second browser profile.
- Reset the ledger.
- Set browser zoom to 110%.
- Use no copyrighted music. Use your own voice or a silent screen with captions.

| Time | On screen | Voice-over (EN) |
|---|---|---|
| 0:00–0:15 | A phone-style chat (`samples/chat_order.txt`), then the Paste2Pay header showing **AI: …** and **PayPal: sandbox REST** | "Small sellers take orders in chats and DMs, then retype every one into an invoice and spend evenings checking who actually paid. Paste2Pay is an AI receivables agent built on PayPal: paste the conversation, get paid." |
| 0:15–0:45 | Paste the chat → **Read with AI**. Zoom on the AI draft: customer, email, 2 items, total 74.50 USD, confidence 100% | "The AI reads the chat: Maya Chen, two speckled mugs at 18 dollars, one matcha bowl, 6.50 shipping. It recomputes the total exactly the way PayPal will and checks it against the 74.50 written in the chat." |
| 0:45–1:10 | Click **Create in PayPal**. Zoom on the trace: `decide: INVOICE…`, `POST /v1/oauth2/token 200`, `POST /v2/invoicing/generate-next-invoice-number`, `POST /v2/invoicing/invoices 201`, `.../send 200` | "There's an email, so the agent decides on a PayPal invoice. Every real sandbox call is visible here: OAuth, invoice number, create, send. The invoice is now in Maya's inbox." |
| 1:10–1:35 | Click the **OCR invoice photo** sample → Read with AI. Highlight the info line "price read as a line total…" | "Here's messy OCR text from a photographed invoice. '4 centrepieces, 180 euros' — unit price or line total? The AI tests both readings against the grand total and picks the one that adds up. It's 45 euros each." |
| 1:35–2:00 | Click the **DM, no email** sample → Create. The trace shows `ORDER` and `POST /v2/checkout/orders`. Click **Copy link**, open it in the buyer profile, the PayPal sandbox checkout appears → **Pay**. It redirects back and the row turns **PAID** | "This Instagram buyer has no email, so the agent creates a PayPal Orders checkout link to paste into the DM instead. The buyer pays, PayPal returns them to us, and the agent captures the payment immediately. Paid, 30 pounds." |
| 2:00–2:30 | Section 5 → **Load sample notices** → **Match payments**. Highlight: bank transfer → recorded on invoice (`POST /invoices/{id}/payments`), "You received $74.50" → **WARNING possible fake**, unknown deposit → unmatched | "End of day: paste the bank statement and payment notices. The bank transfer is matched to Daniel's invoice and recorded in PayPal. But this 'you received 74.50' message? PayPal's API says the invoice is still unpaid, so the agent flags it as a possible fake before you ship anything. A classic seller scam, caught." |
| 2:30–2:45 | Ledger tiles: Outstanding, Collected, Exceptions. Click **Reminder**, which shows the drafted message with the pay link | "Outstanding, collected, exceptions, and a one-click reminder with the PayPal link. Paste2Pay: AI reads the mess, PayPal moves the money. Open source, runs in one command." |

**If you record in mock mode instead,** say "running against our PayPal mock". Use the **Simulate buyer pays** and **Sync** buttons in place of the real checkout. Sandbox footage is stronger for the "Best Use of PayPal + AI" judging.

**Captions to overlay:**
- "PayPal Invoicing v2"
- "PayPal Orders v2"
- "OAuth 2.0 client credentials"
- "AI extraction + self-check"
- "AI payment matching"
