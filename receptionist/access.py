"""Who may open a shop's dashboard and calendar feed.

Three kinds of key, none derivable from another:
  - the admin key (`DASHBOARD_TOKEN`) opens any shop's dashboard and feed; it stays with Noah.
  - a shop's dashboard key and a shop's feed key are HMACs under `TOKEN_SECRET`, which is never shown to anyone. Each
    is bound to one shop id and that shop's `token_version`, so one shop's link opens nothing of another's, and raising
    `token_version` in a shop's file revokes that shop's links alone.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from collections import defaultdict, deque

COOKIE = "rk"
FAILS_PER_MINUTE = 10
_TOKEN_IN_LOG = re.compile(r"(token=)[^&\s\"']*")


def shop_key(secret: str, biz: dict, purpose: str) -> str:
    """purpose is 'dashboard' or 'calendar'; the prefix keeps one key from ever working as the other."""
    msg = f"{purpose}:{biz['id']}:{int(biz.get('token_version', 1))}"
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()[:32]


def allowed(key: str, purpose: str, biz: dict | None, admin_key: str | None, token_secret: str | None) -> bool:
    """True when `key` opens `purpose` for `biz`. The admin key opens dashboards only: a calendar link gets pasted into
    other apps, so the key that opens everything never goes in one. Both candidates are compared every time."""
    if not key or biz is None:
        return False
    ok = purpose == "dashboard" and bool(admin_key) and secrets.compare_digest(key, admin_key)
    if token_secret:
        ok |= secrets.compare_digest(key, shop_key(token_secret, biz, purpose))
    return ok


class FailureLimit:
    """Failed key attempts per client address in the last minute; past the limit every attempt is refused unchecked."""

    def __init__(self, per_minute: int = FAILS_PER_MINUTE, clock=time.monotonic):
        self.per_minute, self.clock = per_minute, clock
        self.fails: dict[str, deque] = defaultdict(deque)

    def _recent(self, who: str) -> deque:
        q = self.fails[who]
        while q and self.clock() - q[0] > 60:
            q.popleft()
        return q

    def blocked(self, who: str) -> bool:
        return len(self._recent(who)) >= self.per_minute

    def record(self, who: str) -> None:
        self._recent(who).append(self.clock())


def redact(text: str) -> str:
    return _TOKEN_IN_LOG.sub(r"\1REDACTED", text)


def redact_access_log(record) -> bool:
    """A logging filter for uvicorn.access: the request path there carries the query string, and so the key."""
    if isinstance(record.args, tuple):
        record.args = tuple(redact(a) if isinstance(a, str) else a for a in record.args)
    record.msg = redact(str(record.msg))
    return True


SAFE_HEADERS = {"Referrer-Policy": "no-referrer", "Cache-Control": "no-store", "X-Robots-Tag": "noindex"}
