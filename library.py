"""My Library: one list of every model and code project on this computer.

It combines what the app downloaded itself with what the read-only scans
found elsewhere (the Hugging Face cache, Ollama, LM Studio, folders you
added, git clones and zip downloads). It can be exported as a spreadsheet
file (CSV) that opens in Excel, Numbers, or Google Sheets.

"Date found" is remembered in a small local file (.mrf_library.json) the
first time the app sees each item. Nothing here is sent anywhere.
"""
from __future__ import annotations

import csv
import io
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

STATE_NAME = ".mrf_library.json"
_LOCK = threading.Lock()
_PATH_OVERRIDE: Path | None = None
MAX_REMEMBERED = 20000

MODEL_SOURCE_LABELS = {
    "hf_cache": "Hugging Face cache",
    "ollama": "Ollama",
    "lmstudio": "LM Studio",
    "custom": "Your folder",
    "app": "Downloaded with this app",
    "auto": "Downloaded by the schedule",
}
REPO_SOURCE_LABELS = {
    "git": "Cloned with git",
    "zip_folder": "Unzipped download",
    "zip_file": "Zip file (not unzipped)",
    "app": "Downloaded with this app",
}

CSV_COLUMNS = [
    ("name", "Name"),
    ("type_label", "Type"),
    ("origin_label", "Downloaded by this app?"),
    ("source_label", "Where it came from"),
    ("category", "Category"),
    ("size_gb", "Size (GB)"),
    ("format", "Format"),
    ("computer", "Computer"),
    ("date_label", "Date downloaded or found"),
    ("modified_label", "Last changed"),
    ("location", "Location on this computer"),
    ("ident", "ID or link"),
]

# Cells that start like a formula are made plain text in spreadsheets.
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def set_path(path: Path | None) -> None:
    global _PATH_OVERRIDE
    _PATH_OVERRIDE = Path(path) if path else None


def current_path(root: Path | None = None) -> Path:
    if _PATH_OVERRIDE is not None:
        return _PATH_OVERRIDE
    return Path(root or Path(__file__).resolve().parent) / STATE_NAME


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return {"first_seen": {}}
    if not isinstance(data, dict) or not isinstance(data.get("first_seen"), dict):
        return {"first_seen": {}}
    return data


