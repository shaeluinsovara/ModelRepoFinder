#!/usr/bin/env python3
"""Remember which suggestions were already shown, so Refresh can offer a new batch.

State is stored only on this computer, in a local JSON file. It is never sent
anywhere. Lists are capped so the file cannot grow without bound.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Callable, Iterable

# Default filename next to the app. Tests and the server may point this elsewhere.
DEFAULT_NAME = ".mrf_seen.json"
MAX_IDS_PER_BUCKET = 2000
SCOPES = frozenset({"hf", "github", "top_picks"})

_LOCK = threading.Lock()
_PATH: Path | None = None


def default_path(root: Path | None = None) -> Path:
    if root is None:
        root = Path(__file__).resolve().parent
    return Path(root) / DEFAULT_NAME


def set_path(path: Path | None) -> Path:
    """Point the store at a file. Used at startup and by tests."""
    global _PATH
    _PATH = Path(path) if path is not None else None
    return current_path()


def current_path(root: Path | None = None) -> Path:
    return _PATH if _PATH is not None else default_path(root)


def _empty() -> dict:
    return {"version": 1, "hf": {}, "github": {}, "top_picks": {}}


def _safe_key(value: str, kind: str = "key") -> str:
    """Reuse the same folder-name rules so machine/category keys cannot wander."""
    # Imported lazily to avoid a circular import at module load.
    import watcher as W

    return W._safe_component(value, kind)


def load(path: Path | None = None) -> dict:
    path = Path(path) if path is not None else current_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _empty()
    except OSError:
        return _empty()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return _empty()
    if not isinstance(data, dict):
        return _empty()
    out = _empty()
    for scope in SCOPES:
        bucket = data.get(scope)
        if isinstance(bucket, dict):
            out[scope] = bucket
    return out


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    payload = json.dumps(data, indent=2, sort_keys=False, ensure_ascii=False)
    tmp.write_text(payload + "\n", encoding="utf-8")
    try:
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def save(data: dict, path: Path | None = None) -> None:
    path = Path(path) if path is not None else current_path()
    _atomic_write(path, data)


def _bucket_list(data: dict, scope: str, machine: str, category: str) -> list[str]:
    scope_map = data.setdefault(scope, {})
    machine_map = scope_map.setdefault(machine, {})
    ids = machine_map.get(category)
    if not isinstance(ids, list):
        ids = []
        machine_map[category] = ids
    return ids


def get_seen(
    scope: str,
    machine: str,
    category: str,
    path: Path | None = None,
) -> list[str]:
    """Return already-shown IDs for one machine and category."""
    if scope not in SCOPES:
        raise ValueError("Unknown seen-list scope.")
    machine = _safe_key(machine, "computer")
    category = _safe_key(category, "category")
    with _LOCK:
        data = load(path)
        ids = _bucket_list(data, scope, machine, category)
        return [i for i in ids if isinstance(i, str) and i]


def add_seen(
    scope: str,
    machine: str,
    category: str,
    ids: Iterable[str],
    path: Path | None = None,
) -> list[str]:
    """Append IDs to the seen list. Duplicates are ignored. Returns the new list."""
    if scope not in SCOPES:
        raise ValueError("Unknown seen-list scope.")
    machine = _safe_key(machine, "computer")
    category = _safe_key(category, "category")
    incoming = [i.strip() for i in ids if isinstance(i, str) and i.strip()]
    with _LOCK:
        data = load(path)
        current = _bucket_list(data, scope, machine, category)
        have = set(current)
        for item in incoming:
            if item not in have:
                current.append(item)
                have.add(item)
        if len(current) > MAX_IDS_PER_BUCKET:
            current[:] = current[-MAX_IDS_PER_BUCKET:]
        data[scope][machine][category] = current
        save(data, path)
        return list(current)


def set_seen(
    scope: str,
    machine: str,
    category: str,
    ids: Iterable[str],
    path: Path | None = None,
) -> list[str]:
    """Replace the seen list for one bucket (used after a wrap-around)."""
    if scope not in SCOPES:
        raise ValueError("Unknown seen-list scope.")
    machine = _safe_key(machine, "computer")
    category = _safe_key(category, "category")
    incoming = [i.strip() for i in ids if isinstance(i, str) and i.strip()]
    # Keep order, drop duplicates.
    cleaned: list[str] = []
    have: set[str] = set()
    for item in incoming:
        if item not in have:
            cleaned.append(item)
            have.add(item)
    if len(cleaned) > MAX_IDS_PER_BUCKET:
        cleaned = cleaned[-MAX_IDS_PER_BUCKET:]
    with _LOCK:
        data = load(path)
        _bucket_list(data, scope, machine, category)
        data[scope][machine][category] = cleaned
        save(data, path)
        return list(cleaned)


def reset(
    scope: str | None = "all",
    machine: str | None = None,
    category: str | None = None,
    path: Path | None = None,
) -> dict:
    """Clear seen IDs. Narrow with scope, machine, and/or category.

    scope='all' (the default) clears every list. Passing a machine without a
    category clears that computer's lists in the given scope.
    """
    if scope not in SCOPES | {"all", None}:
        raise ValueError("Unknown seen-list scope.")
    if machine:
        machine = _safe_key(machine, "computer")
    if category:
        category = _safe_key(category, "category")
    scopes = SCOPES if scope in {None, "all"} else {scope}
    with _LOCK:
        data = load(path)
        for sc in scopes:
            if machine is None:
                data[sc] = {}
                continue
            machine_map = data.get(sc) or {}
            if machine not in machine_map:
                continue
            if category is None:
                machine_map[machine] = {}
            else:
                bucket = machine_map.get(machine) or {}
                bucket.pop(category, None)
                machine_map[machine] = bucket
            data[sc] = machine_map
        save(data, path)
        return data


def pick_fresh(
    candidates: list,
    *,
    id_of: Callable[[object], str],
    seen: Iterable[str],
    exclude: Iterable[str],
    keep: int,
) -> tuple[list, dict]:
    """Pick the next `keep` items, skipping ones already shown or excluded.

    `candidates` should already be in the preferred order (most downloaded,
    most liked, and so on). If every remaining candidate has already been
    shown, the list wraps around and `wrapped` is True so the caller can
    reset the seen list to just the new batch.
    """
    keep = max(0, int(keep))
    seen_set = {s for s in seen if s}
    exclude_set = {s for s in exclude if s}
    fresh: list = []
    wrapped_pool: list = []
    skipped_seen = 0
    skipped_excluded = 0
    for item in candidates:
        ident = id_of(item)
        if not ident:
            continue
        if ident in exclude_set:
            skipped_excluded += 1
            continue
        if ident in seen_set:
            skipped_seen += 1
            wrapped_pool.append(item)
            continue
        fresh.append(item)

    wrapped = False
    if len(fresh) >= keep:
        picked = fresh[:keep]
    else:
        needed = keep - len(fresh)
        picked = list(fresh) + wrapped_pool[:needed]
        wrapped = bool(wrapped_pool) and len(fresh) < keep

    return picked, {
        "wrapped": wrapped,
        "skipped_seen": skipped_seen,
        "skipped_already": skipped_excluded,
        "fresh_available": len(fresh),
        "pool": len(fresh) + len(wrapped_pool),
        "ids": [id_of(i) for i in picked],
    }


def record_batch(
    scope: str,
    machine: str,
    category: str,
    ids: Iterable[str],
    *,
    wrapped: bool = False,
    path: Path | None = None,
) -> list[str]:
    """Persist a newly shown batch. After a wrap, the list starts over at this batch."""
    ids = [i for i in ids if i]
    if wrapped:
        return set_seen(scope, machine, category, ids, path=path)
    return add_seen(scope, machine, category, ids, path=path)
