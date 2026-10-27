"""
Channel-independent message handling, shared by every chat channel.
"""
import json
import logging
import os
import threading
from collections import OrderedDict
from dataclasses import dataclass

import requests

import rag
from channels.base import Channel

log = logging.getLogger("core")

# Optional: an n8n (or any) webhook that receives handoff alerts.
# n8n can then notify staff, start the booking workflow, label the chat, etc.
STAFF_WEBHOOK_URL = os.getenv("STAFF_WEBHOOK_URL", "")

NON_TEXT_REPLY = "Sorry, I can only read text messages for now. Please type your question."


@dataclass
class IncomingMessage:
    channel: str            # "facebook", "whatsapp", "viber", ...
    user_id: str            # the platform's ID for this guest
    text: str               # empty string for photos, stickers, voice notes, etc.
    message_id: str | None = None
    user_name: str | None = None

    @property
    def thread_id(self) -> str:
        """One conversation per guest per channel."""
        return f"{self.channel}:{self.user_id}"


# Platforms sometimes deliver the same message twice; remember recent IDs.
_seen: OrderedDict[str, bool] = OrderedDict()
_seen_lock = threading.Lock()


def _already_seen(key: str) -> bool:
    with _seen_lock:
        if key in _seen:
            return True
        _seen[key] = True
        if len(_seen) > 5000:
            _seen.popitem(last=False)
        return False


def notify_staff(msg: IncomingMessage, question: str, answer: str,
                 event: str = "handoff", reservation: dict | None = None) -> None:
    """Alert staff: event "handoff" (needs a person) or "reservation" (the bot booked a room)."""
    log.info("Staff alert (%s): %s", event, msg.thread_id)
    if not STAFF_WEBHOOK_URL:
        return
    payload = {
        "event": event,
        "channel": msg.channel,
        "user_id": msg.user_id,
        "user_name": msg.user_name,
        "question": question,
        "bot_answer": answer,
        "reservation": reservation,
    }
    try:
        requests.post(
            STAFF_WEBHOOK_URL,
            data=json.dumps(payload, default=str),
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
    except requests.RequestException:
        log.exception("Could not reach STAFF_WEBHOOK_URL")


def handle_incoming(msg: IncomingMessage, channel: Channel) -> None:
    """Answer one guest message on any channel. Called in the background."""
    if msg.message_id and _already_seen(f"{msg.channel}:{msg.message_id}"):
        return

    text = msg.text.strip()
    if not text:
        channel.reply(msg.user_id, NON_TEXT_REPLY)
        return

    channel.send_typing(msg.user_id)
    try:
        result = rag.answer(text, thread_id=msg.thread_id)
    except Exception:
        log.exception("Failed to answer %s", msg.thread_id)
        channel.reply(msg.user_id, rag.ERROR_REPLY)
        return

    channel.reply(msg.user_id, result["answer"])
    if result.get("reservation"):
        notify_staff(msg, text, result["answer"], event="reservation", reservation=result["reservation"])
    if result["handoff"]:
        notify_staff(msg, text, result["answer"])
