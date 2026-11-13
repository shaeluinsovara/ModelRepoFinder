#!/usr/bin/env python3
"""ModelRepoFinder — a tiny local web app for models and GitHub repos.

Opens in your browser. Nothing is downloaded until you click Approve & download.
Binds to 127.0.0.1 only (this computer, not the internet).
"""
from __future__ import annotations

import argparse
import json
import secrets
import sys
import threading
import traceback
import webbrowser
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

import github_finder as GH
import watcher as W

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
CONFIG_PATH = ROOT / "machines.yaml"
VERSION = W.VERSION

_LOCK = threading.Lock()
_STATE: dict = {
    "refresh": {
        "status": "idle",
        "message": "",
        "error": None,
        "data": None,
    },
    "download": {
        "status": "idle",
        "message": "",
        "error": None,
        "current": None,
        "queue": [],
        "log": [],
        "downloaded_bytes": 0,
        "total_bytes": 0,
        "percent": None,
        "failed": [],
        "failed_machine": None,
    },
    "github_refresh": {
        "status": "idle",
        "message": "",
        "error": None,
        "data": None,
    },
    "github_download": {
        "status": "idle",
        "message": "",
        "error": None,
        "current": None,
        "queue": [],
        "log": [],
        "downloaded_bytes": 0,
        "total_bytes": 0,
        "percent": None,
    },
    "settings": {
        "machine": None,
        "base_folder": "",
    },
}
_CFG_CACHE: dict | None = None
_CFG_LOCK = threading.Lock()
# Set by --base-folder so tests and one-off runs do not rewrite machines.yaml.
_SESSION_BASE: str | None = None

# Per-launch request token, injected into the served page and required on POST.
CSRF_HEADER = "X-MRF-Token"
CSRF_PLACEHOLDER = "__MRF_REQUEST_TOKEN__"
MAX_BODY = 1 * 1024 * 1024  # 1 MB
_CSRF_TOKEN = secrets.token_urlsafe(32)


class BodyTooLarge(ValueError):
    """Request body (or Content-Length) exceeded MAX_BODY."""


def reset_csrf_token(token: str | None = None) -> str:
    """Install a new (or given) per-launch token. Used at startup and in tests."""
    global _CSRF_TOKEN
    _CSRF_TOKEN = token or secrets.token_urlsafe(32)
    return _CSRF_TOKEN


def _server_port(handler) -> int:
    try:
        return int(handler.server.server_address[1])
    except Exception:  # noqa: BLE001
        return 8765


def _allowed_hosts(port: int) -> set[str]:
    return {
        f"127.0.0.1:{port}",
        f"localhost:{port}",
        f"[::1]:{port}",
        "127.0.0.1",
        "localhost",
        "[::1]",
    }


def _allowed_origins(port: int) -> set[str]:
    return {
        f"http://127.0.0.1:{port}",
        f"http://localhost:{port}",
        f"http://[::1]:{port}",
    }


def request_boundary_error(handler, *, mutating: bool) -> tuple[int, str] | None:
    """Return (status, message) if the request should be rejected."""
    port = _server_port(handler)
    host = (handler.headers.get("Host") or "").strip().lower()
    if host not in _allowed_hosts(port):
        return 403, "Rejected: unexpected Host header."
    origin = (handler.headers.get("Origin") or "").strip().rstrip("/").lower()
    if origin and origin not in {o.lower() for o in _allowed_origins(port)}:
        return 403, "Rejected: unexpected Origin header."
    if mutating:
        token = (
            handler.headers.get(CSRF_HEADER)
            or handler.headers.get("X-CSRF-Token")
            or ""
        )
        if not token or not secrets.compare_digest(str(token), str(_CSRF_TOKEN)):
            return 403, "Rejected: missing or invalid request token."
    return None


def _cfg() -> dict:
    global _CFG_CACHE
    with _CFG_LOCK:
        if _CFG_CACHE is None:
            _CFG_CACHE = W.load_config(CONFIG_PATH)
        return _CFG_CACHE


def _apply_session_base(cfg: dict) -> dict:
    if _SESSION_BASE is not None:
        paths = dict(cfg.get("paths") or {})
        paths["base"] = _SESSION_BASE
        cfg["paths"] = paths
    return cfg


def _reload_cfg() -> dict:
    global _CFG_CACHE
    with _CFG_LOCK:
        _CFG_CACHE = _apply_session_base(W.load_config(CONFIG_PATH))
        return _CFG_CACHE


def _save_cfg(cfg: dict) -> None:
    global _CFG_CACHE
    with _CFG_LOCK:
        W.save_config(CONFIG_PATH, cfg)
        _CFG_CACHE = cfg


