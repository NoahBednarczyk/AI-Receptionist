"""Webhook server for the voice platforms: python -m receptionist serve

Vapi:   phone number server URL  -> POST /vapi/webhook  (assistant-request, tool-calls, end-of-call-report)
Retell, custom LLM (recommended: our agent runs every turn)
        agent LLM websocket      -> wss://<host>/llm-websocket/<RETELL_WS_SECRET>   (Retell appends /{call_id})
        agent webhook URL        -> POST /retell/webhook   (call_ended)
Retell, platform-hosted model (prompt and tools only)
        phone number inbound URL -> POST /retell/inbound
        custom tools             -> POST /retell/tool/{name}
Owner page                       -> GET /dashboard?token=<DASHBOARD_TOKEN>

Environment:
  PUBLIC_BASE_URL     https URL the platforms reach this server on (e.g. an ngrok URL)
  VAPI_SERVER_TOKEN   shared token; requests must send "Authorization: Bearer <token>" or "X-Vapi-Secret: <token>"
  RETELL_API_KEY      verifies Retell's X-Retell-Signature
  RETELL_WS_SECRET    secret path segment for the live-call websocket, so only Retell can start calls
  DASHBOARD_TOKEN     password for the owner page; the page is off without it
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import anthropic
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response

from . import calendar_feed, dashboard, live, platforms, postcall, prompts, schedule
from .agent import CallSession, prewarm, transcript_text
from .config import all_businesses, business_for_number
from .speech import spoken_phone
from .store import Store
from .tools import ToolContext, run_tool

log = logging.getLogger("receptionist")
pool = ThreadPoolExecutor(max_workers=4)   # background cache warm-ups


def create_app(cfg: dict, client: anthropic.Anthropic | None = None, warm_cache: bool = True) -> FastAPI:
    app = FastAPI(title="receptionist")
    client = client or anthropic.Anthropic()
    store = Store(str(Path(cfg["data_dir"]) / "receptionist.sqlite"))
    for biz in all_businesses(cfg):
        if biz.get("demo_seed"):
            store.seed_demo(biz, schedule.local_now(biz))
    vapi_token = os.environ.get("VAPI_SERVER_TOKEN")
    retell_key = os.environ.get("RETELL_API_KEY")
    ws_secret = os.environ.get("RETELL_WS_SECRET")
    dashboard_token = os.environ.get("DASHBOARD_TOKEN")
    if not ws_secret:
        log.warning("RETELL_WS_SECRET is not set: live Retell calls are off")

    def keep_warm() -> None:
        """The API answers the first request after a long idle stretch slowly (20+ s seen), so keep the connection
        fresh with a free model lookup every 3 minutes. The prompt cache itself is written at the start of each
        call, while the greeting plays (start_session); re-writing it all day would cost about $0.45 a day per
        business on Sonnet."""
        once = True
        while True:
            try:
                if once:   # warm the cache at startup too, so the first test call is quick
                    for biz in all_businesses(cfg):
                        if biz.get("phone_numbers"):
                            prewarm(client, cfg, biz)
                    once = False
                client.models.retrieve(cfg["models"]["agent"])
            except anthropic.APIError as e:
                log.warning("keep-warm request failed: %s", e)
            stop.wait(180)

    stop = threading.Event()
    if warm_cache:
        threading.Thread(target=keep_warm, daemon=True, name="keep-warm").start()
    if not vapi_token:
        log.warning("VAPI_SERVER_TOKEN is not set: /vapi/webhook accepts unauthenticated requests")
    if not retell_key:
        log.warning("RETELL_API_KEY is not set: Retell signatures are not checked")

    def base_url(request: Request) -> str:
        if os.environ.get("PUBLIC_BASE_URL"):
            return os.environ["PUBLIC_BASE_URL"].rstrip("/")
        proto = request.headers.get("x-forwarded-proto", request.url.scheme)
        return f"{proto}://{request.headers.get('host', request.url.netloc)}"

    def call_state_for(biz: dict, caller: str | None, dialed: str | None = None) -> str:
        now = schedule.local_now(biz)
        customer = store.get_customer(biz["id"], caller)
        upcoming = store.upcoming_appointments(biz["id"], caller, now) if customer else []
        return prompts.call_state(biz, now, caller, customer, upcoming, dialed)

    def finish_call(biz: dict, call_id: str, transcript: list[dict], reason: str | None,
                    call_state: str | None = None) -> None:
        """Runs after the response is sent: store the transcript, then summarize and text the owner."""
        store.end_call(call_id, transcript, reason, schedule.local_now(biz))
        if len(transcript) < 2:
            return
        try:
            summary = postcall.summarize(client, cfg, biz, transcript_text(transcript), call_state)
        except anthropic.APIError as e:
            log.error("post-call summary failed for %s: %s", call_id, e)
            return
        store.save_summary(call_id, summary)
        if biz.get("texting_enabled"):   # otherwise the owner reads it on the dashboard
            store.queue_sms(biz["id"], call_id, biz["owner_sms"],
                            f"Call from {spoken_phone(summary.get('callback_number')) if summary.get('callback_number') else 'unknown'}: "
                            f"{summary['summary']}", schedule.local_now(biz))

    @app.get("/health")
    def health():
        return {"ok": True, "businesses": [b["id"] for b in all_businesses(cfg)]}

    @app.get("/calendar.ics")
    def calendar_feed_route(token: str = "", business: str | None = None):
        biz = next((b for b in all_businesses(cfg) if b["id"] == (business or cfg["default_business"])), None)
        if not dashboard_token or not biz or not secrets.compare_digest(token, calendar_feed.feed_token(dashboard_token, biz["id"])):
            raise HTTPException(404)
        now = schedule.local_now(biz)
        since = (now - timedelta(days=calendar_feed.KEEP_CANCELLED_DAYS)).date().isoformat()
        return Response(calendar_feed.render(biz, store.appointments_since(biz["id"], since), now),
                        media_type="text/calendar; charset=utf-8")

    @app.get("/dashboard", response_class=HTMLResponse)
    def owner_dashboard(token: str = "", business: str | None = None):
        if not dashboard_token or not secrets.compare_digest(token, dashboard_token):
            raise HTTPException(404)
        biz = next((b for b in all_businesses(cfg) if b["id"] == (business or cfg["default_business"])), None)
        if not biz:
            raise HTTPException(404)
        return dashboard.render(biz, store)

    # ---- Retell, custom LLM: our agent answers every turn of a live call ---------------
    @app.websocket("/llm-websocket/{secret}/{retell_call_id}")
    async def retell_llm(websocket: WebSocket, secret: str, retell_call_id: str):
        if not ws_secret or not secrets.compare_digest(secret, ws_secret):
            await websocket.close(code=1008)
            return
        await websocket.accept()

        def start_session(call: dict) -> CallSession | None:
            biz = business_for_number(cfg, call.get("to_number"))
            if not biz:
                log.error("live call to unknown number %s; add it to a business's phone_numbers", call.get("to_number"))
                return None
            if warm_cache:   # the greeting takes a few seconds to say: warm the prompt cache meanwhile
                pool.submit(prewarm, client, cfg, biz)
            return CallSession(client, cfg, biz, store, schedule.local_now(biz), caller=call.get("from_number"),
                               dialed=call.get("to_number"), real_transfers=True,
                               call_id=f"retell-{call.get('call_id') or retell_call_id}", platform="retell")

        def finish(session: CallSession) -> None:
            finish_call(session.biz, session.call_id, session.transcript, session.ctx.end_reason or "caller hung up",
                        session.system[1]["text"])

        try:
            await live.run_retell_call(websocket.receive_json, websocket.send_json, start_session, finish)
        except WebSocketDisconnect:
            pass

    # ---- Vapi -------------------------------------------------------------
    @app.post("/vapi/webhook")
    async def vapi_webhook(request: Request, background: BackgroundTasks):
        if vapi_token:
            auth = request.headers.get("authorization", "")
            if auth != f"Bearer {vapi_token}" and request.headers.get("x-vapi-secret") != vapi_token:
                raise HTTPException(401, "bad token")
        msg = (await request.json()).get("message") or {}
        kind = msg.get("type")
        call_id, dialed, caller = platforms.vapi_call_info(msg)
        call_id = f"vapi-{call_id}"
        biz = business_for_number(cfg, dialed)

        if kind == "assistant-request":
            if not biz:
                log.error("assistant-request for unknown number %s; add it to a business's phone_numbers", dialed)
                return {"error": "Sorry, this number isn't set up yet."}
            store.start_call(call_id, biz["id"], "vapi", caller, schedule.local_now(biz))
            return {"assistant": platforms.vapi_assistant(cfg, biz, base_url(request),
                                                          call_state_for(biz, caller, dialed), vapi_token)}
        if kind == "tool-calls":
            calls = platforms.vapi_tool_calls(msg)
            if not biz:
                return {"results": [{"toolCallId": tc_id, "error": "This number isn't set up."} for tc_id, _, _ in calls]}
            ctx = ToolContext(biz, store, schedule.local_now(biz), caller, call_id)
            results = []
            for tc_id, name, args in calls:
                result, is_error = run_tool(ctx, name, args)
                results.append({"name": name, "toolCallId": tc_id,
                                ("error" if is_error else "result"): json.dumps(result, ensure_ascii=False)})
            return {"results": results}
        if kind == "end-of-call-report" and biz:
            background.add_task(finish_call, biz, call_id, platforms.vapi_transcript(msg), msg.get("endedReason"))
        return {}

    # ---- Retell -----------------------------------------------------------
    async def retell_body(request: Request) -> dict:
        raw = await request.body()
        if retell_key and not platforms.retell_signature_ok(raw, retell_key, request.headers.get("x-retell-signature")):
            raise HTTPException(401, "bad signature")
        return json.loads(raw or b"{}")

    @app.post("/retell/inbound")
    async def retell_inbound(request: Request):
        inbound = (await retell_body(request)).get("call_inbound") or {}
        biz = business_for_number(cfg, inbound.get("to_number"))
        if not biz:
            log.error("inbound call to unknown number %s; add it to a business's phone_numbers", inbound.get("to_number"))
            return {"call_inbound": {"reject": True}}
        caller = inbound.get("from_number")
        store.start_call(f"retell-{inbound.get('call_id')}", biz["id"], "retell", caller, schedule.local_now(biz))
        return {"call_inbound": {"dynamic_variables": {"call_state": call_state_for(biz, caller,
                                                                                    inbound.get("to_number"))}}}

    @app.post("/retell/tool/{name}")
    async def retell_tool(name: str, request: Request):
        body = await retell_body(request)
        call = body.get("call") or {}
        biz = business_for_number(cfg, call.get("to_number"))
        if not biz:
            return JSONResponse({"error": "This number isn't set up."})
        ctx = ToolContext(biz, store, schedule.local_now(biz), call.get("from_number"), f"retell-{call.get('call_id')}")
        result, _ = run_tool(ctx, name, body.get("args") or {})
        return JSONResponse(result)

    @app.post("/retell/webhook")
    async def retell_webhook(request: Request, background: BackgroundTasks):
        body = await retell_body(request)
        call = body.get("call") or {}
        biz = business_for_number(cfg, call.get("to_number"))
        call_id = f"retell-{call.get('call_id')}"
        log.info("Retell %s for %s: status %s, reason %s", body.get("event"), call.get("call_id"),
                 call.get("call_status"), call.get("disconnection_reason"))
        stored = store.get_call(call_id)
        if body.get("event") == "call_ended" and biz and not (stored and stored.get("transcript")):
            # Live custom-LLM calls already stored our own, richer transcript when the socket closed.
            background.add_task(finish_call, biz, call_id, platforms.retell_transcript(call),
                                call.get("disconnection_reason"))
        return {}

    return app
