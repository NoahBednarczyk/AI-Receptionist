"""Webhook server for the voice platforms: python -m receptionist serve

Vapi:   phone number server URL  -> POST /vapi/webhook  (assistant-request, tool-calls, end-of-call-report)
Retell, custom LLM (recommended: our agent runs every turn)
        agent LLM websocket      -> wss://<host>/llm-websocket/<RETELL_WS_SECRET>   (Retell appends /{call_id})
        agent webhook URL        -> POST /retell/webhook   (call_ended)
Retell, platform-hosted model (prompt and tools only)
        phone number inbound URL -> POST /retell/inbound
        custom tools             -> POST /retell/tool/{name}
Owner page                       -> GET /dashboard?token=<shop key>&business=<id> (then a cookie)

Environment:
  PUBLIC_BASE_URL     https URL the platforms reach this server on (e.g. an ngrok URL)
  VAPI_SERVER_TOKEN   shared token; requests must send "Authorization: Bearer <token>" or "X-Vapi-Secret: <token>"
  RETELL_API_KEY      verifies Retell's X-Retell-Signature
  RETELL_WS_SECRET    secret path segment for the live-call websocket, so only Retell can start calls
  DASHBOARD_TOKEN     admin key: opens any shop's dashboard and calendar feed; both are off without it
  TOKEN_SECRET        derives each shop's own dashboard and feed keys (`owner-links`); never shown to anyone
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
import datetime
from datetime import timedelta
from pathlib import Path

import anthropic
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from . import access, calendar_busy, calendar_feed, dashboard, live, outbound, outbound_live, platforms, postcall, prompts, schedule
from . import keepwarm, latency as latency_mod, notify
from .agent import CallSession, prewarm, transcript_text
from .config import all_businesses, business_for_number
from .speech import spoken_phone
from .store import Store
from .tools import ToolContext, run_tool

log = logging.getLogger("receptionist")
pool = ThreadPoolExecutor(max_workers=4)   # background cache warm-ups


def prewarm_targets(cfg: dict) -> list[dict]:
    """The shops to warm at startup: only those that take real calls (`live_businesses` in config.json, or the default
    shop). Every prewarm writes the whole 16,000-token prompt (about 6 cents), and a demo shop nobody dials would pay that
    on every deploy and restart."""
    live = set(cfg.get("live_businesses") or [cfg["default_business"]])
    return [b for b in all_businesses(cfg) if b["id"] in live and b.get("phone_numbers")]


def create_app(cfg: dict, client: anthropic.Anthropic | None = None, warm_cache: bool = True) -> FastAPI:
    app = FastAPI(title="receptionist", docs_url=None, redoc_url=None, openapi_url=None)   # no route map for strangers
    client = client or anthropic.Anthropic()
    store = Store(str(Path(cfg["data_dir"]) / "receptionist.sqlite"))
    for biz in all_businesses(cfg):
        if biz.get("demo_seed"):
            store.seed_demo(biz, schedule.local_now(biz))
    vapi_token = os.environ.get("VAPI_SERVER_TOKEN")
    retell_key = os.environ.get("RETELL_API_KEY")
    ws_secret = os.environ.get("RETELL_WS_SECRET")
    dashboard_token = os.environ.get("DASHBOARD_TOKEN")      # the admin key: opens any shop
    token_secret = os.environ.get("TOKEN_SECRET")            # derives each shop's own dashboard and feed keys; never shown
    for name, value in (("DASHBOARD_TOKEN", dashboard_token), ("TOKEN_SECRET", token_secret)):
        if value and len(value) < 24:
            log.error("%s is shorter than 24 characters and is ignored; make one with `openssl rand -hex 32`", name)
    dashboard_token = dashboard_token if dashboard_token and len(dashboard_token) >= 24 else None
    token_secret = token_secret if token_secret and len(token_secret) >= 24 else None
    unverified_ok = os.environ.get("ALLOW_UNVERIFIED_WEBHOOKS") == "1"   # local development only
    limiter = access.FailureLimit()
    logging.getLogger("uvicorn.access").addFilter(access.redact_access_log)
    if not ws_secret:
        log.warning("RETELL_WS_SECRET is not set: live Retell calls are off")

    refresher = keepwarm.CacheRefresher(cfg, client, prewarm_targets, prewarm)

    def keep_warm() -> None:
        """The API answers the first request after a long idle stretch slowly (20+ s seen), so keep the connection
        fresh with a free model lookup every 3 minutes. The prompt cache itself is written at the start of each
        call, while the greeting plays (start_session); re-writing it all day would cost about $0.45 a day per
        business on Sonnet."""
        once = True
        while True:
            try:
                if once:   # warm the cache at startup too, so the first call after a deploy is quick
                    for biz in prewarm_targets(cfg):
                        prewarm(client, cfg, biz)
                        refresher.mark(biz["id"], datetime.datetime.now(datetime.timezone.utc))
                    once = False
                refresher.tick()   # every live shop, every 50 minutes in its day (live.cache_refresh)
                client.models.retrieve(cfg["models"]["agent"])
            except anthropic.APIError as e:
                log.warning("keep-warm request failed: %s", e)
            stop.wait(180)

    def apply_retention() -> int:
        """Delete calls (transcripts and summaries) older than each shop's retention_days, default 90."""
        n = 0
        for biz in all_businesses(cfg):
            cutoff = schedule.local_now(biz) - timedelta(days=int(biz.get("retention_days", 90)))
            n += store.purge_calls_before(biz["id"], cutoff)
        return n

    def retention_loop() -> None:
        while not stop.wait(6 * 3600):   # twice a day is plenty
            try:
                if n := apply_retention():
                    log.info("retention: deleted %d old calls", n)
            except Exception:
                log.exception("retention sweep failed")

    app.state.apply_retention = apply_retention
    stop = threading.Event()
    threading.Thread(target=retention_loop, daemon=True, name="retention").start()
    if warm_cache:
        threading.Thread(target=keep_warm, daemon=True, name="keep-warm").start()
    if not vapi_token:
        log.warning("VAPI_SERVER_TOKEN is not set: /vapi/webhook is %s", "open (ALLOW_UNVERIFIED_WEBHOOKS=1)" if unverified_ok else "closed")
    if not retell_key:
        log.warning("RETELL_API_KEY is not set: Retell routes are %s", "open (ALLOW_UNVERIFIED_WEBHOOKS=1)" if unverified_ok else "closed")

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

    notifier = notify.Notifier(
        store, email=notify.email_sender_from_env(), admin_email=os.environ.get("ADMIN_EMAIL"),
        base_url=os.environ.get("PUBLIC_BASE_URL"),
        shop_key=(lambda b: access.shop_key(token_secret, b, "dashboard")) if token_secret else None)
    app.state.notifier = notifier

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
            notifier.call_finished(biz, call_id, None, reason)   # never raises
            return
        store.save_summary(call_id, summary)
        notifier.call_finished(biz, call_id, summary, reason)   # never raises
        if biz.get("texting_enabled"):   # otherwise the owner reads it on the dashboard
            store.queue_sms(biz["id"], call_id, biz["owner_sms"],
                            f"Call from {spoken_phone(summary.get('callback_number')) if summary.get('callback_number') else 'unknown'}: "
                            f"{summary['summary']}", schedule.local_now(biz))

    @app.get("/health")
    def health():
        return {"ok": True}   # no shop names: this one is open to anyone

    def _shop(business: str | None, admin: bool) -> dict | None:
        """The shop named in the request. Only the admin key may fall back to the default shop."""
        wanted = business or (cfg["default_business"] if admin else None)
        return next((b for b in all_businesses(cfg) if b["id"] == wanted), None)

    def _who(request: Request) -> str:
        fwd = request.headers.get("x-forwarded-for")
        return fwd.split(",")[-1].strip() if fwd else (request.client.host if request.client else "?")

    def _checked(request: Request, key: str, business: str | None, purpose: str) -> dict:
        """The shop if `key` opens `purpose` for it; otherwise the one 404 every failure gets."""
        who = _who(request)
        if limiter.blocked(who):
            raise HTTPException(404)
        is_admin = bool(dashboard_token) and secrets.compare_digest(key or "", dashboard_token)
        biz = _shop(business, is_admin)
        if not access.allowed(key, purpose, biz, dashboard_token, token_secret):
            limiter.record(who)
            raise HTTPException(404)
        return biz

    MAX_BODY = 1_000_000

    @app.middleware("http")
    async def keep_keys_private(request: Request, call_next):
        if int(request.headers.get("content-length") or 0) > MAX_BODY:
            return Response(status_code=413)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        if request.url.path.startswith("/dashboard") or request.url.path == "/calendar.ics":
            for k, v in access.SAFE_HEADERS.items():
                response.headers[k] = v
        if request.url.path == "/dashboard":
            response.headers["Content-Security-Policy"] = dashboard.CSP
        return response

    @app.put("/admin/clients")
    async def upload_client(request: Request):
        """Put a real shop's file on the server's disk (it is kept out of git). Admin key in the Authorization header."""
        who = _who(request)
        sent = request.headers.get("authorization", "").removeprefix("Bearer ")
        if limiter.blocked(who) or not (dashboard_token and secrets.compare_digest(sent, dashboard_token)):
            limiter.record(who)
            raise HTTPException(404)
        from . import onboarding
        try:
            path = onboarding.accept_upload(cfg, await request.json())
        except (onboarding.IntakeError, ValueError) as e:
            raise HTTPException(422, str(e))
        return {"ok": True, "saved": path.name}

    @app.get("/admin/outbound")
    async def outbound_state(request: Request):
        """The discovery caller's results, bookings and do-not-call list (admin key). The Mac pulls this to update the dashboard."""
        who = _who(request)
        sent = request.headers.get("authorization", "").removeprefix("Bearer ")
        if limiter.blocked(who) or not (dashboard_token and secrets.compare_digest(sent, dashboard_token)):
            limiter.record(who)
            raise HTTPException(404)
        return outbound_live.State(Path(cfg["data_dir"]) / "outbound.json").data

    @app.put("/admin/outbound/slots")
    async def outbound_slots(request: Request):
        """The manager agent posts the open meeting times: {"slots": ["2026-11-03T16:30:00-08:00", ...]} (admin key).
        They replace the default windows for 36 hours, so the caller offers only times that are really free."""
        who = _who(request)
        sent = request.headers.get("authorization", "").removeprefix("Bearer ")
        if limiter.blocked(who) or not (dashboard_token and secrets.compare_digest(sent, dashboard_token)):
            limiter.record(who)
            raise HTTPException(404)
        try:
            slots = [datetime.datetime.fromisoformat(t).isoformat() for t in (await request.json())["slots"]]
        except (KeyError, TypeError, ValueError):
            raise HTTPException(422, "send {\"slots\": [ISO times with a UTC offset]}")
        outbound_live.State(Path(cfg["data_dir"]) / "outbound.json").set_slots(slots, datetime.datetime.now(datetime.timezone.utc))
        return {"ok": True, "slots": len(slots)}

    block_runner = outbound_live.BlockRunner(cfg, Path(cfg["data_dir"]) / "outbound.json",
                                             outbound_live.live_call_fn(retell_key) if retell_key else None,
                                             warm_fn=lambda: outbound_live.warm(client, cfg, datetime.datetime.now(datetime.timezone.utc)))

    def _admin_only(request: Request) -> None:
        who = _who(request)
        sent = request.headers.get("authorization", "").removeprefix("Bearer ")
        if limiter.blocked(who) or not (dashboard_token and secrets.compare_digest(sent, dashboard_token)):
            limiter.record(who)
            raise HTTPException(404)

    @app.get("/admin/notifications")
    def admin_notifications(request: Request):
        """Recent emails and texts the server tried to send, with failures (admin key)."""
        _admin_only(request)
        return {"sent_today": notifier.sent_today(), "failed_today": notifier.failures_today(), "recent": notifier.recent()}

    @app.get("/admin/latency")
    async def latency(request: Request, last: int = 10):
        """The latency-report table as JSON (admin key), for when RETELL_API_KEY is only on this server. Read-only."""
        _admin_only(request)
        if not retell_key:
            raise HTTPException(503, "RETELL_API_KEY is not set on this server.")
        try:
            return {"rows": latency_mod.report(max(1, min(last, 50)), cfg, {"RETELL_API_KEY": retell_key})}
        except RuntimeError as e:
            raise HTTPException(502, str(e))

    @app.put("/admin/outbound/agent")
    async def outbound_agent_put(request: Request):
        """Record which Retell agent and from-number place_call() dials with (admin key). The CLI's outbound-agent
        --create writes this file on whatever machine runs it; this lets that same record reach the server's own
        disk without shell access to it, for a Retell agent/number that already exists."""
        _admin_only(request)
        try:
            body = await request.json()
            agent_id = body["agent_id"]
            assert isinstance(agent_id, str) and agent_id
        except (KeyError, TypeError, AssertionError):
            raise HTTPException(422, "send {\"agent_id\": \"...\", \"number\": \"+1...\" (number optional)}")
        if body.get("number") and not outbound_live.normalize_phone(body["number"]):
            raise HTTPException(422, f"{body['number']!r} is not a full phone number.")
        rec = outbound_live.save_agent_record(cfg, agent_id, body.get("number"))
        return {"ok": True, **rec}

    @app.get("/admin/outbound/agent")
    async def outbound_agent_get(request: Request):
        """What place_call() currently has on file (admin key); null if nothing has been registered yet."""
        _admin_only(request)
        return {"agent": outbound_live.load_agent_record(cfg)}

    @app.put("/admin/outbound/leads")
    async def outbound_leads_put(request: Request):
        """Replace the discovery caller's whole lead list (admin key). Lives on the server's disk, never in git;
        written by hand for now, and later by the lead-building skill."""
        _admin_only(request)
        try:
            leads = (await request.json())["leads"]
            assert isinstance(leads, list)
        except (KeyError, TypeError, AssertionError):
            raise HTTPException(422, "send {\"leads\": [...]}")
        outbound_live.save_leads(cfg, leads)
        return {"ok": True, "leads": len(leads)}

    @app.get("/admin/outbound/leads")
    async def outbound_leads_get(request: Request):
        """The current lead list (admin key), so Noah can check what's loaded without shell access to the disk."""
        _admin_only(request)
        return {"leads": outbound_live.load_leads(cfg)}

    @app.post("/admin/outbound/call-next")
    async def outbound_call_next(request: Request):
        """Places one real call to the next eligible lead, or dials nothing and says why. Fired by the scheduled
        cron trigger each weekday morning, or by hand (admin key) to test it."""
        _admin_only(request)
        if not retell_key:
            raise HTTPException(503, "RETELL_API_KEY is not set on this server; no outbound call can be placed.")
        if outbound.settings(cfg)["block_runner"]["enabled"]:
            if not block_runner.start():
                return JSONResponse({"started": False, "reason": "a block is already running"}, status_code=409)
            threading.Thread(target=block_runner.run, daemon=True, name="outbound-block").start()
            return JSONResponse({"started": True, "block": block_runner.status}, status_code=202)
        leads = outbound_live.load_leads(cfg)
        state = outbound_live.State(Path(cfg["data_dir"]) / "outbound.json")
        now = datetime.datetime.now(datetime.timezone.utc)
        return outbound_live.call_next(cfg, leads, state, outbound_live.live_call_fn(retell_key), now,
                                       warm_fn=lambda: outbound_live.warm(client, cfg, now))

    @app.get("/admin/outbound/block")
    async def outbound_block_status(request: Request):
        _admin_only(request)
        return {"enabled": outbound.settings(cfg)["block_runner"]["enabled"], **block_runner.status}

    @app.post("/admin/outbound/block/stop")
    async def outbound_block_stop(request: Request):
        """Ends the running block after its current call; nothing more is dialed."""
        _admin_only(request)
        block_runner.stop()
        return {"stopping": block_runner.status.get("running", False)}

    @app.get("/calendar.ics")
    def calendar_feed_route(request: Request, token: str = "", business: str | None = None):
        biz = _checked(request, token, business, "calendar")
        now = schedule.local_now(biz)
        since = (now - timedelta(days=calendar_feed.KEEP_CANCELLED_DAYS)).date().isoformat()
        return Response(calendar_feed.render(biz, store.appointments_since(biz["id"], since), now),
                        media_type="text/calendar; charset=utf-8")

    @app.get("/dashboard", response_class=HTMLResponse)
    def owner_dashboard(request: Request, token: str = "", business: str | None = None):
        if token:   # trade the key in the link for a cookie, then drop it from the address bar and history
            biz = _checked(request, token, business, "dashboard")
            secure = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
            response = RedirectResponse(f"/dashboard?business={biz['id']}", status_code=303)
            response.set_cookie(access.COOKIE, token, httponly=True, secure=secure, samesite="lax", path="/dashboard",
                                max_age=60 * 60 * 24 * 90)
            return response
        biz = _checked(request, request.cookies.get(access.COOKIE, ""), business, "dashboard")
        _, problems = calendar_busy.booked_counts(biz, store, schedule.local_now(biz))
        return dashboard.render(biz, store, problems=problems)

    def _owner_post(request: Request, business: str) -> dict:
        """A form post from the owner's page: the cookie must open this shop, and the request must come from our own page."""
        origin = request.headers.get("origin")
        if origin and urllib.parse.urlsplit(origin).netloc != request.headers.get("host", request.url.netloc):
            raise HTTPException(404)
        return _checked(request, request.cookies.get(access.COOKIE, ""), business, "dashboard")

    @app.post("/dashboard/block")
    async def add_block(request: Request):
        form = urllib.parse.parse_qs((await request.body()).decode(), keep_blank_values=True)
        one = lambda k: (form.get(k) or [""])[0].strip()
        biz = _owner_post(request, one("business"))
        tz = schedule.tz(biz)
        try:
            day = datetime.date.fromisoformat(one("date"))
            start = datetime.datetime.combine(day, datetime.time.fromisoformat(one("start")), tzinfo=tz)
            end = datetime.datetime.combine(day, datetime.time.fromisoformat(one("end")), tzinfo=tz)
        except ValueError:
            raise HTTPException(422, "Give a date and a start and end time.")
        if end <= start:
            raise HTTPException(422, "The end must be after the start.")
        store.add_block(biz["id"], start, end, one("note")[:80], schedule.local_now(biz))
        return RedirectResponse(f"/dashboard?business={biz['id']}", status_code=303)

    @app.post("/dashboard/block/delete")
    async def delete_block(request: Request):
        form = urllib.parse.parse_qs((await request.body()).decode())
        biz = _owner_post(request, (form.get("business") or [""])[0])
        try:
            store.delete_block(biz["id"], int((form.get("id") or [""])[0]))
        except ValueError:
            raise HTTPException(422)
        return RedirectResponse(f"/dashboard?business={biz['id']}", status_code=303)

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

    # ---- Retell, outbound discovery caller (off unless data/retell_outbound.json exists) -------------
    @app.websocket("/llm-websocket-out/{secret}/{retell_call_id}")
    async def retell_llm_out(websocket: WebSocket, secret: str, retell_call_id: str):
        if not ws_secret or not secrets.compare_digest(secret, ws_secret):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        out_state = outbound_live.State(Path(cfg["data_dir"]) / "outbound.json")

        def start_session(call: dict):
            lead = (call.get("metadata") or {}).get("lead")
            if not lead or cfg.get("outbound", {}).get("mode", "dry-run") == "dry-run":
                log.error("outbound socket opened without a lead or while outbound.mode is dry-run; hanging up")
                return None
            return outbound_live.OutboundSession(client, cfg, lead, out_state, store, datetime.datetime.now(datetime.timezone.utc),
                                                 call_id=f"retell-{call.get('call_id') or retell_call_id}", warm_cache=True)

        def finish(session) -> None:
            now = datetime.datetime.now(datetime.timezone.utc)
            session.finish(now, (now - session.started).total_seconds() / 60, session.ctx.end_reason)

        try:
            await live.run_retell_call(websocket.receive_json, websocket.send_json, start_session, finish)
        except WebSocketDisconnect:
            pass

    # ---- Vapi -------------------------------------------------------------
    @app.post("/vapi/webhook")
    async def vapi_webhook(request: Request, background: BackgroundTasks):
        if not vapi_token and not unverified_ok:
            raise HTTPException(401, "bad token")
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
        if not retell_key and not unverified_ok:
            raise HTTPException(401, "bad signature")
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
        if body.get("event") == "call_ended" and call.get("call_id"):
            # A dial that nobody answered never opened our socket: leave a result row so the queue sees it.
            try:
                outbound_live.webhook_result(outbound_live.State(Path(cfg["data_dir"]) / "outbound.json"), cfg, call,
                                             datetime.datetime.now(datetime.timezone.utc))
            except Exception:
                log.exception("could not record the unanswered outbound call %s", call.get("call_id"))
        stored = store.get_call(call_id)
        if body.get("event") == "call_ended" and biz and not (stored and stored.get("transcript")):
            # Live custom-LLM calls already stored our own, richer transcript when the socket closed.
            background.add_task(finish_call, biz, call_id, platforms.retell_transcript(call),
                                call.get("disconnection_reason"))
        return {}

    return app