def _persist_fields(**updates) -> dict:
    """Write selected keys to disk without letting a session base-folder leak in."""
    disk = W.load_config(CONFIG_PATH)
    for key, value in updates.items():
        disk[key] = value
        _cfg()[key] = value
    # Nested machine category updates are passed via the live cfg machines copy.
    if "machines" in updates or "custom_tags" in updates or "github_custom_tags" in updates or "tags" in updates:
        disk["machines"] = _cfg().get("machines") or disk.get("machines")
        disk["custom_tags"] = _cfg().get("custom_tags") or {}
        disk["github_custom_tags"] = _cfg().get("github_custom_tags") or {}
        disk["tags"] = _cfg().get("tags") or disk.get("tags")
        disk["github_tags"] = _cfg().get("github_tags") or disk.get("github_tags")
    W.save_config(CONFIG_PATH, disk)
    return disk


def _default_machine(cfg: dict) -> str:
    suggested = W.suggest_machine(cfg)
    machines = W.enabled_machines(cfg)
    if suggested in machines:
        return suggested
    return next(iter(machines), "pc_32gb_gpu")


def _current_machine() -> str:
    cfg = _cfg()
    with _LOCK:
        return _STATE["settings"]["machine"] or _default_machine(cfg)


def _job_public(kind: str) -> dict:
    d = _STATE[kind]
    out = {
        "status": d["status"],
        "message": d["message"],
        "error": d["error"],
        "data": d.get("data"),
    }
    if "log" in d:
        out.update(
            {
                "current": d.get("current"),
                "queue": list(d.get("queue") or []),
                "log": list((d.get("log") or [])[-40:]),
                "downloaded_bytes": d.get("downloaded_bytes") or 0,
                "total_bytes": d.get("total_bytes") or 0,
                "percent": d.get("percent"),
                "failed": list(d.get("failed") or []),
            }
        )
    return out


def _info_payload() -> dict:
    cfg = _cfg()
    machine = _current_machine()
    with _LOCK:
        base = _STATE["settings"]["base_folder"] or str(W.base_dir(cfg, ROOT))
    machines = []
    for key, m in W.enabled_machines(cfg).items():
        machines.append(
            {
                "key": key,
                "name": m.get("name") or key,
                "max_model_gb": m.get("max_model_gb"),
                "vram_gb": m.get("vram_gb") or m.get("unified_gb"),
                "formats": [f.lower() for f in (m.get("formats") or ["gguf"])],
                "note": m.get("note") or "",
            }
        )
    cats = W.category_payload(cfg, machine)
    token_set = bool(GH.load_token(ROOT))
    return {
        "app": "ModelRepoFinder",
        "version": VERSION,
        "os": W.detect_os(),
        "suggested_machine": _default_machine(cfg),
        "machine": machine,
        "base_folder": base,
        "machines": machines,
        "categories": cats,
        "allow_list": list(cfg.get("allow_list") or []),
        "github_allow_list": list(cfg.get("github_allow_list") or []),
        "github_token_set": token_set,
        "hf_token_set": bool(W.load_hf_token(ROOT)),
        "git_available": GH.git_available(),
        "refresh": _job_public("refresh"),
        "download": _job_public("download"),
        "github_refresh": _job_public("github_refresh"),
        "github_download": _job_public("github_download"),
        "installed": W.list_installed(cfg, machine=machine, root=ROOT),
        "github_installed": W.list_github_installed(cfg, root=ROOT),
    }


def _set_base_folder(path_str: str) -> None:
    path_str = (path_str or "").strip()
    cfg = _cfg()
    paths = dict(cfg.get("paths") or {})
    if path_str:
        candidate = Path(path_str).expanduser()
        try:
            candidate.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ValueError(f"Could not use that folder: {exc}") from exc
        paths["base"] = str(candidate)
        stored = str(candidate)
    else:
        paths["base"] = ""
        stored = str(W.base_dir({**cfg, "paths": paths}, ROOT))
    cfg["paths"] = paths
    disk = W.load_config(CONFIG_PATH)
    disk["paths"] = paths
    W.save_config(CONFIG_PATH, disk)
    with _LOCK:
        _STATE["settings"]["base_folder"] = stored


def _any_running() -> str | None:
    for kind in ("refresh", "download", "github_refresh", "github_download"):
        if _STATE[kind]["status"] == "running":
            return kind
    return None


def _run_refresh(machine: str, categories: list[str] | None, search_limit: int) -> None:
    def progress(msg: str) -> None:
        with _LOCK:
            _STATE["refresh"]["message"] = msg

    try:
        with _LOCK:
            _STATE["refresh"]["status"] = "running"
            _STATE["refresh"]["error"] = None
            _STATE["refresh"]["message"] = "Starting a fresh look at Hugging Face…"
        cfg = _reload_cfg()
        data = W.collect_suggestions(
            cfg,
            machine,
            progress_cb=progress,
            search_limit=search_limit,
            categories=categories,
        )
        report_name = (cfg.get("paths") or {}).get("report") or "report.md"
        try:
            W.write_report(data, ROOT / report_name, dry_run=True)
        except OSError:
            pass
        with _LOCK:
            _STATE["refresh"]["data"] = data
            _STATE["refresh"]["status"] = "done"
            _STATE["refresh"]["message"] = "Here are the models that fit. Nothing has been downloaded."
    except Exception as exc:  # noqa: BLE001
        with _LOCK:
            _STATE["refresh"]["status"] = "error"
            _STATE["refresh"]["error"] = str(exc)
            _STATE["refresh"]["message"] = "Could not fetch suggestions."
        traceback.print_exc()


