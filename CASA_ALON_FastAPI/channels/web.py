"""
Website chat channel.

Unlike Facebook or WhatsApp, the website calls US directly and waits for the answer,
so there is no webhook and no send_text: the reply goes back in the HTTP response.

  POST /web/chat        {session_id, message} -> {answer, handoff}   (called by the widget)
  GET  /widget/chat.js  the chat widget the hotel website loads with one <script> tag
  GET  /widget/demo     a test page with the widget on it

This endpoint is PUBLIC (a browser can't keep an API key secret), so it is protected by
allowed origins, message length limits, and rate limits instead.
"""
import os
import re
import threading
import time
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

import rag
from core import IncomingMessage, notify_staff
from .base import Channel

ENABLED = os.getenv("WEB_ENABLED", "false").lower() == "true"
ALLOWED_ORIGINS = [o.strip().rstrip("/") for o in os.getenv("WEB_ALLOWED_ORIGINS", "").split(",") if o.strip()]
TRUST_PROXY = os.getenv("WEB_TRUST_PROXY", "false").lower() == "true"
PER_SESSION_PER_MINUTE = int(os.getenv("WEB_RATE_PER_MINUTE", "8"))
PER_IP_PER_MINUTE = int(os.getenv("WEB_RATE_PER_IP_PER_MINUTE", "30"))  # guests may share hotel/office Wi-Fi
PER_IP_PER_DAY = int(os.getenv("WEB_RATE_PER_IP_PER_DAY", "300"))
MAX_MESSAGE_CHARS = 1000

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9-]{8,64}$")


class WebChannel(Channel):
    name = "web"

    def is_configured(self) -> bool:
        return ENABLED

    def send_text(self, user_id: str, text: str) -> None:
        # Not used: website replies are returned directly in the /web/chat response.
        pass


channel = WebChannel()
router = APIRouter(tags=["Website chat"])


# ------------------------------------------------------------------
# Rate limiting (in server memory; fine for a single server instance)
# ------------------------------------------------------------------
class RateLimiter:
    def __init__(self):
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, window_seconds: int) -> bool:
        now = time.time()
        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] > window_seconds:
                hits.popleft()
            if len(hits) >= limit:
                return False
            hits.append(now)
            if len(self._hits) > 20000:  # drop idle keys now and then
                for k in [k for k, v in self._hits.items() if not v]:
                    del self._hits[k]
            return True


limiter = RateLimiter()


def _client_ip(request: Request) -> str:
    if TRUST_PROXY:
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _origin_allowed(request: Request) -> bool:
    origin = request.headers.get("Origin")
    if not origin:
        return True  # non-browser clients; still rate limited
    if origin.rstrip("/") in ALLOWED_ORIGINS:
        return True
    # the demo page served by this same server
    return urlparse(origin).netloc == request.headers.get("host")


# ------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------
class ChatRequest(BaseModel):
    session_id: str
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)


class ChatResponse(BaseModel):
    answer: str
    handoff: bool


@router.post("/web/chat", response_model=ChatResponse)
async def web_chat(req: ChatRequest, request: Request, background: BackgroundTasks):
    if not _origin_allowed(request):
        raise HTTPException(status_code=403, detail="This website is not allowed to use the chat")
    if not SESSION_ID_PATTERN.match(req.session_id):
        raise HTTPException(status_code=400, detail="Invalid session")

    ip = _client_ip(request)
    if not (
        limiter.allow(f"s:{req.session_id}", PER_SESSION_PER_MINUTE, 60)
        and limiter.allow(f"ip:{ip}", PER_IP_PER_MINUTE, 60)
        and limiter.allow(f"ipday:{ip}", PER_IP_PER_DAY, 86400)
    ):
        raise HTTPException(status_code=429, detail="Too many messages")

    text = req.message.strip()
    try:
        result = await run_in_threadpool(rag.answer, text, f"web:{req.session_id}")
    except Exception:
        raise HTTPException(status_code=503, detail="The assistant is temporarily unavailable")

    if result["handoff"]:
        msg = IncomingMessage(channel="web", user_id=req.session_id, text=text)
        background.add_task(notify_staff, msg, text, result["answer"])
    return result


@router.get("/widget/chat.js", include_in_schema=False)
def widget_script():
    return FileResponse(
        STATIC_DIR / "chat.js",
        media_type="application/javascript",
        headers={"Cache-Control": "public, max-age=300"},
    )


@router.get("/widget/demo", include_in_schema=False)
def widget_demo():
    return FileResponse(STATIC_DIR / "demo.html", media_type="text/html")
