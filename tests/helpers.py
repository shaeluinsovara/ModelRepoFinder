"""Shared fixtures for ModelRepoFinder unit tests. No network."""
from __future__ import annotations

import json
import shutil
import sys
import threading
from http.client import HTTPConnection, RemoteDisconnected
from http.server import ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def quiet_hf_network_layer() -> None:
    """Tests never wait: no pacing, no caching between tests, no real sleeps."""
    import watcher as W

    W.HF_PACING_SECONDS = 0
    W.HF_CACHE_TTL = 0
    W._HF_SLEEP = lambda seconds: None
    W.clear_hf_cache()
    W.reset_hf_budget()
    W.REPO_SIZE_LOOKUP = False  # fake downloads never ask Hugging Face for sizes


quiet_hf_network_layer()


def app_version() -> str:
    import watcher as W

    return W.VERSION


def sample_config() -> dict:
    import watcher as W

    return W.load_config(REPO / "machines.yaml")


def fake_hardware(kind: str = "gpu") -> dict:
    """A fixed hardware profile so tests do not depend on the test machine."""
    import hardware

    if kind == "apple":
        raw = {"ram_gb": 16, "gpus": [], "apple_silicon": True, "chip": "Apple M2"}
        return hardware.build_profile(raw, system="Darwin")
    if kind == "cpu":
        return hardware.build_profile({"ram_gb": 16, "gpus": []}, system="Linux")
    raw = {
        "ram_gb": 32,
        "gpus": [hardware._gpu("NVIDIA GeForce RTX 4070", 11.99, "nvidia-smi")],
    }
    return hardware.build_profile(raw, system="Windows")


def isolate(tmp: Path, *, hardware_kind: str = "gpu"):
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
    A.set_hardware(fake_hardware(hardware_kind))
    A._clear_local_cache()
    import library
    library.set_path(tmp / ".mrf_library.json")
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


# Connection errors a client may see when the server refuses a request and
# closes the socket (Windows reports WinError 10053/10054 for these).
CONNECTION_DROPPED = (ConnectionResetError, ConnectionAbortedError, BrokenPipeError,
                      RemoteDisconnected)
REJECTED = (403, "dropped")


def http_request(port, method, path, *, body=None, headers=None, host=None,
                 expect_reject=False, attempts=3):
    """Issue a request, optionally overriding the Host header. Returns (status, payload, raw).

    With expect_reject=True the request is one the server should refuse. The
    server drains the body and answers 403 with "Connection: close", but some
    platforms can still drop the socket before the client reads the reply. In
    that case we retry a few times, and if it keeps dropping the status is
    reported as "dropped" (the server refused to process it either way).
    """
    if expect_reject:
        for _ in range(max(1, attempts)):
            try:
                return _http_once(port, method, path, body=body, headers=headers, host=host)
            except CONNECTION_DROPPED:
                continue
        return "dropped", {}, b""
    return _http_once(port, method, path, body=body, headers=headers, host=host)


def _http_once(port, method, path, *, body=None, headers=None, host=None):
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
