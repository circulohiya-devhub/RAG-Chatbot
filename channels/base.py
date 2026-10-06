"""
Base class for chat channels.

Every channel (Facebook, WhatsApp, Viber, ...) does the same three jobs:
  1. Receive a webhook and turn it into an IncomingMessage   (in the channel's router)
  2. Send a text reply back to the guest                     (send_text)
  3. Say whether its settings are filled in                  (is_configured)
Everything else (dedupe, the agent, staff handoff) is shared in core.py.
"""
import hashlib
import hmac
from abc import ABC, abstractmethod


class Channel(ABC):
    name: str = "base"
    max_chars: int = 2000  # longest single message the platform accepts

    @abstractmethod
    def is_configured(self) -> bool:
        """True when all required settings for this channel are present."""

    @abstractmethod
    def send_text(self, user_id: str, text: str) -> None:
        """Send one message (already within max_chars) to the guest."""

    def send_typing(self, user_id: str) -> None:
        """Show a typing indicator, if the platform supports it."""

    def reply(self, user_id: str, text: str) -> None:
        for chunk in split_message(text, self.max_chars):
            self.send_text(user_id, chunk)


def split_message(text: str, size: int):
    """Split long replies at line breaks so each part fits the platform limit."""
    while len(text) > size:
        cut = text.rfind("\n", 0, size)
        if cut <= 0:
            cut = size
        yield text[:cut]
        text = text[cut:].lstrip()
    if text:
        yield text


def verify_hmac_sha256(secret: str, body: bytes, provided: str, prefix: str = "") -> bool:
    """Check a webhook signature. Meta uses prefix 'sha256=', Viber uses no prefix."""
    if not secret or not provided:
        return False
    expected = prefix + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, provided)