def _run_downloads(machine: str, items: list[dict]) -> None:
    cfg = _reload_cfg()
    changed = False
    for item in items:
        repo_id = (item.get("id") or "").strip()
        if repo_id and W.add_to_allow_list(cfg, repo_id):
            changed = True
    if changed:
        disk = W.load_config(CONFIG_PATH)
        disk["allow_list"] = list(cfg.get("allow_list") or [])
        W.save_config(CONFIG_PATH, disk)
        cfg["allow_list"] = disk["allow_list"]

    allow = set(cfg.get("allow_list") or [])
    failed: list[dict] = []
    succeeded = 0
    with _LOCK:
        _STATE["download"]["status"] = "running"
        _STATE["download"]["error"] = None
        _STATE["download"]["failed"] = []
        _STATE["download"]["failed_machine"] = machine
        _STATE["download"]["log"].append("Added to the approve list. Starting download…")

    for item in items:
        repo_id = (item.get("id") or "").strip()
        try:
            tag = W.validate_category_key(
                cfg,
                (item.get("tag") or "misc").strip(),
                scope="hf",
                extra=W.SPECIAL_HF_CATEGORIES,
            )
            W.validate_machine_key(cfg, machine)
        except ValueError as exc:
            with _LOCK:
                _STATE["download"]["log"].append(f"Skipped {item.get('id') or 'item'}: {exc}")
            failed.append(dict(item))
            continue
        fmt = (item.get("format") or "gguf").lower()
        file_path = item.get("file_path") or item.get("file") or ""
        if not repo_id:
            continue
        if repo_id not in allow:
            with _LOCK:
                _STATE["download"]["log"].append(f"Skipped {repo_id}: not on the approve list.")
            continue
        token = W.load_hf_token(ROOT) or None
        try:
            if tag == "manual":
                dest = W.manual_dest(cfg, machine, repo_id, ROOT)
            else:
                dest = W.incoming_dir(cfg, machine, tag, ROOT)
            W.assert_contained(dest, W.incoming_root(cfg, ROOT))
        except ValueError as exc:
            with _LOCK:
                _STATE["download"]["log"].append(f"Skipped {repo_id}: {exc}")
            failed.append(dict(item))
            continue
        label = f"{repo_id} ({fmt})"
        with _LOCK:
            _STATE["download"]["current"] = {
                "id": repo_id,
                "tag": tag,
                "format": fmt,
                "file": file_path,
            }
            _STATE["download"]["message"] = f"Downloading {label}…"
            _STATE["download"]["downloaded_bytes"] = 0
            _STATE["download"]["total_bytes"] = int(item.get("size") or 0)
            _STATE["download"]["percent"] = 0
            _STATE["download"]["log"].append(f"Downloading {label} into {dest}")
        progress = _STATE["download"]
        try:
            whole = bool(item.get("whole_repo")) or file_path in {"", "(whole model)"}
            if fmt == "gguf" and file_path and file_path != "(whole model)" and not item.get("whole_repo"):
                path = W.download_gguf(repo_id, file_path, dest, progress=progress, token=token)
            else:
                ignore = [".git*"]
                if item.get("skip_bin_duplicates"):
                    ignore.append("*.bin")
                path = W.download_repo(
                    repo_id,
                    dest,
                    progress=progress,
                    token=token,
                    ignore_patterns=ignore,
                    nested=(tag != "manual"),
                )
            companions = item.get("companions") or []
            if companions and item.get("download_companions", True) and not whole:
                for comp in companions:
                    cpath = (comp.get("path") if isinstance(comp, dict) else "") or ""
                    if not cpath:
                        continue
                    role = (comp.get("role") if isinstance(comp, dict) else "") or "companion"
                    with _LOCK:
                        _STATE["download"]["log"].append(
                            f"Also downloading the matching {role} file ({cpath.rsplit('/', 1)[-1]})…"
                        )
                    cdest = W.download_gguf(repo_id, cpath, dest, progress=progress, token=token)
                    with _LOCK:
                        _STATE["download"]["log"].append(f"Saved companion {cdest}")
            with _LOCK:
                _STATE["download"]["log"].append(f"Saved {path}")
                _STATE["download"]["percent"] = 100
                _STATE["download"]["message"] = f"Finished {label}"
            succeeded += 1
        except Exception as exc:  # noqa: BLE001
            failed.append(dict(item))
            with _LOCK:
                _STATE["download"]["log"].append(f"Failed {label}: {exc}")
                _STATE["download"]["error"] = str(exc)
                _STATE["download"]["failed"] = list(failed)
            traceback.print_exc()
            # Keep going through the rest of the batch.
            continue

    with _LOCK:
        _STATE["download"]["failed"] = list(failed)
        _STATE["download"]["failed_machine"] = machine
        if failed:
            _STATE["download"]["status"] = "error"
            if succeeded and failed:
                _STATE["download"]["message"] = (
                    f"{succeeded} finished, {len(failed)} had a problem. "
                    "Click Retry failed to try again — you do not need to close the app."
                )
            else:
                _STATE["download"]["message"] = (
                    "That download had a problem after a few tries. "
                    "Click Retry failed to try again — you do not need to close the app."
                )
        else:
            _STATE["download"]["status"] = "done"
            _STATE["download"]["message"] = "All requested downloads finished."
            _STATE["download"]["error"] = None
        _STATE["download"]["current"] = None
        _STATE["download"]["queue"] = []


