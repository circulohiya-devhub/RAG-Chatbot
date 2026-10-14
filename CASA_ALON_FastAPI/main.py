"""
Casa Alon Hotel bot server

  /webhooks/facebook   Facebook Messenger     (on when FB_* settings are in .env)
  /webhooks/whatsapp   WhatsApp               (on when WHATSAPP_* settings are in .env)
  /webhooks/viber      Viber                  (on when VIBER_* settings are in .env)
  /web/chat            Website chat widget    (on when WEB_ENABLED=true); widget at /widget/chat.js
  POST /ask            Ask the bot directly: for n8n (Gmail and other channels n8n handles) and testing
  POST /refresh        Force-reload the Google Doc now
  GET  /health         Health check and list of enabled channels

/ask and /refresh need the X-API-Key header.
Run locally:  uvicorn main:app --reload --port 8000
"""
from dotenv import load_dotenv

load_dotenv()  # must run before importing modules that read the environment

import hmac
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("server")

import rag  # noqa: E402
from channels import ALL_CHANNELS, web  # noqa: E402

INTERNAL_API_KEY = os.getenv("INTERNAL_API_KEY", "")


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Loading knowledge base from Google Doc...")
    await run_in_threadpool(rag.kb.refresh, True)
    yield


app = FastAPI(title="Casa Alon Hotel Bot", lifespan=lifespan)

ENABLED_CHANNELS: list[str] = []
for module in ALL_CHANNELS:
    if module.channel.is_configured():
        app.include_router(module.router)
        ENABLED_CHANNELS.append(module.channel.name)
        log.info("Channel enabled: %s", module.channel.name)
    else:
        log.info("Channel skipped (settings missing): %s", module.channel.name)

# Let the hotel website (a different domain) call /web/chat from the browser.
if web.channel.is_configured() and web.ALLOWED_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=web.ALLOWED_ORIGINS,
        allow_methods=["POST"],
        allow_headers=["Content-Type"],
    )


def _check_api_key(x_api_key: str | None):
    if not INTERNAL_API_KEY or not hmac.compare_digest(x_api_key or "", INTERNAL_API_KEY):
        raise HTTPException(status_code=401, detail="Invalid API key")


class AskRequest(BaseModel):
    question: str
    user_id: str           # e.g. the guest's email address for Gmail
    channel: str = "api"   # e.g. "email"; keeps conversations separate per channel


class AskResponse(BaseModel):
    answer: str
    handoff: bool          # True: n8n should route to staff or the booking workflow


@app.post("/ask", response_model=AskResponse)
def ask(req: AskRequest, x_api_key: str | None = Header(default=None)):
    _check_api_key(x_api_key)
    try:
        return rag.answer(req.question, thread_id=f"{req.channel}:{req.user_id}")
    except Exception:
        log.exception("Failed to answer /ask request")
        raise HTTPException(status_code=503, detail="The assistant is temporarily unavailable")


@app.post("/refresh")
def refresh(x_api_key: str | None = Header(default=None)):
    _check_api_key(x_api_key)
    reloaded = rag.kb.refresh(force=True)
    return {"reloaded": reloaded, "note": "Knowledge base updated" if reloaded else "No changes found"}


@app.get("/health")
def health():
    return {
        "status": "ok",
        "knowledge_base_loaded": rag.kb.store is not None,
        "channels": ENABLED_CHANNELS,
    }
