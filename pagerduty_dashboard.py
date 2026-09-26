#!/usr/bin/env python3
"""
PagerDuty Console Dashboard
============================
A lightweight terminal (curses) dashboard for Raspberry Pi that polls the
PagerDuty REST API and shows live incidents, color-coded by severity:

    GREEN  = resolved / closed (fills unused lines at the bottom)
    ORANGE = warning severity (open)
    RED    = critical severity (open)
    GRAY   = acknowledged

Docs used:
    - REST API overview:      https://developer.pagerduty.com/docs/introduction
    - List Incidents:         https://developer.pagerduty.com/api-reference/9d0b4b12e36f9-list-incidents
    - List Alerts for an
      Incident:                https://developer.pagerduty.com/api-reference (Incidents > Alerts)
    - Auth (API token header): "Authorization: Token token=<API_KEY>"

Setup
-----
1. Create a REST API key:
     PagerDuty > Integrations > API Access Keys > Create New API Key
   (Read-only is enough for this dashboard.)

2. Install dependencies (on the Pi):
     pip3 install requests

3. Run:
     export PAGERDUTY_API_TOKEN="your_token_here"
     export PAGERDUTY_SERVICE_NAME="your_service_name"  # optional
     python3 pagerduty_dashboard.py

   Or pass the token directly:
     python3 pagerduty_dashboard.py --token your_token_here

   You can also filter by service on the command line:
     python3 pagerduty_dashboard.py --service-name "your_service_name"

4. Optional: run at boot on the Pi's console (no desktop needed) via systemd.
   See the bottom of this file for a sample unit.

Controls
--------
    q       quit
    r       force refresh
    UP/DOWN scroll the incident list

When --bridge-url is set, click an incident row (mouse or touchscreen-as-mouse)
to open it on your laptop (see pagerduty_laptop_bridge.py).
"""

import argparse
import curses
import os
import sys
import textwrap
import threading
import time
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    print("This script requires the 'requests' package: pip3 install requests")
    sys.exit(1)

API_BASE = "https://api.pagerduty.com"

# ----------------------------------------------------------------------------
# PagerDuty API client
# ----------------------------------------------------------------------------


class PagerDutyClient:
    def __init__(self, token, timeout=10):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Token token={token}",
                "Accept": "application/vnd.pagerduty+json;version=2",
                "Content-Type": "application/json",
            }
        )
        self.timeout = timeout

    def _get(self, path, params=None):
        url = f"{API_BASE}{path}"
        while True:
            resp = self.session.get(url, params=params, timeout=self.timeout)
            if resp.status_code == 429:
                # Respect rate limiting
                retry_after = int(resp.headers.get("Retry-After", 5))
                time.sleep(max(retry_after, 1))
                continue
            resp.raise_for_status()
            return resp.json()

    def list_incidents(
        self,
        statuses=("triggered", "acknowledged", "resolved"),
        limit=50,
        service_ids=None,
        sort_by="created_at:desc",
    ):
        """Fetch recent incidents, newest first, with pagination."""
        incidents = []
        offset = 0
        while True:
            params = {
                "statuses[]": list(statuses),
                "sort_by": sort_by,
                "limit": min(limit, 100),
                "offset": offset,
                "total": "false",
            }
            if service_ids:
                params["service_ids[]"] = list(service_ids)
            data = self._get("/incidents", params=params)
            incidents.extend(data.get("incidents", []))
            if not data.get("more") or len(incidents) >= limit:
                break
            offset += len(data.get("incidents", []))
        return incidents[:limit]

    def get_incident_alerts(self, incident_id):
        """Fetch alerts attached to an incident (each alert carries a severity)."""
        data = self._get(f"/incidents/{incident_id}/alerts")
        return data.get("alerts", [])

    def find_service_ids(self, name):
        """Resolve a service name to its PagerDuty service id(s).

        Docs: https://developer.pagerduty.com/api-reference/e960cca205c0f-list-services
        The `query` param does a partial, case-insensitive match, so we still
        filter for an exact (case-insensitive) name match when possible.
        """
        data = self._get("/services", params={"query": name, "limit": 100})
        services = data.get("services", [])
        exact = [s for s in services if s.get("name", "").strip().lower() == name.strip().lower()]
        matches = exact or services
        return [s["id"] for s in matches], [s.get("name") for s in matches]


