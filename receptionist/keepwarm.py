"""Keeps the live shops' prompt cache alive. The cache lasts an hour; a read renews it, so one prefill every 50 minutes
during the shop's day means the first caller after a quiet stretch finds it warm instead of waiting for a full write.

Expected in the log: cache_read > 0 and cache_write near 0. If every refresh shows a full write, it is paying for what it
should be saving (about $0.45 a day on Sonnet): turn it off with live.cache_refresh false."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger("receptionist")

OPEN_HOUR, CLOSE_HOUR = 7, 21   # the shop's local day: 07:00 to 21:00


class CacheRefresher:
    def __init__(self, cfg: dict, client, targets, prewarm_fn, interval_min: int | None = None):
        self.cfg, self.client, self.targets, self.prewarm_fn = cfg, client, targets, prewarm_fn
        live = cfg.get("live", {})
        self.enabled = bool(live.get("cache_refresh", True))
        self.interval_s = 60 * int(interval_min or live.get("cache_refresh_minutes", 50))
        self.last: dict[str, datetime] = {}

    def mark(self, biz_id: str, now: datetime) -> None:
        """A prefill happened (startup warm, or a call's own): the clock for this shop restarts."""
        self.last[biz_id] = now

    def tick(self, now: datetime | None = None) -> list[dict]:
        """Refreshes every live shop that is due and inside its day. Returns what it did (usage per shop)."""
        now = now or datetime.now(timezone.utc)
        if not self.enabled:
            return []
        done = []
        for biz in self.targets(self.cfg):
            local = now.astimezone(ZoneInfo(biz.get("timezone") or "America/Los_Angeles"))
            if not (OPEN_HOUR <= local.hour < CLOSE_HOUR):
                continue
            last = self.last.get(biz["id"])
            if last and (now - last).total_seconds() < self.interval_s:
                continue
            try:
                usage = self.prewarm_fn(self.client, self.cfg, biz)
            except Exception as e:   # a failed refresh costs only the wait it would have saved
                log.warning("cache refresh for %s failed: %s", biz["id"], e)
                continue
            self.last[biz["id"]] = now
            log.info("cache refresh %s: %s", biz["id"], usage)
            if usage.get("cache_write", 0) > 0 and usage.get("cache_read", 0) == 0:
                log.warning("cache refresh %s wrote the whole prompt and read nothing: the refresh is not keeping the cache alive. "
                            "Set live.cache_refresh to false.", biz["id"])
            self._record(biz["id"], now, usage)
            done.append({"biz": biz["id"], "usage": usage})
        return done

    def _record(self, biz_id: str, now: datetime, usage: dict) -> None:
        """One line per refresh on the server's disk, so what the refresh costs can be priced (data/cache_refresh.jsonl)."""
        try:
            with open(Path(self.cfg["data_dir"]) / "cache_refresh.jsonl", "a") as f:
                f.write(json.dumps({"at": now.isoformat(), "business": biz_id, **usage}) + "\n")
        except OSError:
            pass
