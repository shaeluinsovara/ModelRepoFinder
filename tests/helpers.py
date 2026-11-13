"""Shared fixtures for ModelRepoFinder unit tests. No network."""
from __future__ import annotations

import json
import shutil
import sys
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def sample_config() -> dict:
    import watcher as W

    return W.load_config(REPO / "machines.yaml")


def isolate(tmp: Path):
    """Point the app at a temp config and data folder. Returns (app, watcher, token)."""
    import app as A
    import watcher as W

    shutil.copy(REPO / "machines.yaml", tmp / "machines.yaml")
    A.CONFIG_PATH = tmp / "machines.yaml"
    A.ROOT = REPO  # web/ still lives in the real tree
    A._SESSION_BASE = str(tmp / "data")
    (tmp / "data").mkdir(parents=True, exist_ok=True)
    A._CFG_CACHE = None
    A._STATE["settings"]["machine"] = None
    A._STATE["settings"]["base_folder"] = str(tmp / "data")
    A._SCAN_DEFAULTS = False
    A._clear_local_cache()
    import seen_store
    import scheduler
    seen_store.set_path(tmp / ".mrf_seen.json")
    scheduler.set_path(tmp / ".mrf_schedule.json")
    scheduler.set_now_fn(None)
    for kind in ("refresh", "download", "github_refresh", "github_download", "top_picks", "schedule"):
        A._STATE[kind]["status"] = "idle"
        A._STATE[kind]["message"] = ""
        A._STATE[kind]["error"] = None
        A._STATE[kind]["data"] = None
        if "log" in A._STATE[kind]:
            A._STATE[kind]["log"] = []
            A._STATE[kind]["queue"] = []
            A._STATE[kind]["failed"] = []
        if kind == "schedule":
            A._STATE[kind]["mode"] = None
    token = A.reset_csrf_token()
    A._reload_cfg()
    return A, W, token


def start_server(A):
    server = ThreadingHTTPServer(("127.0.0.1", 0), A.Handler)
    server.allow_reuse_address = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    return server, port


def http_request(port, method, path, *, body=None, headers=None, host=None):
    """Issue a request, optionally overriding the Host header. Returns (status, payload, raw)."""
    conn = HTTPConnection("127.0.0.1", port, timeout=10)
    hdrs = {}
    payload = body
    if isinstance(body, dict):
        payload = json.dumps(body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
        hdrs["Content-Length"] = str(len(payload))
    elif isinstance(body, str):
        payload = body.encode("utf-8")
        hdrs["Content-Length"] = str(len(payload))
    elif isinstance(body, (bytes, bytearray)):
        payload = bytes(body)
        hdrs["Content-Length"] = str(len(payload))
    if headers:
        hdrs.update(headers)
    if host is not None:
        hdrs["Host"] = host
    try:
        conn.request(method, path, body=payload, headers=hdrs)
        resp = conn.getresponse()
        raw = resp.read()
        status = resp.status
    finally:
        conn.close()
    try:
        data = json.loads(raw.decode("utf-8")) if raw else {}
    except json.JSONDecodeError:
        data = {}
    return status, data, raw