# ----------------------------------------------------------------------------
# Severity resolution
# ----------------------------------------------------------------------------

SEV_RESOLVED = "resolved"
SEV_CRITICAL = "critical"
SEV_WARNING = "warning"

# Events API v2 severities are: critical, error, warning, info
_SEVERITY_MAP = {
    "critical": SEV_CRITICAL,
    "error": SEV_CRITICAL,
    "warning": SEV_WARNING,
    "info": SEV_WARNING,
}


class SeverityCache:
    """Caches alert severity per incident so we don't re-fetch on every poll."""

    def __init__(self):
        self._cache = {}  # incident_id -> (updated_at, severity)
        self._lock = threading.Lock()

    def get(self, incident_id):
        with self._lock:
            entry = self._cache.get(incident_id)
            return entry[1] if entry else None

    def set(self, incident_id, updated_at, severity):
        with self._lock:
            self._cache[incident_id] = (updated_at, severity)

    def is_stale(self, incident_id, updated_at):
        with self._lock:
            entry = self._cache.get(incident_id)
            return entry is None or entry[0] != updated_at


def resolve_severity(client, incident, cache):
    """Return one of SEV_RESOLVED / SEV_CRITICAL / SEV_WARNING for an incident."""
    if incident.get("status") == "resolved":
        return SEV_RESOLVED

    incident_id = incident["id"]
    updated_at = incident.get("updated_at") or incident.get("created_at")

    if not cache.is_stale(incident_id, updated_at):
        return cache.get(incident_id) or SEV_CRITICAL

    try:
        alerts = client.get_incident_alerts(incident_id)
    except requests.RequestException:
        # Network hiccup: fall back to last known value, else assume critical
        return cache.get(incident_id) or SEV_CRITICAL

    severity = SEV_CRITICAL  # default assumption for triggered incidents
    if alerts:
        raw = (alerts[0].get("severity") or "").lower()
        severity = _SEVERITY_MAP.get(raw, SEV_CRITICAL)

    cache.set(incident_id, updated_at, severity)
    return severity


# ----------------------------------------------------------------------------
# Background polling
# ----------------------------------------------------------------------------


class Poller(threading.Thread):
    def __init__(self, client, interval, limit, state, use_alert_severity=True,
                 statuses=("triggered", "acknowledged", "resolved"), service_ids=None):
        super().__init__(daemon=True)
        self.client = client
        self.interval = interval
        self.limit = limit
        self.state = state
        self.use_alert_severity = use_alert_severity
        self.statuses = statuses
        self.service_ids = service_ids
        self.cache = SeverityCache()
        self._stop = threading.Event()
        self._force = threading.Event()

    def force_refresh(self):
        self._force.set()

    def stop(self):
        self._stop.set()
        self._force.set()

    def run(self):
        while not self._stop.is_set():
            self._poll_once()
            self._force.wait(self.interval)
            self._force.clear()

    def _to_row(self, inc):
        status = inc.get("status")
        if status == "acknowledged":
            sev = None
        elif status == "resolved":
            sev = SEV_RESOLVED
        elif self.use_alert_severity:
            sev = resolve_severity(self.client, inc, self.cache)
        else:
            sev = SEV_CRITICAL if inc.get("urgency") == "high" else SEV_WARNING
        return {
            "id": inc.get("id"),
            "number": inc.get("incident_number"),
            "title": inc.get("title") or inc.get("summary") or "",
            "service": (inc.get("service") or {}).get("summary", "?"),
            "status": status,
            "urgency": inc.get("urgency"),
            "created_at": inc.get("created_at"),
            "severity": sev,
            "html_url": inc.get("html_url"),
        }

    def _poll_once(self):
        try:
            open_statuses = [status for status in self.statuses if status != "resolved"]
            rows = []
            if open_statuses:
                incidents = self.client.list_incidents(
                    statuses=open_statuses, limit=self.limit, service_ids=self.service_ids
                )
                rows = [self._to_row(inc) for inc in incidents]
            resolved = self.client.list_incidents(
                statuses=["resolved"],
                limit=self.limit,
                service_ids=self.service_ids,
                sort_by="resolved_at:desc",
            )
            resolved_rows = [self._to_row(inc) for inc in resolved]
            with self.state["lock"]:
                self.state["rows"] = rows
                self.state["resolved_rows"] = resolved_rows
                self.state["last_update"] = datetime.now(timezone.utc)
                self.state["error"] = None
        except requests.HTTPError as e:
            with self.state["lock"]:
                code = e.response.status_code if e.response is not None else "?"
                if code == 401:
                    self.state["error"] = "401 Unauthorized: check your API token"
                else:
                    self.state["error"] = f"HTTP error {code}"
        except requests.RequestException as e:
            with self.state["lock"]:
                self.state["error"] = f"Network error: {e}"


