#!/usr/bin/env python3
"""
PagerDuty laptop bridge
=======================
Small HTTP server for your Linux laptop. The Pi dashboard POSTs a PagerDuty
incident URL here; this process opens it in the default browser.

Stdlib only — no pip dependencies.

Usage:
    python3 pagerduty_laptop_bridge.py --host 0.0.0.0 --port 8765

Optional shared secret (set on Pi and laptop):
    export PAGERDUTY_BRIDGE_TOKEN=your_random_secret
    python3 pagerduty_laptop_bridge.py --token "$PAGERDUTY_BRIDGE_TOKEN"
"""

import argparse
import json
import logging
import os
import sys
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

LOG = logging.getLogger("pagerduty_laptop_bridge")

ALLOWED_HOST_SUFFIX = ".pagerduty.com"


def is_allowed_pagerduty_url(url):
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    if host == "pagerduty.com" or host.endswith(ALLOWED_HOST_SUFFIX):
        return True
    return False


class BridgeHandler(BaseHTTPRequestHandler):
    server_version = "PagerDutyLaptopBridge/1.0"
    expected_token = None

    def log_message(self, format, *args):
        LOG.info("%s - %s", self.address_string(), format % args)

    def _send_json(self, status, body):
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _auth_ok(self):
        token = self.expected_token
        if not token:
            return True
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer ") and auth[7:] == token:
            return True
        if self.headers.get("X-Bridge-Token") == token:
            return True
        return False

    def _post_path_ok(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        return path in ("/", "/open")

    def do_POST(self):
        if not self._post_path_ok():
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        if not self._auth_ok():
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0 or length > 8192:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid body"})
            return
        try:
            body = self.rfile.read(length)
            payload = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid json"})
            return
        url = payload.get("url")
        if not url or not isinstance(url, str):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "missing url"})
            return
        if not is_allowed_pagerduty_url(url):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "url not allowed"})
            return
        LOG.info("open %s from %s", url, self.client_address[0])
        webbrowser.open(url)
        self.send_response(HTTPStatus.NO_CONTENT)
        self.end_headers()

    def do_GET(self):
        if self.path.rstrip("/") in ("", "/"):
            self._send_json(HTTPStatus.OK, {"service": "pagerduty_laptop_bridge", "post": "/open"})
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})


def main():
    parser = argparse.ArgumentParser(description="Open PagerDuty incident URLs from the Pi dashboard")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (use 0.0.0.0 for LAN)")
    parser.add_argument("--port", type=int, default=8765, help="Listen port (default: 8765)")
    parser.add_argument(
        "--token",
        default=os.environ.get("PAGERDUTY_BRIDGE_TOKEN"),
        help="Shared secret (or set PAGERDUTY_BRIDGE_TOKEN)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    BridgeHandler.expected_token = args.token
    server = ThreadingHTTPServer((args.host, args.port), BridgeHandler)
    LOG.info("listening on http://%s:%s/open", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOG.info("shutting down")
        server.shutdown()
        return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
