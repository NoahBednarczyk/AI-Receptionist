"""SQLite store for calls, customers, bookings, leads, messages and the outgoing text queue.

Simulations use Store(":memory:") so test calls never touch the real database.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta

from . import schedule
from .speech import normalize_phone

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
  id TEXT PRIMARY KEY, business_id TEXT, platform TEXT, caller TEXT,
  started_at TEXT, ended_at TEXT, outcome TEXT, transcript TEXT, summary TEXT
);
CREATE TABLE IF NOT EXISTS customers (
  business_id TEXT, phone TEXT, name TEXT, address TEXT, town TEXT, notes TEXT,
  PRIMARY KEY (business_id, phone)
);
CREATE TABLE IF NOT EXISTS appointments (
  id INTEGER PRIMARY KEY, business_id TEXT, confirmation TEXT, window_id TEXT, service_id TEXT,
  name TEXT, phone TEXT, address TEXT, town TEXT, problem TEXT, access_notes TEXT,
  status TEXT DEFAULT 'booked', call_id TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS leads (
  id INTEGER PRIMARY KEY, business_id TEXT, call_id TEXT, name TEXT, phone TEXT, address TEXT,
  problem TEXT, urgency TEXT, reason TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY, business_id TEXT, call_id TEXT, name TEXT, phone TEXT, message TEXT,
  for_whom TEXT, urgent INTEGER, created_at TEXT
);
CREATE TABLE IF NOT EXISTS sms_outbox (
  id INTEGER PRIMARY KEY, business_id TEXT, call_id TEXT, to_number TEXT, body TEXT,
  status TEXT DEFAULT 'queued', created_at TEXT
);
CREATE TABLE IF NOT EXISTS tool_events (
  id INTEGER PRIMARY KEY, call_id TEXT, name TEXT, args TEXT, result TEXT, created_at TEXT
);
"""


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        self.db.executescript(SCHEMA)

    def __del__(self):
        self.db.close()

    def _write(self, sql: str, params: tuple = ()) -> int:
        with self.lock:
            cur = self.db.execute(sql, params)
            self.db.commit()
            return cur.lastrowid

    def _rows(self, sql: str, params: tuple = ()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, params).fetchall()]

    # ---- demo data -------------------------------------------------------
    def seed_demo(self, biz: dict, now: datetime) -> None:
        """Returning customers plus a few booked windows near `now`, so availability looks like a real week."""
        bid = biz["id"]
        seed = biz.get("demo_seed", {})
        for c in seed.get("customers", []):
            self.upsert_customer(bid, c["phone"], c["name"], c.get("address"), c.get("town"), c.get("notes"))
        seeded = self._rows("SELECT created_at FROM appointments WHERE business_id=? AND call_id='seed' LIMIT 1", (bid,))
        if seeded and datetime.fromisoformat(seeded[0]["created_at"]).astimezone(now.tzinfo).date() == now.date():
            return   # (compared in the business's time zone: the stamp may come from an older time zone setting)
        # Seeded on an earlier day: those windows are in the past now, so start the demo week again from today.
        self._write("DELETE FROM appointments WHERE business_id=? AND call_id='seed'", (bid,))
        for a in seed.get("customer_appointments", []):
            d = now.date() + timedelta(days=a["day_offset"])
            c = next((c for c in seed.get("customers", []) if c["phone"] == a["phone"]), {})
            self._write(
                "INSERT INTO appointments (business_id, confirmation, window_id, service_id, name, phone, address, town,"
                " problem, status, call_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'booked', 'seed', ?)",
                (bid, a["confirmation"], schedule.window_id(d, a["window_start"]), a["service_id"], c.get("name"),
                 normalize_phone(a["phone"]), c.get("address"), c.get("town"), a.get("problem"), now.isoformat()),
            )
        for a in seed.get("appointments", []):
            d = now.date() + timedelta(days=a["day_offset"])
            if not schedule.windows_on(biz, d):
                continue
            wid = schedule.window_id(d, a["window_start"])
            for _ in range(a["count"]):
                self._write(
                    "INSERT INTO appointments (business_id, confirmation, window_id, service_id, name, status, call_id, created_at)"
                    " VALUES (?, 'SEED', ?, 'seed', 'Existing booking', 'booked', 'seed', ?)",
                    (bid, wid, now.isoformat()),
                )

    # ---- calls -----------------------------------------------------------
    def start_call(self, call_id: str, business_id: str, platform: str, caller: str | None, now: datetime) -> None:
        self._write(
            "INSERT OR IGNORE INTO calls (id, business_id, platform, caller, started_at) VALUES (?, ?, ?, ?, ?)",
            (call_id, business_id, platform, caller, now.isoformat()),
        )

    def end_call(self, call_id: str, transcript: list, outcome: str | None, now: datetime) -> None:
        self._write(
            "UPDATE calls SET ended_at=?, transcript=?, outcome=COALESCE(?, outcome) WHERE id=?",
            (now.isoformat(), json.dumps(transcript), outcome, call_id),
        )

    def save_summary(self, call_id: str, summary: dict) -> None:
        self._write("UPDATE calls SET summary=?, outcome=COALESCE(outcome, ?) WHERE id=?",
                    (json.dumps(summary), summary.get("outcome"), call_id))

    def get_call(self, call_id: str) -> dict | None:
        rows = self._rows("SELECT * FROM calls WHERE id=?", (call_id,))
        return rows[0] if rows else None

    def log_tool(self, call_id: str, name: str, args: dict, result: dict, now: datetime) -> None:
        self._write("INSERT INTO tool_events (call_id, name, args, result, created_at) VALUES (?, ?, ?, ?, ?)",
                    (call_id, name, json.dumps(args), json.dumps(result), now.isoformat()))

    def tool_events(self, call_id: str) -> list[dict]:
        rows = self._rows("SELECT name, args, result FROM tool_events WHERE call_id=? ORDER BY id", (call_id,))
        return [{"name": r["name"], "args": json.loads(r["args"]), "result": json.loads(r["result"])} for r in rows]

    # ---- customers and bookings -------------------------------------------
    def get_customer(self, business_id: str, phone: str | None) -> dict | None:
        n = normalize_phone(phone)
        rows = self._rows("SELECT * FROM customers WHERE business_id=? AND phone=?", (business_id, n)) if n else []
        return rows[0] if rows else None

    def upsert_customer(self, business_id, phone, name, address=None, town=None, notes=None) -> None:
        n = normalize_phone(phone)
        if not n:
            return
        self._write(
            "INSERT INTO customers (business_id, phone, name, address, town, notes) VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(business_id, phone) DO UPDATE SET name=excluded.name,"
            " address=COALESCE(excluded.address, address), town=COALESCE(excluded.town, town),"
            " notes=COALESCE(excluded.notes, notes)",
            (business_id, n, name, address, town, notes),
        )

    def booked_counts(self, business_id: str) -> dict[str, int]:
        rows = self._rows("SELECT window_id, COUNT(*) AS n FROM appointments WHERE business_id=? AND status='booked'"
                          " GROUP BY window_id", (business_id,))
        return {r["window_id"]: r["n"] for r in rows}

    def upcoming_appointments(self, business_id: str, phone: str, now: datetime) -> list[dict]:
        today = now.date().isoformat()
        return self._rows("SELECT * FROM appointments WHERE business_id=? AND phone=? AND status='booked'"
                          " AND window_id >= ? ORDER BY window_id", (business_id, normalize_phone(phone), today))

    def add_appointment(self, business_id, call_id, window_id, service_id, name, phone, address, town,
                        problem, access_notes, now) -> str:
        row = self._write(
            "INSERT INTO appointments (business_id, window_id, service_id, name, phone, address, town, problem,"
            " access_notes, call_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (business_id, window_id, service_id, name, normalize_phone(phone), address, town, problem,
             access_notes, call_id, now.isoformat()),
        )
        confirmation = f"A{1000 + row}"
        self._write("UPDATE appointments SET confirmation=? WHERE id=?", (confirmation, row))
        return confirmation

    def update_appointment(self, appt_id, window_id, service_id, address, town, problem, access_notes) -> None:
        self._write("UPDATE appointments SET window_id=?, service_id=?, address=?, town=?, problem=?,"
                    " access_notes=COALESCE(?, access_notes) WHERE id=?",
                    (window_id, service_id, address, town, problem, access_notes, appt_id))

    def get_appointment(self, business_id: str, confirmation: str) -> dict | None:
        rows = self._rows("SELECT * FROM appointments WHERE business_id=? AND UPPER(confirmation)=UPPER(?)",
                          (business_id, confirmation.strip()))
        return rows[0] if rows else None

    def set_appointment(self, appt_id: int, window_id: str | None = None, status: str | None = None) -> None:
        self._write("UPDATE appointments SET window_id=COALESCE(?, window_id), status=COALESCE(?, status) WHERE id=?",
                    (window_id, status, appt_id))

    def appointments_for_call(self, call_id: str) -> list[dict]:
        return self._rows("SELECT * FROM appointments WHERE call_id=?", (call_id,))

    # ---- leads, messages, texts -------------------------------------------
    def add_lead(self, business_id, call_id, name, phone, address, problem, urgency, reason, now) -> int:
        return self._write(
            "INSERT INTO leads (business_id, call_id, name, phone, address, problem, urgency, reason, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (business_id, call_id, name, normalize_phone(phone) or phone, address, problem, urgency, reason, now.isoformat()),
        )

    def add_message(self, business_id, call_id, name, phone, message, for_whom, urgent, now) -> int:
        return self._write(
            "INSERT INTO messages (business_id, call_id, name, phone, message, for_whom, urgent, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (business_id, call_id, name, normalize_phone(phone) or phone, message, for_whom, int(bool(urgent)), now.isoformat()),
        )

    def queue_sms(self, business_id, call_id, to_number, body, now) -> int:
        return self._write("INSERT INTO sms_outbox (business_id, call_id, to_number, body, created_at) VALUES (?, ?, ?, ?, ?)",
                           (business_id, call_id, to_number, body, now.isoformat()))

    def for_call(self, table: str, call_id: str) -> list[dict]:
        assert table in {"leads", "messages", "sms_outbox", "appointments"}
        return self._rows(f"SELECT * FROM {table} WHERE call_id=? ORDER BY id", (call_id,))

    def recent(self, table: str, business_id: str, limit: int = 10) -> list[dict]:
        assert table in {"calls", "appointments", "leads", "messages", "sms_outbox"}
        extra = " AND call_id != 'seed'" if table == "appointments" else ""
        return self._rows(f"SELECT * FROM {table} WHERE business_id=?{extra} ORDER BY rowid DESC LIMIT ?",
                          (business_id, limit))