# ----------------------------------------------------------------------------
# Curses UI
# ----------------------------------------------------------------------------

COLOR_GREEN_PAIR = 1
COLOR_ORANGE_PAIR = 2
COLOR_RED_PAIR = 3
COLOR_HEADER_PAIR = 4
COLOR_DIM_PAIR = 5
COLOR_GRAY_PAIR = 6

_GRAY_ATTR = 0


def init_colors():
    global _GRAY_ATTR
    curses.start_color()
    curses.use_default_colors()

    orange = 208 if curses.COLORS >= 256 else curses.COLOR_YELLOW
    if curses.COLORS >= 256:
        gray = 252
        gray_attr = 0
    else:
        gray = curses.COLOR_WHITE
        gray_attr = curses.A_DIM

    curses.init_pair(COLOR_GREEN_PAIR, curses.COLOR_GREEN, -1)
    curses.init_pair(COLOR_ORANGE_PAIR, orange, -1)
    curses.init_pair(COLOR_RED_PAIR, curses.COLOR_RED, -1)
    curses.init_pair(COLOR_HEADER_PAIR, curses.COLOR_BLACK, curses.COLOR_CYAN)
    curses.init_pair(COLOR_DIM_PAIR, curses.COLOR_WHITE, -1)
    curses.init_pair(COLOR_GRAY_PAIR, gray, -1)
    _GRAY_ATTR = curses.color_pair(COLOR_GRAY_PAIR) | gray_attr


def color_for(severity, status=None):
    if status == "acknowledged":
        return _GRAY_ATTR
    if severity == SEV_RESOLVED:
        return curses.color_pair(COLOR_GREEN_PAIR) | curses.A_BOLD
    if severity == SEV_WARNING:
        return curses.color_pair(COLOR_ORANGE_PAIR) | curses.A_BOLD
    return curses.color_pair(COLOR_RED_PAIR) | curses.A_BOLD


def fmt_age(iso_ts):
    if not iso_ts:
        return "-"
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except ValueError:
        return "-"
    delta = datetime.now(timezone.utc) - dt
    secs = int(delta.total_seconds())
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def layout_incidents(open_rows, resolved_rows, body_height, scroll_pos):
    """Place open incidents first, then fill leftover lines with resolved ones.

    Returns (visible_open, show_separator, visible_resolved, scroll_pos).
    The separator is drawn only when a resolved incident can sit under it.
    One leftover line is used for the newest resolved incident.
    """
    if body_height < 1:
        return [], False, [], 0

    max_scroll = max(0, len(open_rows) - body_height)
    scroll_pos = min(max(0, scroll_pos), max_scroll)
    visible_open = open_rows[scroll_pos:scroll_pos + body_height]
    free = body_height - len(visible_open)
    if free <= 0 or not resolved_rows:
        return visible_open, False, [], scroll_pos
    if visible_open and free == 1:
        return visible_open, False, resolved_rows[:1], scroll_pos
    if not visible_open and body_height == 1:
        return [], False, resolved_rows[:1], scroll_pos
    return visible_open, True, resolved_rows[: free - 1], scroll_pos


