"""
WhatsApp channel (WhatsApp Business Platform / Cloud API).
Prepared but NOT YET TESTED. Stays switched off until its settings are in .env.
Webhook URL for Meta: https://YOUR-DOMAIN/webhooks/whatsapp
"""
import json
import logging
import os

import requests
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from core import IncomingMessage, handle_incoming
from .base import Channel, verify_hmac_sha256

log = logging.getLogger("channel.whatsapp")

VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "")
APP_SECRET = os.getenv("WHATSAPP_APP_SECRET", "")
ACCESS_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN", "")
PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
GRAPH_API_VERSION = os.getenv("GRAPH_API_VERSION", "v23.0")


class WhatsAppChannel(Channel):
    name = "whatsapp"
    max_chars = 4096

    def is_configured(self) -> bool:
        return all([VERIFY_TOKEN, APP_SECRET, ACCESS_TOKEN, PHONE_NUMBER_ID])

    def send_text(self, user_id: str, text: str) -> None:
        r = requests.post(
            f"https://graph.facebook.com/{GRAPH_API_VERSION}/{PHONE_NUMBER_ID}/messages",
            headers={"Authorization": f"Bearer {ACCESS_TOKEN}"},
            json={
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": user_id,
                "type": "text",
                "text": {"body": text},
            },
            timeout=15,
        )
        if r.status_code >= 400:
            log.error("WhatsApp send error %s: %s", r.status_code, r.text)


channel = WhatsAppChannel()
router = APIRouter(prefix="/webhooks", tags=["WhatsApp"])


@router.get("/whatsapp")
def verify(
    hub_mode: str | None = Query(None, alias="hub.mode"),
    hub_verify_token: str | None = Query(None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(None, alias="hub.challenge"),
):
    if hub_mode == "subscribe" and hub_verify_token == VERIFY_TOKEN:
        return PlainTextResponse(hub_challenge or "")
    raise HTTPException(status_code=403, detail="Verification failed")


@router.post("/whatsapp")
async def receive(request: Request, background: BackgroundTasks):
    body = await request.body()
    if not verify_hmac_sha256(APP_SECRET, body, request.headers.get("X-Hub-Signature-256", ""), "sha256="):
        raise HTTPException(status_code=403, detail="Invalid signature")

    payload = json.loads(body)
    if payload.get("object") != "whatsapp_business_account":
        return {"status": "ignored"}

    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            names = {
                c.get("wa_id"): c.get("profile", {}).get("name")
                for c in value.get("contacts", [])
            }
            # value["statuses"] (sent/delivered/read updates) are ignored
            for m in value.get("messages", []):
                sender = m.get("from")
                if not sender:
                    continue
                text = m.get("text", {}).get("body", "") if m.get("type") == "text" else ""
                msg = IncomingMessage(
                    channel=channel.name,
                    user_id=sender,
                    text=text,
                    message_id=m.get("id"),
                    user_name=names.get(sender),
                )
                background.add_task(handle_incoming, msg, channel)
    return {"status": "ok"}
