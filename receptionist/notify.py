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