LIST_TOP = 4


def incident_at_row(rows, resolved_rows, scroll_pos, term_row, height):
    """Return the incident drawn on curses row term_row, or None."""
    body_height = height - LIST_TOP - 1
    if term_row < LIST_TOP or term_row >= height - 1:
        return None
    visible_open, show_separator, visible_resolved, _ = layout_incidents(
        rows, resolved_rows, body_height, scroll_pos
    )
    y = LIST_TOP
    for inc in visible_open:
        if y == term_row:
            return inc
        y += 1
    if show_separator:
        if y == term_row:
            return None
        y += 1
    for inc in visible_resolved:
        if y == term_row:
            return inc
        y += 1
    return None


MOUSE_CLICK_MASK = (
    curses.BUTTON1_CLICKED | curses.BUTTON1_RELEASED | curses.BUTTON1_PRESSED
)


def is_mouse_click(bstate):
    return bool(bstate & MOUSE_CLICK_MASK)


def pixel_y_to_row(pixel_y, y_absinfo, term_height):
    """Map a pointer Y coordinate to a curses row index."""
    lo = y_absinfo.min
    hi = y_absinfo.max
    if hi <= lo or term_height < 1:
        return min(max(0, pixel_y // 16), max(term_height - 1, 0))
    span = hi - lo + 1
    row = (pixel_y - lo) * term_height // span
    return min(max(0, row), term_height - 1)


def resolve_pointer_device(spec):
    """Find the touchscreen or mouse evdev node (Pi console does not use curses mouse)."""
    try:
        from evdev import InputDevice, ecodes, list_devices
    except ImportError:
        return None

    if spec and spec != "auto":
        return spec

    best_path = None
    best_score = 0
    for path in list_devices():
        try:
            dev = InputDevice(path)
        except OSError:
            continue
        name = (dev.name or "").lower()
        caps = dev.capabilities()
        keys = caps.get(ecodes.EV_KEY, [])
        abs_caps = caps.get(ecodes.EV_ABS, [])
        rel_caps = caps.get(ecodes.EV_REL, [])

        score = 0
        if ecodes.BTN_TOUCH in keys:
            score += 10
        if ecodes.BTN_LEFT in keys:
            score += 5
        mt_y = getattr(ecodes, "ABS_MT_POSITION_Y", None)
        if ecodes.ABS_X in abs_caps and ecodes.ABS_Y in abs_caps:
            score += 10
        elif mt_y is not None and mt_y in abs_caps:
            score += 10
        elif ecodes.REL_X in rel_caps and ecodes.REL_Y in rel_caps:
            score += 6
        if any(
            k in name
            for k in ("touch", "ft5406", "ft5x06", "generic ft5x06", "raspberrypi-ts")
        ):
            score += 25
        if "mouse" in name:
            score += 8
        if "keyboard" in name or "kbd" in name:
            score -= 50

        if score > best_score:
            best_score = score
            best_path = path
    return best_path if best_score > 0 else None


class PointerListener(threading.Thread):
    """Read Linux evdev pointer clicks (touchscreen or mouse) for tty1 consoles."""

    _CLICK_CODES = None

    def __init__(self, device_path, on_row_click, term_height_fn, stop_event):
        super().__init__(daemon=True)
        self.device_path = device_path
        self.on_row_click = on_row_click
        self.term_height_fn = term_height_fn
        self._stop = stop_event
        self._last_click = 0.0

    def _maybe_click(self, term_row):
        now = time.monotonic()
        if now - self._last_click < 0.25:
            return
        self._last_click = now
        self.on_row_click(term_row)

    def run(self):
        import select

        from evdev import InputDevice, ecodes

        if PointerListener._CLICK_CODES is None:
            PointerListener._CLICK_CODES = frozenset(
                c for c in (ecodes.BTN_LEFT, ecodes.BTN_TOUCH) if c is not None
            )

        try:
            dev = InputDevice(self.device_path)
            dev.grab()
        except OSError as e:
            sys.stderr.write(f"Pointer device {self.device_path}: {e}\n")
            return

        caps = dev.capabilities()
        abs_caps = caps.get(ecodes.EV_ABS, [])
        mt_pos_y = getattr(ecodes, "ABS_MT_POSITION_Y", None)
        mt_track = getattr(ecodes, "ABS_MT_TRACKING_ID", None)
        absolute = ecodes.ABS_Y in abs_caps or (
            mt_pos_y is not None and mt_pos_y in abs_caps
        )
        if ecodes.ABS_Y in abs_caps:
            y_absinfo = dev.absinfo(ecodes.ABS_Y)
        elif mt_pos_y is not None and mt_pos_y in abs_caps:
            y_absinfo = dev.absinfo(mt_pos_y)
        else:
            y_absinfo = None
        rel_y = 0
        y = 0

        def emit_row_click():
            term_height = self.term_height_fn()
            if absolute and y_absinfo is not None:
                term_row = pixel_y_to_row(y, y_absinfo, term_height)
            else:
                term_row = min(rel_y // 16, max(term_height - 1, 0))
            self._maybe_click(term_row)

        while not self._stop.is_set():
            try:
                ready, _, _ = select.select([dev.fd], [], [], 0.25)
                if not ready:
                    continue
                for event in dev.read():
                    if self._stop.is_set():
                        break
                    if event.type == ecodes.EV_ABS:
                        if event.code == ecodes.ABS_Y:
                            y = event.value
                        elif mt_pos_y is not None and event.code == mt_pos_y:
                            y = event.value
                        elif mt_track is not None and event.code == mt_track and event.value == -1:
                            emit_row_click()
                    elif event.type == ecodes.EV_REL and event.code == ecodes.REL_Y:
                        rel_y = max(0, rel_y + event.value)
                    elif event.type == ecodes.EV_KEY and event.code in PointerListener._CLICK_CODES:
                        if event.value != 0:
                            continue
                        emit_row_click()
            except OSError:
                break


def post_bridge(bridge_url, bridge_token, url, state):
    headers = {"Content-Type": "application/json"}
    if bridge_token:
        headers["Authorization"] = f"Bearer {bridge_token}"
    try:
        resp = requests.post(bridge_url, json={"url": url}, headers=headers, timeout=2)
        if resp.status_code in (200, 204):
            msg = "Opened on laptop"
        else:
            msg = f"Bridge error {resp.status_code}"
    except requests.RequestException:
        msg = "Bridge unreachable"
    with state["lock"]:
        state["bridge_feedback"] = msg
        state["bridge_feedback_until"] = time.monotonic() + 3


def activate_incident_at_row(term_row, scroll_pos, height, rows, resolved_rows, state, args):
    inc = incident_at_row(rows, resolved_rows, scroll_pos, term_row, height)
    if not inc:
        if args.bridge_url:
            with state["lock"]:
                state["bridge_feedback"] = f"No row {term_row}"
                state["bridge_feedback_until"] = time.monotonic() + 1.0
        return
    with state["lock"]:
        state["highlight_row"] = term_row
        state["highlight_until"] = time.monotonic() + 1.0
    if not args.bridge_url:
        return
    url = inc.get("html_url")
    if not url:
        with state["lock"]:
            state["bridge_feedback"] = "No URL"
            state["bridge_feedback_until"] = time.monotonic() + 3
        return
    threading.Thread(
        target=post_bridge,
        args=(args.bridge_url, args.bridge_token, url, state),
        daemon=True,
    ).start()


def safe_addnstr(stdscr, y, x, text, n, attr=0):
    """addnstr that swallows the harmless 'wrote to bottom-right cell' curses.error."""
    try:
        stdscr.addnstr(y, x, text, n, attr)
    except curses.error:
        pass


def draw(stdscr, state, scroll_pos):
    stdscr.erase()
    height, width = stdscr.getmaxyx()
    # Never write into the terminal's very last cell (bottom-right): curses
    # tries to advance the cursor past it and raises curses.error.
    last_row = height - 1
    safe_width = max(width - 1, 1)

    with state["lock"]:
        rows = list(state.get("rows", []))
        resolved_rows = list(state.get("resolved_rows", []))
        last_update = state.get("last_update")
        error = state.get("error")

    def row_width(y):
        """Usable width for a row: one less on the terminal's last line."""
        return safe_width if y == last_row else width

    # --- Header bar ---
    title = " PAGERDUTY DASHBOARD "
    header = title.ljust(width)
    safe_addnstr(stdscr, 0, 0, header, row_width(0), curses.color_pair(COLOR_HEADER_PAIR) | curses.A_BOLD)

    counts = {SEV_CRITICAL: 0, SEV_WARNING: 0, SEV_RESOLVED: len(resolved_rows), "acknowledged": 0}
    for r in rows:
        if r.get("status") == "acknowledged":
            counts["acknowledged"] += 1
        else:
            counts[r["severity"]] = counts.get(r["severity"], 0) + 1

    status_line = (
        f" Critical: {counts[SEV_CRITICAL]}   "
        f"Warning: {counts[SEV_WARNING]}   "
        f"Acknowledged: {counts['acknowledged']}   "
        f"Resolved: {counts[SEV_RESOLVED]}   "
        f"Total: {len(rows) + len(resolved_rows)}"
    )
    safe_addnstr(stdscr, 1, 0, status_line, row_width(1), curses.A_BOLD)

    if last_update:
        ts = last_update.astimezone().strftime("%H:%M:%S")
        stamp = f"Last update: {ts}"
        safe_addnstr(stdscr, 1, max(width - len(stamp) - 1, 0), stamp, row_width(1), curses.color_pair(COLOR_DIM_PAIR))

    if error:
        safe_addnstr(stdscr, 2, 0, f"! {error}"[: width - 1], row_width(2), curses.color_pair(COLOR_RED_PAIR) | curses.A_BOLD)

    # --- Column header ---
    col_header = f"{'AGE':<6}{'TITLE'}"
    safe_addnstr(stdscr, 3, 0, col_header, row_width(3), curses.A_UNDERLINE)

    # --- Rows ---
    list_top = LIST_TOP
    body_height = height - list_top - 1
    visible_open, show_separator, visible_resolved, scroll_pos = layout_incidents(
        rows, resolved_rows, body_height, scroll_pos
    )

    with state["lock"]:
        highlight_row = state.get("highlight_row")
        highlight_until = state.get("highlight_until") or 0
    now = time.monotonic()

    def paint_incident(y, incident):
        line = f"{fmt_age(incident['created_at']):<6}{incident['title']}"
        attr = color_for(incident["severity"], incident.get("status"))
        if highlight_row == y and now < highlight_until:
            if attr & curses.A_BOLD:
                attr |= curses.A_UNDERLINE
            else:
                attr |= curses.A_BOLD
        safe_addnstr(
            stdscr,
            y,
            0,
            line[: width - 1],
            row_width(y),
            attr,
        )

    y = list_top
    for incident in visible_open:
        paint_incident(y, incident)
        y += 1
    if show_separator:
        label = " resolved "
        separator = label.center(max(width - 1, len(label)), "-")
        safe_addnstr(stdscr, y, 0, separator, row_width(y), curses.color_pair(COLOR_DIM_PAIR))
        y += 1
    for incident in visible_resolved:
        paint_incident(y, incident)
        y += 1

    # --- Footer ---
    feedback = None
    with state["lock"]:
        until = state.get("bridge_feedback_until") or 0
        if time.monotonic() < until:
            feedback = state.get("bridge_feedback")
    footer = " [q] quit   [r] refresh   [UP/DOWN] scroll "
    if state.get("bridge_enabled"):
        footer += "  [click] open "
    if feedback:
        footer = f" {feedback} |" + footer
    safe_addnstr(stdscr, last_row, 0, footer.ljust(width), safe_width, curses.color_pair(COLOR_HEADER_PAIR))

    stdscr.refresh()
    return scroll_pos


def main_curses(stdscr, client, args, service_ids):
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.timeout(300)
    init_colors()

    state = {
        "lock": threading.Lock(),
        "rows": [],
        "resolved_rows": [],
        "last_update": None,
        "error": None,
        "bridge_enabled": bool(args.bridge_url),
    }
    poller = Poller(
        client,
        interval=args.interval,
        limit=args.limit,
        state=state,
        use_alert_severity=not args.no_alert_severity,
        statuses=args.status,
        service_ids=service_ids,
    )
    poller.start()

    pointer_stop = threading.Event()
    pointer_thread = None
    if args.bridge_url:
        curses.mousemask(MOUSE_CLICK_MASK)
        curses.mouseinterval(0)

        def term_height_fn():
            with state["lock"]:
                return state.get("term_height", 24)

        def on_row_click(term_row):
            with state["lock"]:
                scroll = state.get("scroll_pos", 0)
                height = state.get("term_height", 24)
                rows = list(state.get("rows", []))
                resolved_rows = list(state.get("resolved_rows", []))
            activate_incident_at_row(
                term_row, scroll, height, rows, resolved_rows, state, args
            )

        if args.pointer_device:
            pointer_thread = PointerListener(
                args.pointer_device, on_row_click, term_height_fn, pointer_stop
            )
            pointer_thread.start()

    scroll_pos = 0
    try:
        while True:
            height, _ = stdscr.getmaxyx()
            with state["lock"]:
                state["scroll_pos"] = scroll_pos
                state["term_height"] = height
            scroll_pos = draw(stdscr, state, scroll_pos)
            ch = stdscr.getch()
            if ch in (ord("q"), ord("Q")):
                break
            elif ch in (ord("r"), ord("R")):
                poller.force_refresh()
            elif ch == curses.KEY_DOWN:
                scroll_pos += 1
            elif ch == curses.KEY_UP:
                scroll_pos = max(0, scroll_pos - 1)
            elif ch == curses.KEY_MOUSE and args.bridge_url:
                try:
                    _, _mx, my, _, bstate = curses.getmouse()
                except curses.error:
                    continue
                if is_mouse_click(bstate):
                    with state["lock"]:
                        rows = list(state.get("rows", []))
                        resolved_rows = list(state.get("resolved_rows", []))
                    activate_incident_at_row(
                        my, scroll_pos, height, rows, resolved_rows, state, args
                    )
            elif ch == curses.KEY_RESIZE:
                stdscr.clear()
    finally:
        pointer_stop.set()
        if pointer_thread is not None:
            pointer_thread.join(timeout=1)
        poller.stop()


def main():
    parser = argparse.ArgumentParser(description="PagerDuty console dashboard")
    parser.add_argument(
        "--token",
        default=os.environ.get("PAGERDUTY_API_TOKEN"),
        help="PagerDuty REST API token (or set PAGERDUTY_API_TOKEN env var)",
    )
    parser.add_argument("--interval", type=int, default=30, help="Poll interval in seconds (default: 30)")
    parser.add_argument("--limit", type=int, default=50, help="Max incidents to display (default: 50)")
    parser.add_argument(
        "--status",
        action="append",
        choices=["triggered", "acknowledged", "resolved"],
        help="Incident status to include in the main list (repeatable). "
        "Default: triggered and acknowledged. The newest resolved incidents "
        "always fill the unused lines below that list.",
    )
    parser.add_argument(
        "--service-name",
        default=os.environ.get("PAGERDUTY_SERVICE_NAME"),
        help="Only show incidents for the PagerDuty service with this name "
        "(or set PAGERDUTY_SERVICE_NAME). By default, all services are shown.",
    )
    parser.add_argument(
        "--service-id",
        action="append",
        help="Only show incidents for this PagerDuty service id (repeatable). Overrides --service-name.",
    )
    parser.add_argument(
        "--no-alert-severity",
        action="store_true",
        help="Skip per-alert severity lookup; color open incidents by urgency instead (fewer API calls)",
    )
    parser.add_argument(
        "--bridge-url",
        default=os.environ.get("PAGERDUTY_BRIDGE_URL"),
        help="POST incident URLs here to open on a laptop (or set PAGERDUTY_BRIDGE_URL)",
    )
    parser.add_argument(
        "--bridge-token",
        default=os.environ.get("PAGERDUTY_BRIDGE_TOKEN"),
        help="Shared secret for the laptop bridge (or set PAGERDUTY_BRIDGE_TOKEN)",
    )
    parser.add_argument(
        "--pointer-device",
        default=os.environ.get("PAGERDUTY_POINTER_DEVICE", "auto"),
        help="Mouse/touch evdev path, or 'auto' (default). Set PAGERDUTY_POINTER_DEVICE to override.",
    )
    args = parser.parse_args()

    if not args.status:
        args.status = ["triggered", "acknowledged"]

    if not args.token:
        print("Error: no API token provided. Use --token or set PAGERDUTY_API_TOKEN.")
        sys.exit(1)

    client = PagerDutyClient(args.token)

    service_ids = None
    if args.service_id:
        service_ids = args.service_id
    elif args.service_name:
        try:
            service_ids, names = client.find_service_ids(args.service_name)
        except requests.RequestException as e:
            print(f"Error looking up service '{args.service_name}': {e}")
            sys.exit(1)
        if not service_ids:
            print(f"No PagerDuty service found matching '{args.service_name}'.")
            sys.exit(1)
        print(f"Filtering to service(s): {', '.join(names)}")

    if args.bridge_url:
        try:
            import evdev  # noqa: F401
        except ImportError:
            print("Error: evdev is required when --bridge-url is set. Install with: pip install evdev")
            sys.exit(1)
        device = resolve_pointer_device(args.pointer_device)
        if not device:
            print(
                "Error: no mouse/touch device found for clicks. Set --pointer-device or "
                "PAGERDUTY_POINTER_DEVICE to an /dev/input/event* path."
            )
            sys.exit(1)
        args.pointer_device = device
        try:
            from evdev import InputDevice

            probe = InputDevice(device)
            pointer_name = probe.name
            probe.close()
        except OSError as e:
            print(
                f"Error: cannot open pointer device {device}: {e}\n"
                "Add the service user to group 'input' (sudo usermod -aG input pi) and reboot."
            )
            sys.exit(1)
        print(f"Pointer input: {device} ({pointer_name})")

    try:
        curses.wrapper(main_curses, client, args, service_ids)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

# ----------------------------------------------------------------------------
# Optional: systemd unit to run this on boot on a headless Pi console
# ----------------------------------------------------------------------------
#
# /etc/systemd/system/pagerduty-dashboard.service
# --------------------------------------------------
# [Unit]
# Description=PagerDuty Console Dashboard
# After=network-online.target
# Wants=network-online.target
#
# [Service]
# Environment=PAGERDUTY_API_TOKEN=your_token_here
# Environment=PAGERDUTY_SERVICE_NAME=your_service_name
# ExecStart=/usr/bin/python3 /home/pi/pagerduty_dashboard.py
# StandardInput=tty
# StandardOutput=tty
# TTYPath=/dev/tty1
# Restart=always
# User=pi
#
# [Install]
# WantedBy=multi-user.target
# --------------------------------------------------
#
# Enable with:
#   sudo systemctl daemon-reload
#   sudo systemctl enable --now pagerduty-dashboard.service
