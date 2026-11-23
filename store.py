"""SQLite-backed reservation + queue store. Separate from Phaze's own state
because Phaze doesn't have a "reservation with a duration" concept natively.
"""
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).with_name("reservations.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS reservations (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL,
    user_id TEXT NOT NULL,
    machine_id TEXT NOT NULL,
    machine_name TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    released_at REAL,
    status TEXT NOT NULL DEFAULT 'active'  -- active | released | expired
);

CREATE TABLE IF NOT EXISTS queue (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL,
    user_id TEXT NOT NULL,
    hours REAL NOT NULL,
    created_at REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'waiting'  -- waiting | promoted | cancelled
);
"""


@contextmanager
def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with _conn() as c:
        c.executescript(SCHEMA)


def create_reservation(email: str, user_id: str, machine_id: str, machine_name: str, hours: float) -> dict:
    rid = str(uuid.uuid4())
    now = time.time()
    expires = now + hours * 3600
    with _conn() as c:
        c.execute(
            "INSERT INTO reservations (id, email, user_id, machine_id, machine_name, created_at, expires_at, status)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 'active')",
            (rid, email, user_id, machine_id, machine_name, now, expires),
        )
    return get_reservation(rid)


def get_reservation(rid: str) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM reservations WHERE id = ?", (rid,)).fetchone()
        return dict(row) if row else None


def active_reservations() -> list[dict]:
    with _conn() as c:
        rows = c.execute("SELECT * FROM reservations WHERE status = 'active' ORDER BY expires_at").fetchall()
        return [dict(r) for r in rows]


def active_reservation_for_machine(machine_id: str) -> dict | None:
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM reservations WHERE machine_id = ? AND status = 'active'", (machine_id,)
        ).fetchone()
        return dict(row) if row else None


def all_reservations(limit: int = 100) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM reservations ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def extend_reservation(rid: str, extra_hours: float) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE reservations SET expires_at = expires_at + ? WHERE id = ? AND status = 'active'",
            (extra_hours * 3600, rid),
        )


def release_reservation(rid: str, status: str = "released") -> dict | None:
    res = get_reservation(rid)
    if not res or res["status"] != "active":
        return res
    with _conn() as c:
        c.execute(
            "UPDATE reservations SET status = ?, released_at = ? WHERE id = ?",
            (status, time.time(), rid),
        )
    return get_reservation(rid)


def expired_active_reservations() -> list[dict]:
    now = time.time()
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM reservations WHERE status = 'active' AND expires_at <= ?", (now,)
        ).fetchall()
        return [dict(r) for r in rows]


# ---- queue ----

def enqueue(email: str, user_id: str, hours: float) -> dict:
    qid = str(uuid.uuid4())
    now = time.time()
    with _conn() as c:
        c.execute(
            "INSERT INTO queue (id, email, user_id, hours, created_at, status) VALUES (?, ?, ?, ?, ?, 'waiting')",
            (qid, email, user_id, hours, now),
        )
    with _conn() as c:
        row = c.execute("SELECT * FROM queue WHERE id = ?", (qid,)).fetchone()
        return dict(row)


def next_waiting() -> dict | None:
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM queue WHERE status = 'waiting' ORDER BY created_at LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


def mark_queue_status(qid: str, status: str) -> None:
    with _conn() as c:
        c.execute("UPDATE queue SET status = ? WHERE id = ?", (status, qid))


def queue_length() -> int:
    with _conn() as c:
        row = c.execute("SELECT COUNT(*) AS n FROM queue WHERE status = 'waiting'").fetchone()
        return row["n"]


def all_queue(limit: int = 50) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM queue ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
