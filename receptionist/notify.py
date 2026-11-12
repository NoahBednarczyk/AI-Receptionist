"""Telling the owner about calls: an email per call (W2.1), texts behind `texting_enabled` (W2.2), a daily digest
(W2.3) and an alarm to the admin for calls that ended with nothing to show (W2.4).

Nothing here may break a call: every public entry point catches what goes wrong, records it in the `notify_log` table
(shown at /admin/notifications) and returns. Senders are small objects with `send(to, subject, body)`, so a test hands
in a fake and no real account is ever touched.

Settings (environment, never the repo):
  SMTP_HOST, SMTP_USER, SMTP_PASSWORD   plain SMTP, e.g. smtp.gmail.com with a Gmail app password (SMTP_PORT, SMTP_FROM optional)
  SMTP_DAILY_CAP                        the provider's daily sending limit, default 500 (Gmail); a warning is logged at 50%
  ADMIN_EMAIL                           where the missed-outcome alarm goes (Noah, not the shop)
  TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_MESSAGING_SERVICE_SID   only used when a shop has texting_enabled
Per shop (business JSON): "notify": {"email": ["owner@example.com"], "max_per_day": 40, "digest": true}
"""
from __future__ import annotations

import base64
import json
import logging
import os
import smtplib
import threading
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage

from . import schedule
from .speech import spoken_phone

log = logging.getLogger("receptionist.notify")

SCHEMA = """
CREATE TABLE IF NOT EXISTS notify_log (
  id INTEGER PRIMARY KEY, at TEXT, day TEXT, business_id TEXT, call_id TEXT, kind TEXT, channel TEXT,
  to_addr TEXT, subject TEXT, status TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS sms_optout (number TEXT PRIMARY KEY, at TEXT);
CREATE TABLE IF NOT EXISTS digest_sent (business_id TEXT, day TEXT, PRIMARY KEY (business_id, day));
"""

DEFAULT_DAILY_CAP = 500        # Gmail's limit for a regular account
DEFAULT_SHOP_PER_DAY = 40      # call emails one shop gets in a day; more wait for the digest


class SendError(Exception):
    pass


