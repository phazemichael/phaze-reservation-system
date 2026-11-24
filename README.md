# Phaze Reservation System

A Flask demo showing how the Phaze Enterprise API can back a simple machine reservation flow. A user enters their Phaze account email and how many hours they need, gets a free machine assigned to them, and connects with a deep link. When the time runs out, the machine is unassigned automatically, the user is disconnected, and the next person in the queue gets it.

This is a sales-engineering demo, not a production system. There are no logins or permissions in the app itself. Every assignment is a real, live Phaze API call against the org you point it at.

## How it works

1. **Book**: the user enters their Phaze account email and the hours needed (0.25 to 24). The app looks up the member in the org, picks the first free machine (online, no user and no group assigned) and assigns it to them.
2. **Status page**: shows the machine, a countdown to the end of the reservation, a "Connect in Phaze" button (a `phaze://connect?id=...` deep link), and whether the user is connected right now (read live from the `/connections` API).
3. **Queue**: if no machine is free, the request is queued. As soon as a machine frees up, the next person in line is assigned to it automatically.
4. **Auto-expiry**: a background sweeper checks every 15 seconds for expired reservations and unassigns those machines. Unassigning a machine also disconnects the user if they're still connected (hosts on Phaze 0.14.0 or later). If `SLACK_BOT_TOKEN` is set, the user gets a Slack DM that their reservation expired.
5. **Extend**: the user can add an hour from the status page, unless someone is waiting in the queue.
6. **Manual disconnect**: "Disconnect" ends the live session and keeps the reservation, so the user can reconnect. "Disconnect & release" ends the session and frees the machine for the next person.
7. **Admin**: `/admin` shows the whole fleet with live session state ("Connected since 14:05"), Disconnect and Disconnect & release buttons per machine, the queue, and the reservation history.

Design choices worth knowing if you're presenting this:
- Machines are pulled live from the org, so there are no hardcoded machine IDs. Pointing it at another org just changes the fleet.
- A machine counts as free only when it's online and has no user or group assigned. Machines that someone assigned outside this app are left alone.
- The admin Disconnect also works on machines this app didn't assign, as long as they're assigned to a single user.
- Manual releases show as `released` in the history. Only reservations that ran out show as `expired`, and only those trigger the Slack DM.
- For a class-schedule version of the same idea, see [phaze-university-system](https://github.com/phazemichael/phaze-university-system).

## Requirements

- Python 3.10+
- A Phaze Enterprise API key and org ID with at least one machine in it
- Hosts on Phaze 0.14.0 or later for the disconnect on unassign

## Setup

```bash
git clone https://github.com/phazemichael/phaze-reservation-system.git
cd phaze-reservation-system

python -m venv .venv
source .venv/Scripts/activate   # Windows (git-bash); use .venv/bin/activate on macOS/Linux

pip install -r requirements.txt
```

Create a `.env` file in the project root (never commit this):

```
PHAZE_API_KEY=your_phaze_enterprise_api_key
PHAZE_ORG_ID=your_org_id
PHAZE_API_BASE=https://public-api.phaze.app/enterprise/v1
FLASK_SECRET_KEY=any_random_string
# Optional, for the expiry DM (also needs: pip install slack_sdk)
SLACK_BOT_TOKEN=xoxb-...
```

## Running it

```bash
python app.py
```

The app starts on `http://127.0.0.1:5055` (see the port in `app.py` if you need to change it) and creates `reservations.db` (SQLite) on first run.

## Using it for a demo

1. Open `http://127.0.0.1:5055/`, enter the email of a member in the connected org and book a short slot (0.25 hours works well for showing expiry).
2. On the status page, click "Connect in Phaze", or just narrate it if the Phaze client isn't installed on the demo machine. The session badge switches to "Connected since ..." after a refresh.
3. Open `/admin` to show the fleet, the live session and the reservation history.
4. Click "Disconnect" on the status page or in `/admin`. The Phaze session ends within a few seconds and the machine stays assigned, so the user can reconnect.
5. Book again with a second email while every machine is taken to show the queue, then click "Disconnect & release" on the first reservation. The queued user is assigned the freed machine straight away.
6. To show auto-expiry, let a short reservation run out. Within 15 seconds the machine is unassigned and the user is disconnected.

Use a member-role account for the disconnect steps. Admin and IT support roles can connect to any machine, so Phaze doesn't disconnect them, and the app tells you so.

## Project layout

```
app.py            Flask routes: booking, status, extend, disconnect, release, admin, background sweeper
phaze_client.py   Thin wrapper around the Phaze Enterprise API (machines, members, assign/unassign, connections)
store.py          SQLite storage for reservations and the queue, expiry queries
templates/        Jinja templates (book, status, admin)
static/           CSS theme and fonts
```

## Notes

- `reservations.db`, `.env`, `.venv/` and `__pycache__/` are gitignored. Don't commit real API keys or the local SQLite file.
- The public API has no separate disconnect call. "Disconnect" unassigns the machine (which ends the session), polls `/connections` until the session is gone (up to 20 seconds), then assigns the machine back to the same user. While that runs, the machine is held so no one else can book it.
- The sweeper runs as long as the Flask process is alive. If the app is stopped, reservations that expire in the meantime are released on the next start.
- Every API request sends a custom `User-Agent`, because Cloudflare in front of the API rejects some default client signatures.