def _save(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def remember_first_seen(keys: Iterable[str], path: Path) -> dict[str, str]:
    """Return {key: first time seen}, recording new keys as seen now."""
    with _LOCK:
        data = _load(path)
        seen = data["first_seen"]
        now = _now_iso()
        changed = False
        out = {}
        for key in keys:
            if not key:
                continue
            if key not in seen:
                seen[key] = now
                changed = True
            out[key] = seen[key]
        if changed:
            if len(seen) > MAX_REMEMBERED:
                for old in sorted(seen, key=lambda k: seen[k])[: len(seen) - MAX_REMEMBERED]:
                    seen.pop(old, None)
            _save(path, data)
        return out


def _mtime(path_text: str) -> str:
    try:
        ts = os.stat(path_text).st_mtime
    except (OSError, ValueError, TypeError):
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _key(path_text: str) -> str:
    return os.path.normcase(os.path.normpath(str(path_text or "")))


def _format_of(name: str, fallback: str = "") -> str:
    low = (name or "").lower()
    if low.endswith(".gguf"):
        return "gguf"
    if fallback:
        return fallback
    if "mlx" in low:
        return "mlx"
    return ""


def build_rows(
    *,
    local_models: dict | None,
    local_repos: dict | None,
    app_models: list[dict],
    app_repos: list[dict],
    tracked: list[dict] | None = None,
    hf_names: dict[str, str] | None = None,
    gh_names: dict[str, str] | None = None,
    machine_names: dict[str, str] | None = None,
    state_path: Path | None = None,
) -> list[dict]:
    """Combine everything into one list (newest first)."""
    hf_names = hf_names or {}
    gh_names = gh_names or {}
    machine_names = machine_names or {}
    tracked_by_key = {}
    for row in tracked or []:
        tracked_by_key[(str(row.get("machine") or ""), str(row.get("tag") or ""),
                        str(row.get("relative_path") or row.get("name") or ""))] = row
    rows: list[dict] = []
    used_paths: set[str] = set()

    for item in app_models or []:
        key = (str(item.get("machine") or ""), str(item.get("tag") or ""),
               str(item.get("relative_path") or item.get("name") or ""))
        auto = tracked_by_key.get(key)
        name = str(item.get("name") or "")
        ident = name.replace("__", "/", 1) if "__" in name and "/" not in name else ""
        rows.append({
            "kind": "model",
            "name": name,
            "ident": (auto or {}).get("id") or ident,
            "origin": "app",
            "source": "auto" if auto else "app",
            "category_key": item.get("tag") or "",
            "category": hf_names.get(item.get("tag") or "", item.get("tag") or ""),
            "size_gb": float(item.get("size_gb") or 0),
            "format": _format_of(name, (auto or {}).get("format") or ("folder" if item.get("is_dir") else "")),
            "machine": item.get("machine") or "",
            "computer": machine_names.get(item.get("machine") or "", item.get("machine") or ""),
            "location": item.get("path") or "",
            "date": (auto or {}).get("downloaded_at") or "",
            "modified": _mtime(item.get("path") or ""),
            "auto_managed": bool(auto),
            "tag": item.get("tag") or "",
            "relative_path": item.get("relative_path") or name,
        })
        used_paths.add(_key(item.get("path") or ""))

    for item in app_repos or []:
        name = str(item.get("name") or "")
        ident = name.replace("__", "/", 1) if "__" in name else name
        rows.append({
            "kind": "repo",
            "name": ident.split("/", 1)[-1] if "/" in ident else name,
            "ident": f"https://github.com/{ident}" if "/" in ident else "",
            "repo_id": ident if "/" in ident else "",
            "origin": "app",
            "source": "app",
            "category_key": item.get("category") or "",
            "category": gh_names.get(item.get("category") or "", item.get("category") or ""),
            "size_gb": float(item.get("size_gb") or 0),
            "format": "git" if item.get("is_git") else "zip",
            "machine": "",
            "computer": "",
            "location": item.get("path") or "",
            "date": "",
            "modified": _mtime(item.get("path") or ""),
            "tag": item.get("category") or "",
            "relative_path": item.get("relative_path") or name,
        })
        used_paths.add(_key(item.get("path") or ""))

    for item in (local_models or {}).get("items") or []:
        if item.get("source") == "app" or _key(item.get("path") or "") in used_paths:
            continue
        rows.append({
            "kind": "model",
            "name": item.get("id") or item.get("name") or Path(str(item.get("path") or "")).name,
            "ident": item.get("id") or "",
            "origin": "found",
            "source": item.get("source") or "",
            "category_key": "",
            "category": "",
            "size_gb": float(item.get("size_gb") or 0),
            "format": item.get("format") or "",
            "machine": "",
            "computer": "",
            "location": item.get("path") or "",
            "date": "",
            "modified": _mtime(item.get("path") or ""),
        })
        used_paths.add(_key(item.get("path") or ""))

    for item in (local_repos or {}).get("items") or []:
        if _key(item.get("path") or "") in used_paths:
            continue
        ident = item.get("url") or ""
        rows.append({
            "kind": "repo",
            "name": item.get("id") or item.get("name") or "",
            "ident": ident,
            "repo_id": item.get("id") if item.get("is_github") else "",
            "origin": "found",
            "source": item.get("source") or "",
            "category_key": "",
            "category": "",
            "size_gb": float(item.get("size_gb") or 0),
            "size_complete": item.get("size_complete", True),
            "format": "git" if item.get("is_git") else ("zip" if item.get("source") == "zip_file" else "folder"),
            "machine": "",
            "computer": "",
            "location": item.get("path") or "",
            "date": "",
            "modified": item.get("modified") or _mtime(item.get("path") or ""),
            "branch": item.get("branch") or "",
        })
        used_paths.add(_key(item.get("path") or ""))

    if state_path is not None:
        first = remember_first_seen([_key(r["location"]) for r in rows], state_path)
        for row in rows:
            if not row.get("date"):
                row["date"] = first.get(_key(row["location"]), "")
            row["first_seen"] = first.get(_key(row["location"]), "")
    for row in rows:
        decorate(row)
    rows.sort(key=lambda r: (r.get("date") or "", r.get("name") or ""), reverse=True)
    return rows


def _date_label(iso: str) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return iso
    return dt.astimezone().strftime("%Y-%m-%d %H:%M")


def decorate(row: dict) -> dict:
    kind = row.get("kind")
    row["type_label"] = "Model" if kind == "model" else "Code project (repo)"
    row["origin_label"] = "Yes" if row.get("origin") == "app" else "No, found on this computer"
    labels = MODEL_SOURCE_LABELS if kind == "model" else REPO_SOURCE_LABELS
    row["source_label"] = labels.get(row.get("source") or "", row.get("source") or "")
    row["date_label"] = _date_label(row.get("date") or "")
    row["modified_label"] = _date_label(row.get("modified") or "")
    row["size_gb"] = round(float(row.get("size_gb") or 0), 3)
    return row


def filter_rows(rows: list[dict], *, kind: str = "", origin: str = "", text: str = "") -> list[dict]:
    kind = (kind or "").strip().lower()
    origin = (origin or "").strip().lower()
    needle = (text or "").strip().lower()
    out = []
    for row in rows:
        if kind in {"model", "repo"} and row.get("kind") != kind:
            continue
        if origin in {"app", "found"} and row.get("origin") != origin:
            continue
        if needle:
            blob = " ".join(str(row.get(k) or "") for k in (
                "name", "ident", "category", "source_label", "location", "format", "computer"
            )).lower()
            if needle not in blob:
                continue
        out.append(row)
    return out


def safe_cell(value) -> str:
    """Spreadsheet-safe text: a leading = + - @ is shown as text, not a formula."""
    if value is None:
        return ""
    if isinstance(value, float):
        text = f"{value:.3f}".rstrip("0").rstrip(".") if value else "0"
        return text
    text = str(value)
    if text.startswith(_FORMULA_START):
        return "'" + text
    return text


def to_csv_bytes(rows: list[dict]) -> bytes:
    """UTF-8 with a byte-order mark so Excel shows accents and symbols correctly."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow([label for _, label in CSV_COLUMNS])
    for row in rows:
        writer.writerow([safe_cell(row.get(key)) for key, _ in CSV_COLUMNS])
    return buf.getvalue().encode("utf-8-sig")


def export_filename(now: datetime | None = None) -> str:
    now = now or datetime.now()
    return f"ModelRepoFinder-library-{now.strftime('%Y-%m-%d')}.csv"


def summary(rows: list[dict]) -> dict:
    out = {"total": len(rows), "models": 0, "repos": 0, "from_app": 0, "found": 0, "size_gb": 0.0}
    for row in rows:
        out["models" if row.get("kind") == "model" else "repos"] += 1
        out["from_app" if row.get("origin") == "app" else "found"] += 1
        out["size_gb"] += float(row.get("size_gb") or 0)
    out["size_gb"] = round(out["size_gb"], 2)
    return out
