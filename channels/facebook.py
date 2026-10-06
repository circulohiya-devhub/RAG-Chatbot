"""
Facebook Messenger channel.
Webhook URL for Meta: https://YOUR-DOMAIN/webhooks/facebook
"""
import json
import logging
import os

import requests
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from core import IncomingMessage, handle_incoming
from .base import Channel, verify_hmac_sha256

log = logging.getLogger("channel.facebook")

VERIFY_TOKEN = os.getenv("FB_VERIFY_TOKEN", "")
APP_SECRET = os.getenv("FB_APP_SECRET", "")
PAGE_ACCESS_TOKEN = os.getenv("FB_PAGE_ACCESS_TOKEN", "")
GRAPH_API_VERSION = os.getenv("GRAPH_API_VERSION", "v23.0")


class FacebookChannel(Channel):
    name = "facebook"
    max_chars = 2000

    def is_configured(self) -> bool:
        return all([VERIFY_TOKEN, APP_SECRET, PAGE_ACCESS_TOKEN])

    def _post(self, payload: dict) -> None:
        r = requests.post(
            f"https://graph.facebook.com/{GRAPH_API_VERSION}/me/messages",
            params={"access_token": PAGE_ACCESS_TOKEN},
            json=payload,
            timeout=15,
        )
        if r.status_code >= 400:
            log.error("Messenger Send API error %s: %s", r.status_code, r.text)

    def send_text(self, user_id: str, text: str) -> None:
        self._post({
            "recipient": {"id": user_id},
            "messaging_type": "RESPONSE",
            "message": {"text": text},
        })

    def send_typing(self, user_id: str) -> None:
        self._post({"recipient": {"id": user_id}, "sender_action": "typing_on"})


channel = FacebookChannel()
router = APIRouter(prefix="/webhooks", tags=["Facebook Messenger"])


@router.get("/facebook")
def verify(
    hub_mode: str | None = Query(None, alias="hub.mode"),
    hub_verify_token: str | None = Query(None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(None, alias="hub.challenge"),
):
    if hub_mode == "subscribe" and hub_verify_token == VERIFY_TOKEN:
        return PlainTextResponse(hub_challenge or "")
    raise HTTPException(status_code=403, detail="Verification failed")


@router.post("/facebook")
async def receive(request: Request, background: BackgroundTasks):
    body = await request.body()
    if not verify_hmac_sha256(APP_SECRET, body, request.headers.get("X-Hub-Signature-256", ""), "sha256="):
        raise HTTPException(status_code=403, detail="Invalid signature")

    payload = json.loads(body)
    if payload.get("object") != "page":
        return {"status": "ignored"}

    # Reply to Meta right away; answer the guest in the background.
    for entry in payload.get("entry", []):
        for event in entry.get("messaging", []):
            msg = _parse(event)
            if msg:
                background.add_task(handle_incoming, msg, channel)
    return {"status": "ok"}


def _parse(event: dict) -> IncomingMessage | None:
    message = event.get("message")
    psid = event.get("sender", {}).get("id")
    if not psid or not message or message.get("is_echo"):
        return None  # delivery receipts, reads, and the Page's own messages
    return IncomingMessage(
        channel=channel.name,
        user_id=psid,
        text=message.get("text") or "",
        message_id=message.get("mid"),
    )