# ---- senders ----------------------------------------------------------------------------------------------------
class SmtpSender:
    """Plain SMTP with STARTTLS (Gmail with an app password). `factory` is injected in tests."""

    def __init__(self, host: str, user: str, password: str, port: int = 587, sender: str | None = None,
                 factory=smtplib.SMTP, timeout: int = 10):
        self.host, self.user, self.password, self.port = host, user, password, port
        self.sender, self.factory, self.timeout = sender or user, factory, timeout

    def send(self, to: str, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self.sender, to, subject
        msg.set_content(body)
        try:
            with self.factory(self.host, self.port, timeout=self.timeout) as s:
                s.starttls()
                s.login(self.user, self.password)
                s.send_message(msg)
        except (OSError, smtplib.SMTPException) as e:
            raise SendError(f"{type(e).__name__}: {e}") from e


class HttpEmailSender:
    """For when a domain exists: POST JSON to a provider (Resend, Postmark). Same interface as SmtpSender; `post` is
    injected in tests. Not used until EMAIL_API_URL and EMAIL_API_KEY are set."""

    def __init__(self, url: str, key: str, sender: str, post=None):
        self.url, self.key, self.sender = url, key, sender
        self.post = post or self._post

    def _post(self, url: str, headers: dict, payload: dict) -> None:
        req = urllib.request.Request(url, json.dumps(payload).encode(), headers, method="POST")
        with urllib.request.urlopen(req, timeout=10):
            pass

    def send(self, to: str, subject: str, body: str) -> None:
        try:
            self.post(self.url, {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"},
                      {"from": self.sender, "to": [to], "subject": subject, "text": body})
        except OSError as e:
            raise SendError(f"{type(e).__name__}: {e}") from e


def email_sender_from_env(env=None) -> object | None:
    env = os.environ if env is None else env
    if env.get("EMAIL_API_URL") and env.get("EMAIL_API_KEY") and env.get("EMAIL_FROM"):
        return HttpEmailSender(env["EMAIL_API_URL"], env["EMAIL_API_KEY"], env["EMAIL_FROM"])
    if env.get("SMTP_HOST") and env.get("SMTP_USER") and env.get("SMTP_PASSWORD"):
        return SmtpSender(env["SMTP_HOST"], env["SMTP_USER"], env["SMTP_PASSWORD"], int(env.get("SMTP_PORT", 587)),
                          env.get("SMTP_FROM"))
    return None


# ---- the notifier ------------------------------------------------------------------------------------------------
def _day(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).date().isoformat()


class Notifier:
    def __init__(self, store, email=None, sms=None, admin_email: str | None = None, base_url: str | None = None,
                 shop_key=None, daily_cap: int | None = None, clock=None):
        """`shop_key(biz)` returns the key that opens that shop's dashboard (put in the email link), or None."""
        self.store, self.email, self.sms = store, email, sms
        self.admin_email = admin_email
        self.base_url = (base_url or "").rstrip("/")
        self.shop_key = shop_key
        self.daily_cap = daily_cap or DEFAULT_DAILY_CAP
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._warned_day: str | None = None
        store.db.executescript(SCHEMA)

    # -- bookkeeping --
    def _log(self, biz_id, call_id, kind, channel, to, subject, status, error=None) -> None:
        now = self.clock()
        self.store._write("INSERT INTO notify_log (at, day, business_id, call_id, kind, channel, to_addr, subject, status, error)"
                          " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                          (now.isoformat(), _day(now), biz_id, call_id, kind, channel, to, subject, status, error))

    def sent_today(self, channel: str = "email", business_id: str | None = None) -> int:
        sql, args = "SELECT COUNT(*) AS n FROM notify_log WHERE day=? AND channel=? AND status='sent'", [_day(self.clock()), channel]
        if business_id:
            sql, args = sql + " AND business_id=?", args + [business_id]
        return self.store._rows(sql, tuple(args))[0]["n"]

    def recent(self, limit: int = 50) -> list[dict]:
        return self.store._rows("SELECT * FROM notify_log ORDER BY id DESC LIMIT ?", (limit,))

    def failures_today(self) -> int:
        return self.store._rows("SELECT COUNT(*) AS n FROM notify_log WHERE day=? AND status='failed'", (_day(self.clock()),))[0]["n"]

    # -- one email, with the limits --
    def _send_email(self, biz_id, call_id, kind, to: str, subject: str, body: str, urgent: bool = False,
                    shop_cap: int | None = None) -> bool:
        """True if sent. Never raises. Non-urgent mail past a shop's daily share, or past 90% of the provider's daily cap,
        is held back (logged 'held') and appears in the digest instead; urgent mail goes until the cap itself."""
        try:
            if self.email is None:
                self._log(biz_id, call_id, kind, "email", to, subject, "skipped", "no email sender configured")
                return False
            sent = self.sent_today()
            if sent >= self.daily_cap or (not urgent and (sent >= 0.9 * self.daily_cap or
                                          (shop_cap is not None and self.sent_today(business_id=biz_id) >= shop_cap))):
                self._log(biz_id, call_id, kind, "email", to, subject, "held", f"daily limit ({sent}/{self.daily_cap})")
                return False
            self.email.send(to, subject, body)
            self._log(biz_id, call_id, kind, "email", to, subject, "sent")
            if sent + 1 >= self.daily_cap // 2 and self._warned_day != _day(self.clock()):
                self._warned_day = _day(self.clock())
                log.warning("email sending is at %d of the %d daily cap", sent + 1, self.daily_cap)
            return True
        except Exception as e:   # SendError, or anything a sender or the database does: never into the call path
            log.error("notification %s for %s failed: %s", kind, call_id, e)
            try:
                self._log(biz_id, call_id, kind, "email", to, subject, "failed", str(e)[:300])
            except Exception:
                log.exception("could not record the failure")
            return False

    # -- W2.1: one email per call --
    def link(self, biz: dict, call_id: str | None = None) -> str:
        """The shop's dashboard (the page has no per-call anchor; the call is the newest card)."""
        if not self.base_url:
            return ""
        key = self.shop_key(biz) if self.shop_key else None
        q = {"business": biz["id"], **({"token": key} if key else {})}
        return f"{self.base_url}/dashboard?{urllib.parse.urlencode(q)}"

    def call_finished(self, biz: dict, call_id: str, summary: dict | None, reason: str | None = None,
                      call_row: dict | None = None) -> None:
        """Called once a call is over and summarized (summary None if the summary failed). Never raises."""
        self.drain_sms(biz)      # texts queued during the call (only when the shop has texting_enabled)
        try:
            facts = self._facts(biz, call_id, summary, reason)
            for to in (biz.get("notify") or {}).get("email", []):
                subject, body = call_email(biz, facts, self.link(biz, call_id))
                self._send_email(biz["id"], call_id, "urgent" if facts["urgent"] else "call", to, subject, body,
                                 urgent=facts["urgent"],
                                 shop_cap=int((biz.get("notify") or {}).get("max_per_day", DEFAULT_SHOP_PER_DAY)))
        except Exception:
            log.exception("call_finished notification failed for %s", call_id)

    def _facts(self, biz, call_id, summary, reason) -> dict:
        s = summary or {}
        msgs = self.store.for_call("messages", call_id)
        appts = self.store.appointments_for_call(call_id)
        leads = self.store.for_call("leads", call_id)
        call = self.store.get_call(call_id) or {}
        urgent = (s.get("urgency") == "emergency" or any(m["urgent"] for m in msgs) or reason == "safety"
                  or s.get("intent") == "emergency")
        return {"call_id": call_id, "summary": s, "messages": msgs, "appointments": appts, "leads": leads, "urgent": urgent,
                "reason": reason, "caller": call.get("caller")}

    # -- W2.2: queued texts and inbound STOP --
    def drain_sms(self, biz: dict) -> int:
        """Send this shop's queued texts. Does nothing unless the shop has texting_enabled and a sender exists (queued
        rows then wait). Owner alerts go only to the shop's own owner_sms number; any other recipient is a caller, who is
        skipped if they replied STOP, and whose text always ends with the opt-out line. Never raises. Returns the count sent."""
        if not biz.get("texting_enabled") or self.sms is None:
            return 0
        sent = 0
        try:
            rows = self.store._rows("SELECT * FROM sms_outbox WHERE business_id=? AND status='queued' ORDER BY id", (biz["id"],))
            for r in rows:
                owner = _digits(r["to_number"]) == _digits(biz.get("owner_sms"))
                if not owner and self.store._rows("SELECT 1 FROM sms_optout WHERE number=?", (_digits(r["to_number"]),)):
                    self.store._write("UPDATE sms_outbox SET status='blocked_optout' WHERE id=?", (r["id"],))
                    self._log(biz["id"], r["call_id"], "sms", "sms", r["to_number"], "", "skipped", "recipient opted out")
                    continue
                body = r["body"] if owner or "STOP" in r["body"] else r["body"] + STOP_SUFFIX
                try:
                    self.sms.send(r["to_number"], body)
                    self.store._write("UPDATE sms_outbox SET status='sent' WHERE id=?", (r["id"],))
                    self._log(biz["id"], r["call_id"], "owner_sms" if owner else "caller_sms", "sms", r["to_number"], "", "sent")
                    sent += 1
                except SendError as e:
                    self.store._write("UPDATE sms_outbox SET status='failed' WHERE id=?", (r["id"],))
                    self._log(biz["id"], r["call_id"], "sms", "sms", r["to_number"], "", "failed", str(e)[:300])
        except Exception:
            log.exception("sending queued texts failed for %s", biz.get("id"))
        return sent


    def handle_inbound_sms(self, from_number: str, body: str) -> str | None:
        """An inbound text from a caller: STOP-type words opt them out, START-type words back in. Returns the reply to
        send (Twilio also answers STOP itself at the Messaging Service; this keeps our own queue honest), else None."""
        word = (body or "").strip().lower()
        n = _digits(from_number)
        if not n:
            return None
        if word in STOP_WORDS:
            self.store._write("INSERT OR REPLACE INTO sms_optout (number, at) VALUES (?, ?)", (n, self.clock().isoformat()))
            return STOP_REPLY
        if word in START_WORDS:
            self.store._write("DELETE FROM sms_optout WHERE number=?", (n,))
            return START_REPLY
        return None


def call_email(biz: dict, f: dict, link: str) -> tuple[str, str]:
    s = f["summary"]
    who = s.get("caller_name") or "a caller"
    number = s.get("callback_number") or f.get("caller")
    outcome = s.get("outcome") or f.get("reason") or "unknown"
    subject = f"{'URGENT: ' if f['urgent'] else ''}{biz['name']}: {who} - {outcome}"
    lines = [s.get("summary") or "The call ended before a summary could be written; the transcript is on your dashboard.", ""]
    lines += [f"Who: {who}", f"Callback number: {spoken_phone(number) if number else 'not given'}", f"Outcome: {outcome}"]
    for a in f["appointments"]:
        lines.append(f"Booked: {a.get('window_id')} ({a.get('confirmation')}) at {', '.join(x for x in (a.get('address'), a.get('town')) if x)}")
    for m in f["messages"]:
        lines.append(f"{'URGENT message' if m['urgent'] else 'Message'}: {m['message']}")
    if s.get("follow_up"):
        lines.append(f"Follow up: {s['follow_up']}")
    if link:
        lines += ["", f"Full call and transcript: {link}"]
    return subject, "\n".join(lines)


# ---- W2.2: texts, behind texting_enabled -----------------------------------------------------------------------------
STOP_WORDS = {"stop", "stopall", "unsubscribe", "cancel", "end", "quit"}
START_WORDS = {"start", "yes", "unstop"}
STOP_SUFFIX = " Reply STOP to opt out."
STOP_REPLY = "You are unsubscribed and will get no more texts from us. Reply START to turn them back on."
START_REPLY = "You are subscribed again. Reply STOP to opt out."


class TwilioSender:
    """Twilio Messages API through a Messaging Service (the service holds the registered 10DLC campaign). `post` is
    injected in tests; it takes (url, auth_header, form dict) and returns the HTTP status."""

    def __init__(self, account_sid: str, auth_token: str, messaging_service_sid: str, post=None):
        self.sid, self.service = account_sid, messaging_service_sid
        self.auth = "Basic " + base64.b64encode(f"{account_sid}:{auth_token}".encode()).decode()
        self.post = post or self._post

    def _post(self, url: str, auth: str, form: dict) -> int:
        req = urllib.request.Request(url, urllib.parse.urlencode(form).encode(), {"Authorization": auth}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status

    def send(self, to: str, body: str) -> None:
        try:
            status = self.post(f"https://api.twilio.com/2010-04-01/Accounts/{self.sid}/Messages.json", self.auth,
                               {"To": to, "MessagingServiceSid": self.service, "Body": body})
        except OSError as e:     # urllib's HTTPError is an OSError too
            raise SendError(f"{type(e).__name__}: {e}") from e
        if status >= 300:
            raise SendError(f"Twilio answered {status}")


def sms_sender_from_env(env=None) -> TwilioSender | None:
    env = os.environ if env is None else env
    keys = ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_MESSAGING_SERVICE_SID")
    return TwilioSender(*(env[k] for k in keys)) if all(env.get(k) for k in keys) else None


def _digits(number: str | None) -> str:
    return "".join(c for c in (number or "") if c.isdigit())[-10:]
