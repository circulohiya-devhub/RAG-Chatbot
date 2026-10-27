# Casa Alon Hotel Bot (FastAPI + LangChain RAG, multi-channel)

Answers hotel inquiries on chat apps using the live Google Doc as its knowledge base. Doc edits go live within about a minute, with no redeploy.

## How it's organized

```
casa-alon-bot/
├── main.py              Server: turns on configured channels, plus /ask, /refresh, /health
├── rag.py               Knowledge base + agent. Edit the prompt, hotel name, contacts here.
├── pms.py               Live rates, availability, and promos (mock or Hotelogix)
├── core.py              Shared by all channels: dedupe, answering, staff handoff
├── static/
│   ├── chat.js          The website chat widget (one <script> tag)
│   └── demo.html        Test page for the widget
├── channels/
│   ├── base.py          What every channel must do (receive, send, is_configured)
│   ├── facebook.py      Facebook Messenger   ← testing now
│   ├── whatsapp.py      WhatsApp             ← prepared, not yet tested
│   ├── viber.py         Viber                ← prepared, not yet tested
│   └── web.py           Website chat widget
├── requirements.txt
└── .env.example         Copy to .env and fill in
```

Every guest message follows the same path, whatever the app:

```
Facebook / WhatsApp / Viber webhook
        ↓  (channel file turns it into an IncomingMessage)
core.handle_incoming  →  rag.answer  →  channel.reply
        ↓ if handoff
STAFF_WEBHOOK_URL (n8n): notify staff / start booking workflow

Gmail and other n8n-handled channels  →  POST /ask  →  n8n sends the reply
```

**A channel switches on automatically when all of its settings are in `.env`.** Empty settings mean it stays off, so WhatsApp and Viber do nothing until you fill them in. `GET /health` shows which channels are on.

## Live rates and promos (PMS)

The bot answers from two sources:

- **The Google Doc** for things that rarely change: policies, amenities, room descriptions.
- **The PMS** (`pms.py`) for things that change daily: rates, availability, and promos, looked up live when a guest gives dates.

`PMS_PROVIDER=mock` (the default) uses sample data, so you can test conversations like "How much is a Deluxe room this weekend for 2 adults?" right away.

### Switching to the Hotelogix sandbox

1. In `.env`, set `PMS_PROVIDER=hotelogix` and fill in `HOTELOGIX_CONSUMER_KEY`, `HOTELOGIX_CONSUMER_SECRET`, and `HOTELOGIX_HOTEL_ID` from Hotelogix. Keep `HOTELOGIX_BASE_URL` on the sandbox (demo) URL while testing.
2. Set `HOTELOGIX_DEBUG=true`, restart, and ask the bot for rates on a **2-night** stay. The terminal shows the raw Hotelogix response.
3. Compare the `price` in the response with what the sandbox's own booking engine shows for the same dates:
   - Whole-stay price: keep `HOTELOGIX_PRICE_IS_TOTAL=true`.
   - One-night price: set it to `false`.
4. Check the `rateType` values in the response. If public rates show a specific type (for example `WEB`), set `HOTELOGIX_RATE_TYPES=WEB` so only those can ever be quoted.
5. Set `HOTELOGIX_DEBUG=false` again when done, and switch `HOTELOGIX_BASE_URL` to the live URL only when Hotelogix approves go-live.

How it works: the server gets an access key from `wsauth`, signs every request with HMAC-SHA1, and renews the key automatically if it expires. It never sends `searchfor`, so only web rates are requested, and any Travel Agent or Corporate rate that still comes back is dropped. Hotelogix requires request times within 5 minutes of real UTC time, which hosting servers handle automatically.

Promos: the Hotelogix Web API has no separate promo call. Packages and promo rates come back as rate plans in search results, so guests see them when they check dates. General promo descriptions ("what promos do you have?") still come from the Google Doc.

The PMS tools are **read-only**. The bot can quote rates but can't create, change, or cancel bookings. When a guest is ready to book, it hands off to staff or your n8n booking workflow.

Identical searches are cached for `PMS_CACHE_SECONDS` (default 5 minutes) to limit API calls.

## Reservations (no payment)

With `BOOKINGS_ENABLED=true`, the bot can reserve one room per reservation directly in the PMS:

1. It checks live rates, then collects the guest's first name, last name, email, and mobile number.
2. `prepare_reservation` re-checks the price and shows the guest a full summary. Nothing is booked yet.
3. The guest must reply YES in a **later** message. The code (not the AI) checks this before anything is created.
4. `confirm_reservation` re-checks the price, then runs Hotelogix `addtocart` → `savebooking` → `confirmbooking` (payment type OTH, no payment).
5. The guest gets the reservation number, and staff get an alert (`"event": "reservation"`) at `STAFF_WEBHOOK_URL`.

Safeguards: price and availability re-checked twice, one booking at a time (the Hotelogix cart belongs to the access key), no retries on booking steps (so no double bookings), summaries expire after 15 minutes, and a daily limit per guest (`MAX_BOOKINGS_PER_GUEST_PER_DAY`). If confirmation fails after the reservation is saved, it stays on hold in Hotelogix and staff are alerted.

The Hotelogix key needs permission for `addtocart`, `savebooking`, `confirmbooking`, and ideally `clearcart`. A missing permission returns error 3992.

Guest names, emails, and phone numbers pass through Gemini and appear in LangSmith traces. Keep LangSmith access limited to your team.

