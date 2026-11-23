"""Phaze machine reservation demo — Flask app.

Booking form -> assign via Phaze API -> status page w/ countdown ->
background sweeper unassigns expired reservations every 15s and
auto-promotes the next queued request onto a freed machine.
Admin view: fleet status, force-release, history, queue.
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
                text=f":alarm_clock: Your reservation on *{reservation['machine_name']}* has expired "
                     f"and the machine has been released.",
            )
    except Exception:
        pass  # notification is best-effort, never block the sweep


def _release_and_promote(reservation: dict):
    """Unassign a machine and, if someone is queued, promote them onto it."""
    phz.unassign_machines([reservation["machine_id"]])
    store.release_reservation(reservation["id"], status="expired")
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
    return render_template("status.html", res=res, deep_link=deep_link)


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
    res = store.get_reservation(rid)
    if res and res["status"] == "active":
        _release_and_promote(res)
        flash("Machine released.")
    return redirect(url_for("book"))


@app.route("/admin")
def admin():
    machines = phz.list_machines()
    reservations = store.all_reservations(limit=50)
    queue = store.all_queue(limit=50)
    active_ids = {r["machine_id"] for r in store.active_reservations()}
    return render_template("admin.html", machines=machines, reservations=reservations,
                            queue=queue, active_ids=active_ids)


@app.route("/admin/force-release/<machine_id>", methods=["POST"])
def force_release(machine_id):
    res = store.active_reservation_for_machine(machine_id)
    if res:
        _release_and_promote(res)
    else:
        phz.unassign_machines([machine_id])
    flash(f"Force-released {machine_id[:12]}…")
    return redirect(url_for("admin"))


if __name__ == "__main__":
    store.init_db()
    start_sweeper()
    app.run(host="127.0.0.1", port=5055, debug=True, use_reloader=False)
