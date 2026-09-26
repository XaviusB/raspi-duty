# raspi-duty

Terminal dashboard for a Raspberry Pi. It polls the PagerDuty REST API and shows live incidents on the console, color-coded so an open critical incident is visible across the room.

A read-only PagerDuty API key is enough. The dashboard never acknowledges, resolves, or edits incidents.

## Screen

```
 PAGERDUTY DASHBOARD
 Critical: 1   Warning: 0   Acknowledged: 2   Resolved: 0   Total: 3    Last update: 14:32:01
 AGE   TITLE
 12m   Database primary is down
 4h    Disk space warning
---------------- resolved ----------------
 2h    Checkout latency
 1d    Disk cleanup finished
 [q] quit   [r] refresh   [UP/DOWN] scroll
```

Each row is the incident age (time since it was created) and its title. Triggered and acknowledged incidents come first. A `resolved` separator follows them, and the newest resolved incidents fill the blank lines under it, in green. When the open list already fills the screen, those lines stay hidden until a row frees up. The clock on the right is the last successful poll, in local time. API and network errors appear in red on the line under the counts; the previous list stays on screen.

## Colors

| Color | Meaning |
| --- | --- |
| Red | Triggered incident whose first alert is `critical` or `error`. Also used when that alert has no severity. |
| Orange | Triggered incident whose first alert is `warning` or `info`. |
| Light gray | Acknowledged incident. |
| Green | Resolved incident, shown under the separator in the leftover lines. |

Acknowledged incidents are always gray, including ones that were critical before someone acknowledged them. With `--no-alert-severity`, open incidents are colored from urgency instead: `high` is red and anything else is orange.

## Keys

| Key | Action |
| --- | --- |
| `q` | Quit |
| `r` | Poll immediately |
| Up / Down | Scroll the list |
| Click row | Open that incident in a browser on your laptop (when the laptop bridge is configured). The row shows bold for one second. |

The list refreshes on its own every 30 seconds.

## Install

Python 3 and the `requests` package. On the Pi:

```bash
cd /home/pi/raspi-duty
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## API key

In PagerDuty, open **Integrations > API Access Keys** and create a key. Read-only access is enough.

Keep the key out of the shell history and out of git. Put it in an environment file that only root can read:

```bash
sudo install -m 600 /dev/null /etc/pagerduty-dashboard.env
sudo editor /etc/pagerduty-dashboard.env
```

```bash
PAGERDUTY_API_TOKEN=your_token_here
PAGERDUTY_SERVICE_NAME=your_service_name
```

`PAGERDUTY_SERVICE_NAME` is optional. Leave it unset to show incidents from every service the key can see.

## Run

```bash
set -a
. /etc/pagerduty-dashboard.env
set +a
.venv/bin/python pagerduty_dashboard.py
```

Or pass the values on the command line:

```bash
.venv/bin/python pagerduty_dashboard.py \
  --token "$PAGERDUTY_API_TOKEN" \
  --service-name "your_service_name"
```

Before the full-screen view opens, a service filter prints the matched service names. If the name does not match, the program exits and leaves the console usable.

## Options

`pagerduty_dashboard.py --help` prints the same list.

| Option | Default | Effect |
| --- | --- | --- |
| `--token` | `PAGERDUTY_API_TOKEN` | REST API token. Required. |
| `--service-name` | `PAGERDUTY_SERVICE_NAME` | Show one service, matched by name. Empty or unset shows every service. |
| `--service-id` | none | Show these service IDs. Repeat the flag for more than one. Overrides `--service-name`. |
| `--status` | `triggered` and `acknowledged` | Statuses in the main list. Repeat the flag. Choices: `triggered`, `acknowledged`, `resolved`. Resolved incidents also fill the unused lines below that list. |
| `--interval` | `30` | Seconds between polls. |
| `--limit` | `50` | Maximum incidents kept, newest first. |
| `--no-alert-severity` | off | Skip the extra alert request and color open incidents by urgency. |
| `--bridge-url` | `PAGERDUTY_BRIDGE_URL` | POST incident URLs to the laptop bridge when you click a row (mouse or touchscreen). |
| `--bridge-token` | `PAGERDUTY_BRIDGE_TOKEN` | Shared secret for the laptop bridge (optional). |
| `--pointer-device` | `PAGERDUTY_POINTER_DEVICE` or `auto` | Mouse/touch `/dev/input/event*` path (used on the Pi console; curses mouse alone is not enough on `tty1`). |

Examples:

```bash
# Triggered incidents only; acknowledged ones stay hidden.
# Resolved incidents still fill the leftover lines.
.venv/bin/python pagerduty_dashboard.py --status triggered