def _run_github_refresh(machine: str, categories: list[str] | None, per_page: int) -> None:
    def progress(msg: str) -> None:
        with _LOCK:
            _STATE["github_refresh"]["message"] = msg

    try:
        with _LOCK:
            _STATE["github_refresh"]["status"] = "running"
            _STATE["github_refresh"]["error"] = None
            _STATE["github_refresh"]["message"] = "Starting a fresh look at GitHub…"
        cfg = _reload_cfg()
        token = GH.load_token(ROOT) or None
        data = GH.collect_github_suggestions(
            cfg,
            machine,
            token=token,
            progress_cb=progress,
            categories=categories,
            per_page=per_page,
        )
        with _LOCK:
            _STATE["github_refresh"]["data"] = data
            _STATE["github_refresh"]["status"] = "done"
            _STATE["github_refresh"]["message"] = "Here are the repositories. Nothing has been downloaded."
    except Exception as exc:  # noqa: BLE001
        with _LOCK:
            _STATE["github_refresh"]["status"] = "error"
            _STATE["github_refresh"]["error"] = str(exc)
            _STATE["github_refresh"]["message"] = "Could not fetch GitHub suggestions."
        traceback.print_exc()


def _run_github_downloads(items: list[dict], action: str = "download") -> None:
    cfg = _reload_cfg()
    changed = False
    for item in items:
        repo_id = (item.get("id") or "").strip()
        if repo_id and W.add_to_allow_list(cfg, repo_id, key="github_allow_list"):
            changed = True
    if changed:
        disk = W.load_config(CONFIG_PATH)
        disk["github_allow_list"] = list(cfg.get("github_allow_list") or [])
        W.save_config(CONFIG_PATH, disk)
        cfg["github_allow_list"] = disk["github_allow_list"]

    allow = set(cfg.get("github_allow_list") or [])
    verb = "Updating" if action == "update" else "Downloading"
    with _LOCK:
        _STATE["github_download"]["status"] = "running"
        _STATE["github_download"]["error"] = None
        _STATE["github_download"]["log"].append(f"{verb} approved GitHub repos…")

    for item in items:
        repo_id = (item.get("id") or "").strip()
        tag = (item.get("tag") or item.get("category") or "misc").strip()
        branch = item.get("default_branch") or "main"
        if not repo_id:
            continue
        if repo_id not in allow:
            with _LOCK:
                _STATE["github_download"]["log"].append(f"Skipped {repo_id}: not on the approve list.")
            continue
        try:
            tag = W._safe_component(tag, "category")
            dest = W.github_incoming_dir(cfg, tag, ROOT)
            W.assert_contained(dest, W.incoming_root(cfg, ROOT))
        except ValueError as exc:
            with _LOCK:
                _STATE["github_download"]["log"].append(f"Skipped {repo_id}: {exc}")
            continue
        with _LOCK:
            _STATE["github_download"]["current"] = {"id": repo_id, "tag": tag}
            _STATE["github_download"]["message"] = f"{verb} {repo_id}…"
            _STATE["github_download"]["downloaded_bytes"] = 0
            _STATE["github_download"]["total_bytes"] = 0
            _STATE["github_download"]["percent"] = 0
            _STATE["github_download"]["log"].append(f"{verb} {repo_id} into {dest}")
        progress = _STATE["github_download"]
        try:
            if action == "update":
                path = GH.update_repo(repo_id, dest, default_branch=branch, progress=progress)
            else:
                path = GH.clone_or_zip(repo_id, dest, default_branch=branch, progress=progress)
            with _LOCK:
                _STATE["github_download"]["log"].append(f"Saved {path}")
                _STATE["github_download"]["percent"] = 100
                _STATE["github_download"]["message"] = f"Finished {repo_id}"
        except Exception as exc:  # noqa: BLE001
            with _LOCK:
                _STATE["github_download"]["log"].append(f"Failed {repo_id}: {exc}")
                _STATE["github_download"]["error"] = str(exc)
            traceback.print_exc()
            continue

    with _LOCK:
        if _STATE["github_download"]["error"] and _STATE["github_download"]["status"] == "running":
            _STATE["github_download"]["status"] = "error"
            _STATE["github_download"]["message"] = "One or more GitHub downloads had a problem. See the log."
        else:
            _STATE["github_download"]["status"] = "done"
            _STATE["github_download"]["message"] = "All requested GitHub downloads finished."
        _STATE["github_download"]["current"] = None
        _STATE["github_download"]["queue"] = []


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB), **kwargs)

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self) -> None:  # noqa: N802
        denied = request_boundary_error(self, mutating=False)
        if denied:
            return self._json(denied[0], {"error": denied[1]})
        parsed = self.path.split("?", 1)[0]
        if parsed in {"/", "/index.html"}:
            return self._serve_index()
        if parsed == "/api/info":
            return self._json(200, _info_payload())
        if parsed == "/api/job":
            machine = _current_machine()
            with _LOCK:
                payload = {
                    "refresh": _job_public("refresh"),
                    "download": _job_public("download"),
                    "github_refresh": _job_public("github_refresh"),
                    "github_download": _job_public("github_download"),
                    "installed": W.list_installed(_cfg(), machine=machine, root=ROOT),
                    "github_installed": W.list_github_installed(_cfg(), root=ROOT),
                }
            return self._json(200, payload)
        return super().do_GET()

    def _serve_index(self) -> None:
        path = WEB / "index.html"
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            return self._json(500, {"error": f"Could not read the app page: {exc}"})
        text = text.replace(CSRF_PLACEHOLDER, _CSRF_TOKEN)
        blob = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(blob)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(blob)

    def do_POST(self) -> None:  # noqa: N802
        denied = request_boundary_error(self, mutating=True)
        if denied:
            # If Content-Length is huge, close rather than hanging on a body we will not use.
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length > MAX_BODY:
                self.close_connection = True
            return self._json(denied[0], {"error": denied[1]})
        parsed = self.path.split("?", 1)[0]
        try:
            body = self._read_json()
        except BodyTooLarge as exc:
            self.close_connection = True
            return self._json(413, {"error": str(exc) or "Request body is too large."})
        except ValueError as exc:
            return self._json(400, {"error": str(exc)})
        routes = {
            "/api/settings": self._handle_settings,
            "/api/refresh": self._handle_refresh,
            "/api/download": self._handle_download,
            "/api/download/retry": self._handle_download_retry,
            "/api/retire": self._handle_retire,
            "/api/categories": self._handle_categories,
            "/api/hf/inspect": self._handle_hf_inspect,
            "/api/github/refresh": self._handle_github_refresh,
            "/api/github/download": self._handle_github_download,
            "/api/github/update": self._handle_github_update,
            "/api/github/retire": self._handle_github_retire,
        }
        handler = routes.get(parsed)
        if not handler:
            return self._json(404, {"error": "Unknown action."})
        return handler(body)

    def _read_json(self) -> dict:
        raw_len = self.headers.get("Content-Length")
        if not raw_len:
            return {}
        try:
            length = int(raw_len)
        except ValueError as exc:
            raise ValueError("Invalid Content-Length.") from exc
        if length < 0:
            raise ValueError("Invalid Content-Length.")
        if length > MAX_BODY:
            raise BodyTooLarge(
                f"Request body is too large (limit is {MAX_BODY} bytes)."
            )
        raw = self.rfile.read(length) if length else b"{}"
        if len(raw) > MAX_BODY:
            raise BodyTooLarge(
                f"Request body is too large (limit is {MAX_BODY} bytes)."
            )
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("Expected a JSON object.")
        return data

    def _json(self, status: int, payload: dict) -> None:
        blob = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def _handle_settings(self, body: dict) -> None:
        cfg = _cfg()
        machine = str(body.get("machine") or _current_machine())
        if body.get("machine"):
            if machine not in W.enabled_machines(cfg):
                return self._json(400, {"error": f"Unknown machine: {machine}"})
            with _LOCK:
                _STATE["settings"]["machine"] = machine
        if "base_folder" in body:
            try:
                _set_base_folder(str(body.get("base_folder") or ""))
            except ValueError as exc:
                return self._json(400, {"error": str(exc)})
        if "hf_categories" in body:
            try:
                W.set_machine_categories(cfg, machine, "hf", list(body.get("hf_categories") or []))
            except ValueError as exc:
                return self._json(400, {"error": str(exc)})
            _persist_fields(machines=cfg.get("machines"))
        if "github_categories" in body:
            try:
                W.set_machine_categories(cfg, machine, "github", list(body.get("github_categories") or []))
            except ValueError as exc:
                return self._json(400, {"error": str(exc)})
            _persist_fields(machines=cfg.get("machines"))
        if "github_token" in body:
            # Empty string clears a saved token.
            GH.save_token(str(body.get("github_token") or ""), ROOT)
        if "hf_token" in body:
            W.save_hf_token(str(body.get("hf_token") or ""), ROOT)
        if "keep" in body and body.get("category") and body.get("scope"):
            key = str(body["category"])
            scope = str(body["scope"])
            try:
                keep = max(1, min(int(body["keep"]), 10))
            except (TypeError, ValueError):
                return self._json(400, {"error": "Keep count must be a number from 1 to 10."})
            bucket = "custom_tags" if scope == "hf" and key in (cfg.get("custom_tags") or {}) else None
            if bucket is None and scope == "github" and key in (cfg.get("github_custom_tags") or {}):
                bucket = "github_custom_tags"
            if bucket:
                cfg[bucket][key]["keep"] = keep
                _persist_fields(**{bucket: cfg[bucket]})
            elif scope == "hf" and key in (cfg.get("tags") or {}):
                cfg["tags"][key]["keep"] = keep
                _persist_fields(tags=cfg["tags"])
            elif scope == "github" and key in (cfg.get("github_tags") or {}):
                cfg["github_tags"][key]["keep"] = keep
                _persist_fields(**{"github_tags": cfg["github_tags"]})
            else:
                return self._json(400, {"error": "Unknown category."})
        return self._json(200, _info_payload())

    def _handle_categories(self, body: dict) -> None:
        action = str(body.get("action") or "add")
        scope = str(body.get("scope") or "hf")
        if scope not in {"hf", "github"}:
            return self._json(400, {"error": "Scope must be hf or github."})
        cfg = _cfg()
        machine = str(body.get("machine") or _current_machine())
        try:
            if action == "add":
                slug = W.add_custom_category(
                    cfg,
                    scope,
                    name=str(body.get("name") or ""),
                    search=str(body.get("search") or ""),
                    keep=int(body.get("keep") or 2),
                    allow_nongguf=bool(body.get("allow_nongguf")),
                )
                field = "hf_categories" if scope == "hf" else "github_categories"
                if machine in (cfg.get("machines") or {}):
                    current = list((cfg["machines"][machine].get(field) or []))
                    if slug not in current:
                        current.append(slug)
                        cfg["machines"][machine][field] = current
                _persist_fields(
                    machines=cfg.get("machines"),
                    custom_tags=cfg.get("custom_tags"),
                    github_custom_tags=cfg.get("github_custom_tags"),
                )
            elif action in {"remove", "delete"}:
                W.remove_custom_category(cfg, scope, str(body.get("key") or body.get("category") or ""))
                _persist_fields(
                    machines=cfg.get("machines"),
                    custom_tags=cfg.get("custom_tags"),
                    github_custom_tags=cfg.get("github_custom_tags"),
                )
            else:
                return self._json(400, {"error": "Unknown category action."})
        except ValueError as exc:
            return self._json(400, {"error": str(exc)})
        return self._json(200, _info_payload())

    def _handle_hf_inspect(self, body: dict) -> None:
        raw = str(body.get("url") or body.get("id") or "")
        machine = str(body.get("machine") or _current_machine())
        cfg = _cfg()
        cap = None
        machines = W.enabled_machines(cfg)
        if machine in machines:
            try:
                cap = float(machines[machine].get("max_model_gb") or 0) or None
            except (TypeError, ValueError):
                cap = None
        try:
            repo_id = W.parse_hf_input(raw)
            data = W.inspect_hf_repo(repo_id, token=W.load_hf_token(ROOT) or None, max_gb=cap)
        except ValueError as exc:
            return self._json(400, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            return self._json(500, {"error": f"Could not check that model: {exc}"})
        return self._json(200, data)

    def _handle_refresh(self, body: dict) -> None:
        cfg = _cfg()
        machine = str(body.get("machine") or _current_machine())
        if machine not in W.enabled_machines(cfg):
            return self._json(400, {"error": f"Unknown machine: {machine}"})
        categories = body.get("categories")
        if categories is not None and not isinstance(categories, list):
            return self._json(400, {"error": "categories must be a list."})
        search_limit = int(body.get("search_limit") or 40)
        search_limit = max(5, min(search_limit, 40))
        if categories is not None and not categories:
            return self._json(400, {"error": "Tick at least one category to search."})
        with _LOCK:
            busy = _any_running()
            if busy:
                return self._json(409, {"error": "Please wait for the current task to finish."})
            _STATE["settings"]["machine"] = machine
            _STATE["refresh"]["status"] = "running"
            _STATE["refresh"]["message"] = "Starting…"
            _STATE["refresh"]["error"] = None
        threading.Thread(
            target=_run_refresh,
            args=(machine, categories, search_limit),
            daemon=True,
        ).start()
        return self._json(200, {"ok": True, "refresh": _job_public("refresh")})

    def _handle_download(self, body: dict) -> None:
        cfg = _cfg()
        machine = str(body.get("machine") or _current_machine())
        try:
            machine = W.validate_machine_key(cfg, machine, must_be_enabled=True)
        except ValueError as exc:
            return self._json(400, {"error": str(exc)})
        items = body.get("items") or []
        if not isinstance(items, list) or not items:
            return self._json(400, {"error": "Pick at least one model to approve and download."})
        clean = []
        for item in items:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            try:
                tag = W.validate_category_key(
                    cfg,
                    str(item.get("tag") or "misc"),
                    scope="hf",
                    extra=W.SPECIAL_HF_CATEGORIES,
                )
            except ValueError as exc:
                return self._json(400, {"error": str(exc)})
            companions = item.get("companions") or []
            if companions and not isinstance(companions, list):
                return self._json(400, {"error": "companions must be a list."})
            clean_comps = []
            for comp in companions:
                if not isinstance(comp, dict) or not comp.get("path"):
                    continue
                # Companion paths are Hugging Face filenames, not local folder names.
                # Still reject absolute / traversal forms so they cannot be misused.
                cpath = str(comp["path"])
                if Path(cpath).is_absolute() or ".." in Path(cpath).parts:
                    return self._json(400, {"error": "That companion file path is not allowed."})
                clean_comps.append(
                    {
                        "path": cpath,
                        "size": comp.get("size") or 0,
                        "size_gb": comp.get("size_gb") or 0,
                        "role": str(comp.get("role") or "companion"),
                    }
                )
            clean.append(
                {
                    "id": str(item["id"]),
                    "tag": tag,
                    "format": str(item.get("format") or "gguf"),
                    "file": str(item.get("file") or ""),
                    "file_path": str(item.get("file_path") or item.get("file") or ""),
                    "size": item.get("size") or 0,
                    "whole_repo": bool(item.get("whole_repo")),
                    "skip_bin_duplicates": bool(item.get("skip_bin_duplicates")),
                    "companions": clean_comps,
                    "download_companions": bool(item.get("download_companions", True)),
                }
            )
        if not clean:
            return self._json(400, {"error": "Pick at least one model to approve and download."})
        with _LOCK:
            if _any_running():
                return self._json(409, {"error": "Please wait for the current task to finish."})
            _STATE["settings"]["machine"] = machine
            _STATE["download"]["status"] = "running"
            _STATE["download"]["queue"] = [c["id"] for c in clean]
            _STATE["download"]["error"] = None
            _STATE["download"]["message"] = "Preparing…"
            _STATE["download"]["log"] = []
        threading.Thread(target=_run_downloads, args=(machine, clean), daemon=True).start()
        return self._json(200, {"ok": True, "download": _job_public("download")})

    def _handle_download_retry(self, body: dict) -> None:
        with _LOCK:
            items = list(_STATE["download"].get("failed") or [])
            machine = str(
                body.get("machine")
                or _STATE["download"].get("failed_machine")
                or _current_machine()
            )
        if not items:
            return self._json(400, {"error": "There is nothing failed to retry."})
        # Reuse the regular download path so allow-list and dest folders stay the same.
        return self._handle_download({"machine": machine, "items": items})

    def _handle_retire(self, body: dict) -> None:
        cfg = _cfg()
        machine = str(body.get("machine") or "")
        tag = str(body.get("tag") or "")
        relative = str(body.get("relative_path") or body.get("name") or "")
        if not (machine and tag and relative):
            return self._json(400, {"error": "Need a machine, tag, and file to retire."})
        if not (cfg.get("keep_retired", True)):
            return self._json(400, {"error": "Retiring is turned off in the settings file."})
        try:
            dest = W.retire_item(cfg, machine, tag, relative, ROOT)
        except (ValueError, FileNotFoundError) as exc:
            return self._json(400, {"error": str(exc)})
        except OSError as exc:
            return self._json(500, {"error": f"Could not move the file: {exc}"})
        return self._json(
            200,
            {
                "ok": True,
                "retired_to": str(dest),
                "installed": W.list_installed(cfg, machine=machine, root=ROOT),
            },
        )

    def _handle_github_refresh(self, body: dict) -> None:
        cfg = _cfg()
        machine = str(body.get("machine") or _current_machine())
        if machine not in W.enabled_machines(cfg):
            return self._json(400, {"error": f"Unknown machine: {machine}"})
        categories = body.get("categories")
        if categories is not None and not isinstance(categories, list):
            return self._json(400, {"error": "categories must be a list."})
        per_page = int(body.get("per_page") or 20)
        per_page = max(5, min(per_page, 30))
        if categories is not None and not categories:
            return self._json(400, {"error": "Tick at least one category to search."})
        with _LOCK:
            if _any_running():
                return self._json(409, {"error": "Please wait for the current task to finish."})
            _STATE["settings"]["machine"] = machine
            _STATE["github_refresh"]["status"] = "running"
            _STATE["github_refresh"]["message"] = "Starting…"
            _STATE["github_refresh"]["error"] = None
        threading.Thread(
            target=_run_github_refresh,
            args=(machine, categories, per_page),
            daemon=True,
        ).start()
        return self._json(200, {"ok": True, "github_refresh": _job_public("github_refresh")})

    def _handle_github_download(self, body: dict) -> None:
        cfg = _cfg()
        items = body.get("items") or []
        if not isinstance(items, list) or not items:
            return self._json(400, {"error": "Pick at least one repository to approve and download."})
        clean = []
        for item in items:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            try:
                tag = W.validate_category_key(
                    cfg,
                    str(item.get("tag") or item.get("category") or "misc"),
                    scope="github",
                    extra=W.SPECIAL_GH_CATEGORIES,
                )
            except ValueError as exc:
                return self._json(400, {"error": str(exc)})
            clean.append(
                {
                    "id": str(item["id"]),
                    "tag": tag,
                    "default_branch": str(item.get("default_branch") or "main"),
                }
            )
        if not clean:
            return self._json(400, {"error": "Pick at least one repository to approve and download."})
        with _LOCK:
            if _any_running():
                return self._json(409, {"error": "Please wait for the current task to finish."})
            _STATE["github_download"]["status"] = "running"
            _STATE["github_download"]["queue"] = [c["id"] for c in clean]
            _STATE["github_download"]["error"] = None
            _STATE["github_download"]["message"] = "Preparing…"
            _STATE["github_download"]["log"] = []
        threading.Thread(target=_run_github_downloads, args=(clean, "download"), daemon=True).start()
        return self._json(200, {"ok": True, "github_download": _job_public("github_download")})

    def _handle_github_update(self, body: dict) -> None:
        repo_id = str(body.get("id") or "")
        tag = str(body.get("tag") or body.get("category") or "")
        if not (repo_id and tag):
            # Allow updating from an installed folder name like owner__repo.
            name = str(body.get("name") or "")
            if name and tag and "__" in name:
                repo_id = name.replace("__", "/", 1)
            elif name and tag and "/" in name:
                repo_id = name
        if not (repo_id and tag):
            return self._json(400, {"error": "Need a repository and a category to update."})
        try:
            tag = W._safe_component(tag, "category")
        except ValueError as exc:
            return self._json(400, {"error": str(exc)})
        item = {
            "id": repo_id,
            "tag": tag,
            "default_branch": str(body.get("default_branch") or "main"),
        }
        with _LOCK:
            if _any_running():
                return self._json(409, {"error": "Please wait for the current task to finish."})
            _STATE["github_download"]["status"] = "running"
            _STATE["github_download"]["queue"] = [repo_id]
            _STATE["github_download"]["error"] = None
            _STATE["github_download"]["message"] = "Preparing update…"
            _STATE["github_download"]["log"] = []
        threading.Thread(target=_run_github_downloads, args=([item], "update"), daemon=True).start()
        return self._json(200, {"ok": True, "github_download": _job_public("github_download")})

    def _handle_github_retire(self, body: dict) -> None:
        cfg = _cfg()
        tag = str(body.get("tag") or body.get("category") or "")
        relative = str(body.get("relative_path") or body.get("name") or "")
        if not (tag and relative):
            return self._json(400, {"error": "Need a category and a folder to retire."})
        if not (cfg.get("keep_retired", True)):
            return self._json(400, {"error": "Retiring is turned off in the settings file."})
        try:
            dest = W.retire_github_item(cfg, tag, relative, ROOT)
        except (ValueError, FileNotFoundError) as exc:
            return self._json(400, {"error": str(exc)})
        except OSError as exc:
            return self._json(500, {"error": f"Could not move the folder: {exc}"})
        return self._json(
            200,
            {
                "ok": True,
                "retired_to": str(dest),
                "github_installed": W.list_github_installed(cfg, root=ROOT),
            },
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="ModelRepoFinder")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true", help="Do not open a browser window")
    parser.add_argument("--config", type=Path, default=ROOT / "machines.yaml")
    parser.add_argument("--base-folder", type=str, default=None, help="Override where files are saved")
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except Exception:
        pass

    global CONFIG_PATH
    CONFIG_PATH = args.config

    if not CONFIG_PATH.exists():
        print(f"Could not find the settings file: {CONFIG_PATH}", file=sys.stderr)
        return 1
    if not (WEB / "index.html").exists():
        print(f"Could not find the web page at {WEB / 'index.html'}", file=sys.stderr)
        return 1

    cfg = _reload_cfg()
    # Fill in any new built-in fields without wiping the user's existing choices.
    packaged = ROOT / "machines.yaml"
    if W.migrate_config(cfg, default_path=packaged):
        try:
            # Only persist migration onto the real config file, not a session copy.
            if CONFIG_PATH.resolve() == packaged.resolve() or args.config:
                disk = W.load_config(CONFIG_PATH)
                if W.migrate_config(disk, default_path=packaged):
                    W.save_config(CONFIG_PATH, disk)
                    _reload_cfg()
                    cfg = _cfg()
        except OSError:
            pass

    with _LOCK:
        _STATE["settings"]["machine"] = _default_machine(cfg)
        _STATE["settings"]["base_folder"] = str(W.base_dir(cfg, ROOT))
    if args.base_folder is not None:
        try:
            candidate = Path(args.base_folder).expanduser()
            candidate.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(f"Could not use that folder: {exc}", file=sys.stderr)
            return 1
        global _SESSION_BASE
        _SESSION_BASE = str(candidate)
        cfg = _reload_cfg()
        with _LOCK:
            _STATE["settings"]["base_folder"] = str(candidate)

    host = "127.0.0.1"
    port = args.port
    reset_csrf_token()
    try:
        server = ThreadingHTTPServer((host, port), Handler)
    except OSError as exc:
        if port == 8765:
            server = ThreadingHTTPServer((host, 0), Handler)
            port = server.server_address[1]
        else:
            print(f"Could not start on port {port}: {exc}", file=sys.stderr)
            return 1

    url = f"http://{host}:{port}/"
    print()
    print(f"  ModelRepoFinder v{VERSION} is ready.")
    print(f"  Open this page: {url}")
    print("  Leave this window open while you use it.")
    print("  Close this window (or press Ctrl+C) when you are done.")
    print()
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            print("  (Could not open the browser automatically. Please open the link above.)")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down. Goodbye.")
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
