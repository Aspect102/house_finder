# House Finder — deployment

Two supported ways to run periodically:

## Option A — systemd service with internal scheduler (recommended)

The app runs its own APScheduler timer, so a single long-lived service is all
you need. Set `HF_SCHEDULER_ENABLED=true` and `HF_SYNC_INTERVAL_MINUTES=180`
in `.env`.

```bash
cd /home/housefinder/Documents/house_finder
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # edit paths/credentials

# Install the unit (edit User= and paths first)
sudo cp deploy/house-finder.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now house-finder
systemctl status house-finder
journalctl -u house-finder -f
```

## Option B — systemd service + timer (external scheduling)

If you prefer the OS to drive the schedule, disable the internal scheduler
(`HF_SCHEDULER_ENABLED=false`) and use the provided timer, which runs a
one-shot sync.

```bash
sudo cp deploy/house-finder.service /etc/systemd/system/
sudo cp deploy/house-finder-sync.service /etc/systemd/system/
sudo cp deploy/house-finder-sync.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now house-finder house-finder-sync.timer
systemctl list-timers house-finder-sync.timer
```

## Option C — cron

```cron
# every 3 hours
0 */3 * * * cd /home/housefinder/Documents/house_finder && .venv/bin/python -m house_finder.cli sync >> /var/log/house-finder.log 2>&1
```

## Bus-route (BODS) refresh

The web app's internal scheduler (Option A) refreshes bus routes/stops on its
own (`HF_BODS_REFRESH_HOURS`, default weekly) and once at startup if nothing is
cached. If you run with the internal scheduler **disabled** (Options B/C), add a
weekly job for the transit data as well:

```cron
# weekly, Monday 04:00
0 4 * * 1 cd /home/housefinder/Documents/house_finder && .venv/bin/python -m house_finder.cli transit sync >> /var/log/house-finder.log 2>&1
```

Requires `HF_BODS_API_KEY` in `.env`; without it the command reports a skip and
nothing else breaks.

## Reverse proxy (optional)

Put nginx/Caddy in front of `127.0.0.1:8000` if you want remote access. The app
has no auth, so keep it bound to localhost or protect it with a proxy.