# Two services, by ID
.venv/bin/python pagerduty_dashboard.py --service-id PXXXXXX --service-id PYYYYYY
```

`--service-name` asks PagerDuty for a partial, case-insensitive match, then keeps services whose name matches exactly when any do.

## Open incidents on your laptop

The Pi dashboard stays on the wall display. A second small program on your Linux laptop listens on the LAN and opens PagerDuty incident pages in your browser when you click a row on the Pi (the touchscreen is handled as a mouse by the console).

### Laptop: bridge

No extra packages — Python 3 stdlib only.

```bash
cd /path/to/raspi-duty
python3 pagerduty_laptop_bridge.py --host 0.0.0.0 --port 8765
```

Optional shared secret (use the same value on the Pi):

```bash
export PAGERDUTY_BRIDGE_TOKEN="$(openssl rand -hex 16)"
python3 pagerduty_laptop_bridge.py --host 0.0.0.0 --port 8765 --token "$PAGERDUTY_BRIDGE_TOKEN"
```

The bridge only opens `https://…pagerduty.com/…` URLs. Test locally:

```bash
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:8765/open \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://notallowed.example/"}'
# expect 400

curl -X POST http://127.0.0.1:8765/open \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://yoursubdomain.pagerduty.com/incidents/XXXX"}'
# browser should open
```

If you use a host firewall, allow the bridge port from the Pi’s IP only.

### Pi: enable click-to-open

The Linux console on `tty1` does not send mouse events to curses. The dashboard reads your touchscreen (or USB mouse) from evdev instead, while still treating it like a normal pointer.

1. Install dependencies on the Pi: `.venv/bin/pip install -r requirements.txt`
2. Add user `pi` to the `input` group, then reboot:

   ```bash
   sudo usermod -aG input pi
   ```

3. Add to `/etc/pagerduty-dashboard.env`:

   ```bash
   PAGERDUTY_BRIDGE_URL=http://192.168.1.50:8765/open
   PAGERDUTY_BRIDGE_TOKEN=your_shared_secret_if_you_use_one
   ```

   Use your laptop’s LAN IP or hostname instead of `192.168.1.50`.

4. Restart the dashboard service. On startup you should see `Pointer input: /dev/input/event…`.

Click an incident row (touchscreen or USB mouse). The clicked line is **bold for one second** (underlined if it was already bold). The footer shows `Opened on laptop`, `Bridge unreachable`, or `No URL` for a few seconds.

If auto-detection picks the wrong device, set `PAGERDUTY_POINTER_DEVICE` after checking:

```bash
grep -B2 -A5 Handlers= /proc/bus/input/devices
# or: sudo evtest   # note which event node moves when you touch the screen
```

## Start on boot

This unit takes the HDMI/console on `/dev/tty1`. Log in over SSH to manage the Pi; the local keyboard is the dashboard.

Create `/etc/systemd/system/pagerduty-dashboard.service`:

```ini
[Unit]
Description=PagerDuty Console Dashboard
After=network-online.target
Wants=network-online.target
Conflicts=getty@tty1.service

[Service]
EnvironmentFile=/etc/pagerduty-dashboard.env
ExecStart=/home/pi/raspi-duty/.venv/bin/python /home/pi/raspi-duty/pagerduty_dashboard.py
StandardInput=tty
StandardOutput=tty
TTYPath=/dev/tty1
Restart=always
RestartSec=5
User=pi

[Install]
WantedBy=multi-user.target
```

Change `User` and both paths if the checkout lives somewhere else. Then:

```bash
sudo systemctl daemon-reload
sudo systemctl disable --now getty@tty1.service
sudo systemctl enable --now pagerduty-dashboard.service
```

Useful checks:

```bash
systemctl status pagerduty-dashboard.service
journalctl -u pagerduty-dashboard.service -e
sudo systemctl restart pagerduty-dashboard.service
```

To get the login prompt back on the console:

```bash
sudo systemctl disable --now pagerduty-dashboard.service
sudo systemctl enable --now getty@tty1.service
```

## When something is wrong

| What you see | What to check |
| --- | --- |
| `Error: no API token provided` | `PAGERDUTY_API_TOKEN` or `--token` is missing. |
| `401 Unauthorized: check your API token` | The key was revoked, copied with extra spaces, or the env file is not loaded by the service. |
| `No PagerDuty service found matching '...'` | The service name does not match an exact name, and the partial search returned nothing. Try the service ID. |
| `Error looking up service` or `Network error` | The Pi cannot reach `api.pagerduty.com`. |
| Poll seems stuck after heavy use | PagerDuty returned HTTP 429. The client waits for the `Retry-After` header, then retries the same request. |
| Red incident you expected to be orange | The first alert has no severity, or its severity is `critical` or `error`. |
| Acknowledged incident missing | The process was started with `--status triggered` only. |
| Resolved incidents missing | The open list fills the screen, so there is no free line under the separator. |
