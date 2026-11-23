"""Phaze machine reservation demo — Flask app.

Booking form -> assign via Phaze API -> status page w/ countdown ->
background sweeper unassigns expired reservations every 15s and
auto-promotes the next queued request onto a freed machine.
Admin view: fleet status with live sessions, disconnect, disconnect &
release, history, queue.

Unassigning a machine disconnects its assigned user (hosts on Phaze 0.14.0
or later), so releasing or expiring a reservation ends the live session.
"Disconnect only" ends the session and gives the seat straight back.
"""
import os
import threading
import time

from flask import Flask, redirect, render_template, request, url_for, flash

import phaze_client as phz
import store

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret-change-me")

SWEEP_INTERVAL_SEC = 15


@app.template_filter("fmt_hm")
def fmt_hm(iso_value):
    """API timestamp (UTC ISO 8601) -> local 24-hour time, e.g. 14:05."""
    dt = phz.parse_time(iso_value)
    return dt.strftime("%H:%M") if dt else ""


def _session_label(conn: dict | None) -> str:
    if not conn:
        return "Not connected"
    since = fmt_hm(conn.get("connected_at"))
    return f"Connected since {since}" if since else "Connected"

# Optional Slack notification on auto-expiry — only imported if configured,
# so this app runs standalone without slack_sdk/slack_bolt installed.
_slack_client = None
if os.getenv("SLACK_BOT_TOKEN"):
    try:
        from slack_sdk import WebClient
        _slack_client = WebClient(token=os.getenv("SLACK_BOT_TOKEN"))
    except ImportError:
        _slack_client = None


def _notify_expiry(reservation: dict):
    if not _slack_client:
        return
    try:
        member = phz.find_member_by_email(reservation["email"])
        slack_user_id = None
        if member:
            # best effort — Phaze doesn't map back to a Slack ID directly;
            # try resolving Slack user by email lookup instead.
            resp = _slack_client.users_lookupByEmail(email=reservation["email"])
            if resp.get("ok"):
                slack_user_id = resp["user"]["id"]
        if slack_user_id:
            _slack_client.chat_postMessage(
                channel=slack_user_id,
                text=f":alarm_clock: Your reservation on *{reservation['machine_name']}* has expired. "
                     f"You've been disconnected and the machine has been released.",
            )
    except Exception:
        pass  # notification is best-effort, never block the sweep


def _release_and_promote(reservation: dict, status: str = "expired"):
    """Unassign a machine (which disconnects the user) and, if someone is
    queued, promote them onto it. Only auto-expiry sends the Slack DM."""
    phz.unassign_machines([reservation["machine_id"]])
    store.release_reservation(reservation["id"], status=status)
    if status == "expired":
        _notify_expiry(reservation)

    nxt = store.next_waiting()
    if nxt:
        member = phz.find_member_by_email(nxt["email"])
        if member:
            phz.assign_machine_to_user(reservation["machine_id"], member["id"])
            store.create_reservation(
                nxt["email"], member["id"], reservation["machine_id"],
                reservation["machine_name"], nxt["hours"],
            )
            store.mark_queue_status(nxt["id"], "promoted")


def _sweeper_loop():
    while True:
        try:
            for res in store.expired_active_reservations():
                _release_and_promote(res)
        except Exception as e:
            print(f"[sweeper] error: {e}")
        time.sleep(SWEEP_INTERVAL_SEC)


def start_sweeper():
    t = threading.Thread(target=_sweeper_loop, daemon=True)
    t.start()


# ---------------- routes ----------------

@app.route("/", methods=["GET", "POST"])
def book():
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        hours_raw = request.form.get("hours", "1").strip()
        try:
            hours = float(hours_raw)
            if hours <= 0 or hours > 24:
                raise ValueError
        except ValueError:
            flash("Enter a number of hours between 0 and 24.")
            return redirect(url_for("book"))

        member = phz.find_member_by_email(email)
        if not member:
            flash(f"No Phaze member found for {email}. Ask an admin to invite you first.")
            return redirect(url_for("book"))

        available = phz.available_machines()
        if not available:
            q = store.enqueue(email, member["id"], hours)
            flash(f"No machines free right now — you're #{store.queue_length()} in the queue. "
                  f"You'll be assigned automatically the moment one frees up.")
            return redirect(url_for("book"))

        machine = available[0]
        phz.assign_machine_to_user(machine["id"], member["id"])
        res = store.create_reservation(email, member["id"], machine["id"], machine["name"], hours)
        return redirect(url_for("status", rid=res["id"]))

    machines = phz.list_machines()
    available_count = len(phz.available_machines())
    return render_template("book.html", total=len(machines), available=available_count,
                            queue_len=store.queue_length())


