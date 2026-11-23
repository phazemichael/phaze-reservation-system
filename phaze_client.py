"""Thin wrapper around the Phaze Enterprise API for the reservation demo."""
import os
import threading
import time
from datetime import datetime

import httpx
from dotenv import load_dotenv

load_dotenv()

API_BASE = os.getenv("PHAZE_API_BASE", "https://public-api.phaze.app/enterprise/v1")
API_KEY = os.getenv("PHAZE_API_KEY", "")
ORG_ID = os.getenv("PHAZE_ORG_ID", "")

# Cloudflare in front of the API rejects some default client signatures,
# so always send our own User-Agent.
USER_AGENT = "phaze-reservation-demo/1.1"

# Machines in the middle of a "disconnect only": they are unassigned for a
# few seconds and must not look free to the booking form or the queue.
_held: set[str] = set()
_held_lock = threading.Lock()


def _client() -> httpx.Client:
    return httpx.Client(
        base_url=API_BASE,
        headers={"Authorization": f"Bearer {API_KEY}", "User-Agent": USER_AGENT},
        timeout=15.0,
    )


def list_orgs() -> list[dict]:
    with _client() as c:
        return c.get("/orgs").json()["data"]


def list_machines(org_id: str = ORG_ID) -> list[dict]:
    with _client() as c:
        r = c.get(f"/orgs/{org_id}/machines", params={"limit": 200})
        r.raise_for_status()
        return r.json()["data"]


def get_machine(machine_id: str, org_id: str = ORG_ID) -> dict | None:
    for m in list_machines(org_id):
        if m["id"] == machine_id:
            return m
    return None


def available_machines(org_id: str = ORG_ID) -> list[dict]:
    """Online AND unassigned (no user, no group), minus machines held
    during a disconnect-only."""
    with _held_lock:
        held = set(_held)
    return [
        m for m in list_machines(org_id)
        if m.get("is_online") and not m.get("assignee_id") and not m.get("group_id")
        and m["id"] not in held
    ]


def find_member_by_email(email: str, org_id: str = ORG_ID) -> dict | None:
    """GET /members?q= is a fuzzy substring match — filter to an exact,
    case-insensitive email match ourselves."""
    email_l = email.strip().lower()
    with _client() as c:
        r = c.get(f"/orgs/{org_id}/members", params={"q": email_l, "limit": 200})
        r.raise_for_status()
        for m in r.json()["data"]:
            if m.get("email", "").strip().lower() == email_l:
                return m
    return None


def assign_machine_to_user(machine_id: str, user_id: str, org_id: str = ORG_ID) -> None:
    with _client() as c:
        r = c.put(
            f"/orgs/{org_id}/machines/assign-user",
            json={"machine_ids": [machine_id], "user_id": user_id},
        )
        r.raise_for_status()


def unassign_machines(machine_ids: list[str], org_id: str = ORG_ID) -> None:
    """Remove the assignment. On hosts running Phaze 0.14.0 or later this
    also disconnects the assigned user if they are connected. Admin and IT
    support roles can connect to any machine and keep their session."""
    if not machine_ids:
        return
    with _client() as c:
        r = c.put(
            f"/orgs/{org_id}/machines/unassign",
            json={"machine_ids": machine_ids},
        )
        r.raise_for_status()


def list_connections(org_id: str = ORG_ID, is_active: bool | None = None,
                     machine_id: str | None = None, accessor_id: str | None = None) -> list[dict]:
    params: dict[str, str | int] = {"limit": 200}
    if is_active is not None:
        params["is_active"] = "true" if is_active else "false"
    if machine_id:
        params["machine_id"] = machine_id
    if accessor_id:
        params["accessor_id"] = accessor_id
    with _client() as c:
        r = c.get(f"/orgs/{org_id}/connections", params=params)
        r.raise_for_status()
        return r.json()["data"]


def active_sessions_by_machine(org_id: str = ORG_ID) -> dict[str, dict]:
    """Live sessions keyed by machine_id. Attempts that never connected
    (no connected_at) are skipped; if a machine has several sessions, the
    newest one wins."""
    out: dict[str, dict] = {}
    for c in list_connections(org_id, is_active=True):
        if not c.get("connected_at"):
            continue
        cur = out.get(c["machine_id"])
        if not cur or c["connected_at"] > cur["connected_at"]:
            out[c["machine_id"]] = c
    return out


def is_connected(machine_id: str, user_id: str, org_id: str = ORG_ID) -> bool:
    return any(
        c.get("connected_at")
        for c in list_connections(org_id, is_active=True, machine_id=machine_id, accessor_id=user_id)
    )


def parse_time(value: str | None) -> datetime | None:
    """API timestamps are ISO 8601 in UTC; return them in local time."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return None


def disconnect_user(machine_id: str, user_id: str, org_id: str = ORG_ID,
                    wait_sec: float = 20.0, poll_sec: float = 1.5) -> bool:
    """End the user's live session but keep their seat.

    The public API has no separate disconnect call: unassigning a machine
    disconnects its assigned user. So unassign, wait until Phaze reports the
    session as ended, then assign the machine back to the same user so they
    can reconnect. The machine is held while this runs so nobody else can
    book it. Returns True if the session was seen ending within wait_sec.
    """
    with _held_lock:
        _held.add(machine_id)
    try:
        unassign_machines([machine_id], org_id)
        deadline = time.time() + wait_sec
        while time.time() < deadline:
            if not is_connected(machine_id, user_id, org_id):
                return True
            time.sleep(poll_sec)
        return False
    finally:
        try:
            assign_machine_to_user(machine_id, user_id, org_id)
        finally:
            with _held_lock:
                _held.discard(machine_id)
