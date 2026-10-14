"""
Viber channel (Viber Bot / REST API).
Prepared but NOT YET TESTED. Stays switched off until its settings are in .env.
Webhook URL (registered with a set_webhook call, see README): https://YOUR-DOMAIN/webhooks/viber
"""
import json
import logging
import os

import requests
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from core import IncomingMessage, handle_incoming
from .base import Channel, verify_hmac_sha256

log = logging.getLogger("channel.viber")

AUTH_TOKEN = os.getenv("VIBER_AUTH_TOKEN", "")
BOT_NAME = os.getenv("VIBER_BOT_NAME", "Casa Alon Hotel")


class ViberChannel(Channel):
    name = "viber"
    max_chars = 7000

    def is_configured(self) -> bool:
        return bool(AUTH_TOKEN)

    def send_text(self, user_id: str, text: str) -> None:
        r = requests.post(
            "https://chatapi.viber.com/pa/send_message",
            headers={"X-Viber-Auth-Token": AUTH_TOKEN},
            json={
                "receiver": user_id,
                "min_api_version": 1,
                "sender": {"name": BOT_NAME},
                "type": "text",
                "text": text,
            },
            timeout=15,
        )
        data = r.json() if r.content else {}
        if r.status_code >= 400 or data.get("status", 0) != 0:
            log.error("Viber send error %s: %s", r.status_code, r.text)


channel = ViberChannel()
router = APIRouter(prefix="/webhooks", tags=["Viber"])


@router.post("/viber")
async def receive(request: Request, background: BackgroundTasks):
    body = await request.body()
    if not verify_hmac_sha256(AUTH_TOKEN, body, request.headers.get("X-Viber-Content-Signature", "")):
        raise HTTPException(status_code=403, detail="Invalid signature")

    event = json.loads(body)
    # "webhook" is Viber's check when you register the URL; other events
    # (delivered, seen, subscribed, conversation_started, ...) are ignored for now.
    if event.get("event") != "message":
        return {"status": "ok"}

    sender = event.get("sender", {})
    message = event.get("message", {})
    if not sender.get("id"):
        return {"status": "ignored"}

    msg = IncomingMessage(
        channel=channel.name,
        user_id=sender["id"],
        text=message.get("text", "") if message.get("type") == "text" else "",
        message_id=str(event.get("message_token", "")) or None,
        user_name=sender.get("name"),
    )
    background.add_task(handle_incoming, msg, channel)
    return {"status": "ok"}