@app.route("/status/<rid>")
def status(rid):
    res = store.get_reservation(rid)
    if not res:
        flash("Reservation not found.")
        return redirect(url_for("book"))
    machine_id = res["machine_id"]
    deep_link = f"phaze://connect?id={machine_id}"
    conn = None
    session_known = True
    if res["status"] == "active":
        try:
            conn = next(
                (c for c in phz.list_connections(is_active=True, machine_id=machine_id,
                                                 accessor_id=res["user_id"])
                 if c.get("connected_at")),
                None,
            )
        except Exception:
            session_known = False
    return render_template("status.html", res=res, deep_link=deep_link, conn=conn,
                           session_label=_session_label(conn), session_known=session_known)


@app.route("/status/<rid>/disconnect", methods=["POST"])
def disconnect(rid):
    """Disconnect only: end the live session, keep the reservation."""
    res = store.get_reservation(rid)
    back = url_for("admin") if request.form.get("next") == "admin" else url_for("status", rid=rid)
    if not res or res["status"] != "active":
        flash("Reservation not active.")
        return redirect(back)
    try:
        ended = phz.disconnect_user(res["machine_id"], res["user_id"])
    except Exception as e:
        flash(f"Disconnect failed: {e}")
        return redirect(back)
    if ended:
        flash(f"{res['email']} was disconnected from {res['machine_name']}. "
              f"The reservation is still active, so they can reconnect.")
    else:
        flash(f"Phaze still shows {res['email']} connected to {res['machine_name']}. "
              f"The host needs Phaze 0.14.0 or later, and admin or IT support roles "
              f"keep their access. The reservation is still active.")
    return redirect(back)


@app.route("/status/<rid>/extend", methods=["POST"])
def extend(rid):
    res = store.get_reservation(rid)
    if not res or res["status"] != "active":
        flash("Reservation not active.")
        return redirect(url_for("book"))
    if store.queue_length() > 0:
        flash("Can't extend — someone is queued behind you.")
        return redirect(url_for("status", rid=rid))
    extra = float(request.form.get("hours", "1"))
    store.extend_reservation(rid, extra)
    flash(f"Extended by {extra}h.")
    return redirect(url_for("status", rid=rid))


@app.route("/status/<rid>/release", methods=["POST"])
def release(rid):
    """Disconnect & release: end the session and free the machine."""
    res = store.get_reservation(rid)
    if res and res["status"] == "active":
        _release_and_promote(res, status="released")
        flash("Disconnected and released. The machine is free for the next person.")
    return redirect(url_for("book"))


@app.route("/admin")
def admin():
    machines = phz.list_machines()
    reservations = store.all_reservations(limit=50)
    queue = store.all_queue(limit=50)
    active_by_machine = {r["machine_id"]: r for r in store.active_reservations()}
    try:
        sessions = phz.active_sessions_by_machine()
        sessions_known = True
    except Exception:
        sessions, sessions_known = {}, False
    return render_template("admin.html", machines=machines, reservations=reservations,
                            queue=queue, active_by_machine=active_by_machine,
                            sessions=sessions, sessions_known=sessions_known)


@app.route("/admin/disconnect/<machine_id>", methods=["POST"])
def admin_disconnect(machine_id):
    """Disconnect only, from the fleet view. Works for any machine assigned
    to a single user, whether or not this app made the reservation."""
    res = store.active_reservation_for_machine(machine_id)
    if res:
        user_id, label = res["user_id"], res["email"]
    else:
        m = phz.get_machine(machine_id)
        if not m or not m.get("assignee_id"):
            flash("Disconnect only works on a machine assigned to one user. "
                  "Use Disconnect & release instead.")
            return redirect(url_for("admin"))
        user_id, label = m["assignee_id"], m["assignee_id"]
        try:
            s = phz.active_sessions_by_machine().get(machine_id) or {}
            label = (s.get("accessor") or {}).get("email") or label
        except Exception:
            pass
    try:
        ended = phz.disconnect_user(machine_id, user_id)
    except Exception as e:
        flash(f"Disconnect failed: {e}")
        return redirect(url_for("admin"))
    if ended:
        flash(f"Disconnected {label}. The machine is still assigned to them.")
    else:
        flash(f"Phaze still shows {label} connected. The host needs Phaze 0.14.0 or later, "
              f"and admin or IT support roles keep their access.")
    return redirect(url_for("admin"))


@app.route("/admin/force-release/<machine_id>", methods=["POST"])
def force_release(machine_id):
    """Disconnect & release from the fleet view."""
    res = store.active_reservation_for_machine(machine_id)
    if res:
        _release_and_promote(res, status="released")
    else:
        phz.unassign_machines([machine_id])
    flash(f"Disconnected and released {machine_id[:12]}…")
    return redirect(url_for("admin"))


if __name__ == "__main__":
    store.init_db()
    start_sweeper()
    app.run(host="127.0.0.1", port=5055, debug=True, use_reloader=False)