## 1. Run locally

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    Mac/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # Windows: copy .env.example .env
# fill in .env
uvicorn main:app --reload --port 8000
```

The terminal should show `Knowledge base reloaded: N chunks` and which channels are enabled.

## 2. Test without any chat app

Open http://localhost:8000/docs, choose `POST /ask`, click "Try it out", put your `INTERNAL_API_KEY` in `x-api-key`, and send:

```json
{ "question": "Are pets allowed?", "user_id": "test1", "channel": "test" }
```

Reuse the same `user_id` to test follow-up questions. Every run appears in LangSmith.

## 3. Public URL for local testing

```bash
ngrok http 8000
```

Use the `https://....ngrok-free.app` address in the webhook URLs below.

## 4. Facebook Messenger (now)

1. At developers.facebook.com, create an app and add the **Messenger** product.
2. In **App settings → Basic**, copy the **App Secret** into `FB_APP_SECRET`.
3. In **Messenger API settings**, connect your Page and generate a **Page access token** for `FB_PAGE_ACCESS_TOKEN`.
4. Set `FB_VERIFY_TOKEN` to any long random text, then restart uvicorn.
5. Webhook callback URL: `https://YOUR-URL/webhooks/facebook`, verify token: your `FB_VERIFY_TOKEN`. Click **Verify and save**.
6. Subscribe the Page to the **messages** field.
7. Message the Page from an account with a role in the app (admin, developer, tester).

While the app is in development mode, only people with a role in the app get replies. Real guests need Meta's **App Review** approval for messaging. Meta's menu names change over time.

## 5. WhatsApp (later)

1. In the same or another Meta app, add the **WhatsApp** product and register a business phone number.
2. Fill in `WHATSAPP_ACCESS_TOKEN` (use a permanent System User token for production, since the temporary token expires), `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_APP_SECRET`, and `WHATSAPP_VERIFY_TOKEN`.
3. Webhook URL: `https://YOUR-URL/webhooks/whatsapp`. Subscribe to the **messages** field.

Free-form replies only work within 24 hours of the guest's last message. Messages you start (confirmations, reminders) need approved templates, which is a job for n8n, not this bot.

## 6. Viber (later)

1. Create a Viber bot account and copy its token into `VIBER_AUTH_TOKEN`. Check Viber's current business bot terms and fees first.
2. Viber has no webhook form, so register the URL once with:

```bash
curl -X POST https://chatapi.viber.com/pa/set_webhook \
  -H "X-Viber-Auth-Token: YOUR_VIBER_AUTH_TOKEN" \
  -d '{"url": "https://YOUR-URL/webhooks/viber", "event_types": ["message"]}'
```

The server must already be running, because Viber checks the URL immediately.

## 7. Website chat widget

1. In `.env`, set `WEB_ENABLED=true`, then restart uvicorn.
2. Open http://localhost:8000/widget/demo and chat with the bot on the test page.
3. To add it to the hotel website, paste this before `</body>` on every page (or in the site's footer or theme settings):

```html
<script src="https://YOUR-SERVER/widget/chat.js"
        data-hotel="Casa Alon Hotel"
        data-phone="+63 917 000 0000"
        defer></script>
```

4. Put the website's address in `WEB_ALLOWED_ORIGINS`, exactly as it appears in the browser, for example `https://www.casaalonhotel.ph`. Include both the `www` and non-`www` versions if the site uses both. Other websites are then blocked from using your chat.

Optional attributes: `data-color` and `data-accent` (brand colors), `data-greeting` (first message), and `data-suggestions` (quick questions separated by `|`).

Good to know:

- **The chat endpoint is public**, because a browser can't keep an API key secret. It's protected by the allowed websites list, a 1,000-character message limit, and rate limits (`WEB_RATE_*` in `.env`) that cap how many Gemini calls one visitor can cause.
- **Behind a hosting proxy** (Cloud Run, Render, Railway), set `WEB_TRUST_PROXY=true` so rate limits see each visitor's real IP address instead of the proxy's.
- **Website visitors are anonymous.** When the bot hands off, the widget shows the front desk phone number, because staff have no way to contact a web visitor. The handoff alert still goes to `STAFF_WEBHOOK_URL`.
- **Conversations continue across pages** for 24 hours in the same browser. "New chat" in the header starts fresh.
- **WordPress, Wix, Squarespace** and similar builders all have a place for custom footer code or an "embed code" block. Paste the script tag there.

## 8. Gmail and other channels via n8n

These don't need code here. In n8n:

1. **Gmail Trigger** (new email)
2. **HTTP Request** → `POST https://YOUR-URL/ask`, header `X-API-Key`, body:
   `{"question": "<email text>", "user_id": "<sender email>", "channel": "email"}`
3. **IF** `handoff` is true → notify staff or start your booking workflow. Otherwise → **Gmail: create draft** (or reply) with `answer`.

Start with drafts that staff review, and switch to auto-reply once you trust it.

## 9. Adding another channel later (Telegram, Instagram, ...)

1. Copy `channels/facebook.py` to `channels/<name>.py`.
2. Change the settings, `send_text`, and how the webhook is read into an `IncomingMessage`.
3. Add it to `ALL_CHANNELS` in `channels/__init__.py`.

Nothing in `rag.py` or `core.py` needs to change.

## Things to know

- **Doc edits** go live automatically. `POST /refresh` (with `x-api-key`) forces an immediate reload.
- **Memory:** leave `MEMORY_DB_PATH` empty while testing. Set it (for example `memory.db`) to keep conversations across restarts. With several server copies in production, move to a shared database such as Postgres.
- **Each channel has its own conversation.** The same person on Facebook and WhatsApp is two separate guests to the bot.
- **Run one server instance** while memory is in-process or SQLite (`--workers 1`).
- **Secrets** stay in `.env` or your host's environment variables. Never commit `.env`.
