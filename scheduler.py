#!/usr/bin/env python3
"""Scheduled auto-scan, auto-download, and auto-cleanup for ModelRepoFinder.

The schedule runs only while the app is open. Enabling it is standing approval
to download models that match the saved conditions. Cleanup only touches
models this schedule downloaded (or that were opted in), never anything the
local scan merely found. The default cleanup action is to retire (move) a
model; permanent deletion requires an explicit confirmation.
"""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import watcher as W

DELETE_CONFIRM_PHRASE = "DELETE"
INVENTORY_NAME = ".mrf_schedule.json"
LOG_LIMIT = 50
CHECK_INTERVAL_SECONDS = 60

FREQUENCIES = ("daily", "weekly", "every_n_days")
POLICIES = ("keep_newest", "older_than", "superseded")
ACTIONS = ("retire", "delete")
WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}

DEFAULT_SCHEDULE: dict[str, Any] = {
    "enabled": False,
    "frequency": "weekly",
    "weekday": "monday",
    "every_n_days": 7,
    "time": "03:00",
    "categories": [],
    "include_top_picks": False,
    "sort": "downloads",
    "formats": [],
    "must_fit": True,
    "min_downloads": 0,
    "min_likes": 0,
    "skip_already_local": True,
    "max_new_per_run": 3,
    "max_total_auto": 20,
    "max_disk_gb": 50.0,
    "cleanup_enabled": True,
    "cleanup_policy": "keep_newest",
    "keep_newest_n": 3,
    "older_than_days": 30,
    "cleanup_action": "retire",
    "delete_confirmed": False,
}

_PATH: Path | None = None
_NOW_FN: Callable[[], datetime] | None = None
_INVENTORY_LOCK = threading.Lock()

CollectFn = Callable[..., dict]


def set_path(path: Path | None) -> Path | None:
    """Point the inventory file at a given path (tests) or back to default."""
    global _PATH
    _PATH = Path(path) if path is not None else None
    return _PATH


def current_path(root: Path | None = None) -> Path:
    if _PATH is not None:
        return _PATH
    return (root or W.ROOT) / INVENTORY_NAME


def set_now_fn(fn: Callable[[], datetime] | None) -> None:
    global _NOW_FN
    _NOW_FN = fn


def now() -> datetime:
    if _NOW_FN is not None:
        value = _NOW_FN()
    else:
        value = datetime.now().astimezone()
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return value


def _empty_inventory() -> dict:
    return {"machines": {}, "tracked": [], "log": []}


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


def load_inventory(path: Path | None = None) -> dict:
    path = Path(path) if path is not None else current_path()
    if not path.exists():
        return _empty_inventory()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_inventory()
    if not isinstance(raw, dict):
        return _empty_inventory()
    machines = raw.get("machines") if isinstance(raw.get("machines"), dict) else {}
    tracked = raw.get("tracked") if isinstance(raw.get("tracked"), list) else []
    log = raw.get("log") if isinstance(raw.get("log"), list) else []
    return {
        "machines": {str(k): (v if isinstance(v, dict) else {}) for k, v in machines.items()},
        "tracked": [t for t in tracked if isinstance(t, dict)],
        "log": [e for e in log if isinstance(e, dict)],
    }


def save_inventory(data: dict, path: Path | None = None) -> None:
    path = Path(path) if path is not None else current_path()
    with _INVENTORY_LOCK:
        _atomic_write(path, data)


def machine_state(inventory: dict, machine: str) -> dict:
    slot = (inventory.get("machines") or {}).get(machine) or {}
    return dict(slot) if isinstance(slot, dict) else {}


def set_machine_state(inventory: dict, machine: str, **updates) -> dict:
    machines = dict(inventory.get("machines") or {})
    slot = dict(machines.get(machine) or {})
    slot.update(updates)
    machines[machine] = slot
    inventory["machines"] = machines
    return inventory


def tracked_for(inventory: dict, machine: str | None = None) -> list[dict]:
    rows = [dict(t) for t in (inventory.get("tracked") or []) if isinstance(t, dict)]
    if machine:
        rows = [t for t in rows if t.get("machine") == machine]
    return rows


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return dt.replace(microsecond=0).isoformat()


def parse_iso(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            dt = None  # type: ignore[assignment]
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
                try:
                    dt = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    pass
            if dt is None:
                return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return dt

def parse_time_of_day(value: str) -> tuple[int, int]:
    text = (value or "").strip()
    match = re.fullmatch(r"(\d{1,2}):(\d{2})(?::\d{2})?", text)
    if not match:
        raise ValueError("Time of day must look like 03:00.")
    hour = int(match.group(1))
    minute = int(match.group(2))
    if hour > 23 or minute > 59:
        raise ValueError("Time of day must look like 03:00.")
    return hour, minute


def format_time_of_day(hour: int, minute: int) -> str:
    return f"{int(hour):02d}:{int(minute):02d}"


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off", ""}:
        return False
    return default


def _as_int(value: Any, default: int, lo: int, hi: int, *, strict: bool, name: str) -> int:
    if value is None or value == "":
        return default
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        if strict:
            raise ValueError(f"{name} must be a whole number.") from exc
        return default
    if number < lo or number > hi:
        if strict:
            raise ValueError(f"{name} must be between {lo} and {hi}.")
        return max(lo, min(hi, number))
    return number


def _as_float(value: Any, default: float, lo: float, hi: float, *, strict: bool, name: str) -> float:
    if value is None or value == "":
        return default
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        if strict:
            raise ValueError(f"{name} must be a number.") from exc
        return default
    if number < lo or number > hi:
        if strict:
            raise ValueError(f"{name} must be between {lo} and {hi}.")
        return max(lo, min(hi, number))
    return number


def normalize_schedule(
    raw: Any,
    *,
    strict: bool = False,
    known_categories: Iterable[str] | None = None,
    machine_formats: Iterable[str] | None = None,
) -> dict:
    """Return a complete, valid schedule dict. In strict mode, bad values raise."""
    src = raw if isinstance(raw, dict) else {}
    out = dict(DEFAULT_SCHEDULE)
    out["enabled"] = _as_bool(src.get("enabled"), False)

    freq = str(src.get("frequency") or out["frequency"]).strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {"every_n": "every_n_days", "interval": "every_n_days", "week": "weekly", "day": "daily"}
    freq = aliases.get(freq, freq)
    if freq not in FREQUENCIES:
        if strict:
            raise ValueError("Frequency must be daily, weekly, or every N days.")
        freq = "weekly"
    out["frequency"] = freq

    weekday = str(src.get("weekday") or out["weekday"]).strip().lower()
    if weekday not in WEEKDAYS:
        if strict and freq == "weekly" and src.get("weekday") not in (None, ""):
            raise ValueError("Weekday must be a day of the week, such as monday.")
        weekday = "monday"
    out["weekday"] = weekday
    out["every_n_days"] = _as_int(
        src.get("every_n_days"), 7, 1, 90, strict=strict, name="Every N days"
    )

    time_raw = src.get("time")
    if time_raw in (None, ""):
        out["time"] = DEFAULT_SCHEDULE["time"]
    else:
        try:
            hour, minute = parse_time_of_day(str(time_raw))
            out["time"] = format_time_of_day(hour, minute)
        except ValueError:
            if strict:
                raise
            out["time"] = DEFAULT_SCHEDULE["time"]

    known = set(known_categories) if known_categories is not None else None
    cats: list[str] = []
    raw_cats = src.get("categories") or []
    if raw_cats and not isinstance(raw_cats, list):
        if strict:
            raise ValueError("Categories must be a list.")
        raw_cats = []
    for item in raw_cats:
        key = str(item or "").strip()
        if not key:
            continue
        if not W.is_safe_path_component(key):
            if strict:
                raise ValueError("That category name is not allowed.")
            continue
        if known is not None and key not in known and key != "top_picks":
            if strict:
                raise ValueError(f"Unknown category: {key}")
            continue
        if key not in cats:
            cats.append(key)
    out["categories"] = cats
    out["include_top_picks"] = _as_bool(src.get("include_top_picks"), False)
    out["sort"] = W.normalize_hf_sort(str(src.get("sort") or "downloads"))
    out["must_fit"] = _as_bool(src.get("must_fit"), True)
    out["skip_already_local"] = _as_bool(src.get("skip_already_local"), True)

    allowed_formats = {f.lower() for f in (machine_formats or [])}
    formats: list[str] = []
    raw_fmts = src.get("formats") or []
    if raw_fmts and not isinstance(raw_fmts, list):
        if strict:
            raise ValueError("Formats must be a list.")
        raw_fmts = []
    for item in raw_fmts:
        fmt = str(item or "").strip().lower()
        if not fmt or not W.is_safe_path_component(fmt):
            if strict and fmt and not W.is_safe_path_component(fmt):
                raise ValueError("That format name is not allowed.")
            continue
        if allowed_formats and fmt not in allowed_formats:
            if fmt not in {"gguf", "mlx", "safetensors", "bin", "onnx", "other"}:
                if strict:
                    raise ValueError(f"Unknown format: {fmt}")
                continue
        if fmt not in formats:
            formats.append(fmt)
    out["formats"] = formats

    out["min_downloads"] = _as_int(
        src.get("min_downloads"), 0, 0, 10_000_000, strict=strict, name="Minimum downloads"
    )
    out["min_likes"] = _as_int(
        src.get("min_likes"), 0, 0, 10_000_000, strict=strict, name="Minimum likes"
    )
    out["max_new_per_run"] = _as_int(
        src.get("max_new_per_run"), 3, 1, 50, strict=strict, name="Max new models per run"
    )
    out["max_total_auto"] = _as_int(
        src.get("max_total_auto"), 20, 1, 200, strict=strict, name="Max total auto-managed models"
    )
    out["max_disk_gb"] = _as_float(
        src.get("max_disk_gb"), 50.0, 0.1, 10000.0, strict=strict, name="Max disk for auto-downloaded models"
    )
    out["cleanup_enabled"] = _as_bool(src.get("cleanup_enabled"), True)

    policy = str(src.get("cleanup_policy") or "keep_newest").strip().lower().replace("-", "_").replace(" ", "_")
    policy_aliases = {
        "newest": "keep_newest", "keep_newest_n": "keep_newest", "keepnewest": "keep_newest",
        "age": "older_than", "older": "older_than", "older_than_days": "older_than",
        "supersede": "superseded", "family": "superseded",
    }
    policy = policy_aliases.get(policy, policy)
    if policy not in POLICIES:
        if strict:
            raise ValueError("Cleanup policy must be keep newest, older than a number of days, or superseded.")
        policy = "keep_newest"
    out["cleanup_policy"] = policy
    out["keep_newest_n"] = _as_int(
        src.get("keep_newest_n"), 3, 1, 50, strict=strict, name="Keep newest N"
    )
    out["older_than_days"] = _as_int(
        src.get("older_than_days"), 30, 1, 3650, strict=strict, name="Older than (days)"
    )

    action = str(src.get("cleanup_action") or "retire").strip().lower()
    if action in {"move", "retire_only"}:
        action = "retire"
    if action in {"remove", "permanent", "permanently", "delete_permanently"}:
        action = "delete"
    if action not in ACTIONS:
        if strict:
            raise ValueError("Cleanup action must be retire or delete.")
        action = "retire"
    out["cleanup_action"] = action
    confirmed = _as_bool(src.get("delete_confirmed"), False) and action == "delete"
    out["delete_confirmed"] = confirmed
    return out


def confirmation_accepted(value: Any) -> bool:
    return str(value or "").strip().upper() == DELETE_CONFIRM_PHRASE


def apply_delete_confirmation(
    incoming: dict,
    existing: dict | None = None,
    *,
    confirmation: Any = None,
) -> dict:
    """Require an explicit DELETE confirmation when turning deletion on.

    Saving other fields while deletion is already confirmed is allowed.
    Switching back to retire clears the confirmation, so turning delete on
    again needs a fresh one.
    """
    schedule = dict(incoming)
    existing = existing or {}
    action = schedule.get("cleanup_action") or "retire"
    if action != "delete":
        schedule["delete_confirmed"] = False
        return schedule
    already = bool(existing.get("delete_confirmed")) and (
        (existing.get("cleanup_action") or "") == "delete"
    )
    if confirmation_accepted(confirmation) or already:
        schedule["delete_confirmed"] = True
        return schedule
    raise ValueError(
        "Permanent deletion requires confirmation. Type DELETE to confirm, "
        "and keep the confirmation box ticked."
    )


def schedule_for_machine(cfg: dict, machine: str, *, strict: bool = False) -> dict:
    machines = cfg.get("machines") or {}
    raw = (machines.get(machine) or {}).get("schedule") if machine in machines else None
    known = list(W.all_hf_tags(cfg).keys()) + list(W.SPECIAL_HF_CATEGORIES)
    formats = []
    if machine in machines:
        formats = [f.lower() for f in (machines[machine].get("formats") or [])]
    return normalize_schedule(
        raw, strict=strict, known_categories=known, machine_formats=formats
    )


def put_schedule(cfg: dict, machine: str, schedule: dict) -> dict:
    machines = dict(cfg.get("machines") or {})
    if machine not in machines:
        raise ValueError(f"Unknown machine: {machine}")
    slot = dict(machines[machine])
    slot["schedule"] = dict(schedule)
    machines[machine] = slot
    cfg["machines"] = machines
    return cfg

def _combine(day, hour: int, minute: int, tzinfo) -> datetime:
    try:
        return datetime(day.year, day.month, day.day, hour, minute, 0, tzinfo=tzinfo)
    except ValueError:
        # A DST spring-forward can skip an hour; land on the next valid hour.
        return datetime(day.year, day.month, day.day, min(hour + 1, 23), minute, 0, tzinfo=tzinfo)


def first_slot_on_or_after(
    schedule: dict,
    dt: datetime,
    *,
    anchor: datetime | None = None,
) -> datetime:
    """First scheduled datetime at or after `dt`."""
    hour, minute = parse_time_of_day(schedule.get("time") or "03:00")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
    tz = dt.tzinfo
    freq = schedule.get("frequency") or "weekly"
    if freq == "daily":
        candidate = _combine(dt.date(), hour, minute, tz)
        if candidate >= dt:
            return candidate
        return _combine(dt.date() + timedelta(days=1), hour, minute, tz)
    if freq == "every_n_days":
        n = max(1, int(schedule.get("every_n_days") or 7))
        start = anchor or dt
        if start.tzinfo is None:
            start = start.replace(tzinfo=tz)
        first = _combine(start.date(), hour, minute, start.tzinfo)
        if first < start:
            first = _combine(first.date() + timedelta(days=1), hour, minute, first.tzinfo)
        if first >= dt:
            return first
        days = (dt.date() - first.date()).days
        periods = days // n
        candidate = _combine(first.date() + timedelta(days=periods * n), hour, minute, first.tzinfo)
        if candidate >= dt:
            return candidate
        return _combine(candidate.date() + timedelta(days=n), hour, minute, candidate.tzinfo)
    want = WEEKDAYS.get(str(schedule.get("weekday") or "monday"), 0)
    days_ahead = (want - dt.weekday()) % 7
    candidate = _combine(dt.date() + timedelta(days=days_ahead), hour, minute, tz)
    if candidate < dt:
        candidate = _combine(candidate.date() + timedelta(days=7), hour, minute, tz)
    return candidate


def first_slot_strictly_after(
    schedule: dict,
    dt: datetime,
    *,
    anchor: datetime | None = None,
) -> datetime:
    return first_slot_on_or_after(schedule, dt + timedelta(seconds=1), anchor=anchor)


def is_due(
    schedule: dict,
    now_dt: datetime | None = None,
    state: dict | None = None,
) -> bool:
    """True if an automatic run should start at `now_dt`.

    A successful run (scheduled, catch-up, or Run now) records last_run.
    The next automatic run is the first scheduled slot strictly after that
    last_run. Enabling the schedule (or re-enabling it) sets enabled_at;
    if there has been no run since then, the first slot on or after
    enabled_at is used, so a newly enabled schedule waits for its first
    time rather than firing immediately. If the app was closed across a
    scheduled time, the missed slot is still in the past and is caught up
    on the next check (including at launch).
    """
    schedule = normalize_schedule(schedule)
    if not schedule["enabled"]:
        return False
    now_dt = now_dt or now()
    state = state or {}
    last_run = parse_iso(state.get("last_run"))
    armed_at = parse_iso(state.get("enabled_at"))
    # A re-enable starts a new waiting period; older last_runs do not
    # trigger an immediate catch-up from before the schedule was turned on.
    if armed_at and last_run and last_run < armed_at:
        last_run = None
    if last_run is None:
        start = armed_at or now_dt
        slot = first_slot_on_or_after(schedule, start, anchor=armed_at or start)
        return slot <= now_dt
    slot = first_slot_strictly_after(schedule, last_run, anchor=armed_at or last_run)
    return slot <= now_dt


def next_run_at(
    schedule: dict,
    now_dt: datetime | None = None,
    state: dict | None = None,
) -> datetime | None:
    """When the next automatic run is due, or None if the schedule is off."""
    schedule = normalize_schedule(schedule)
    if not schedule["enabled"]:
        return None
    now_dt = now_dt or now()
    state = state or {}
    last_run = parse_iso(state.get("last_run"))
    armed_at = parse_iso(state.get("enabled_at"))
    if armed_at and last_run and last_run < armed_at:
        last_run = None
    if last_run is None:
        start = armed_at or now_dt
        return first_slot_on_or_after(schedule, start, anchor=armed_at or start)
    return first_slot_strictly_after(schedule, last_run, anchor=armed_at or last_run)


_QUANT_PART = re.compile(
    r"^(?:q[1-8](?:_[kksm0-9]+)?|iq[1-4](?:_[xsml]+)?|"
    r"f16|f32|fp16|fp32|bf16|int[48]|q8_0|k_[ms]|gguf|ggml|mlx)$",
    re.I,
)
_VERSION_PART = re.compile(r"^v?\d+(?:\.\d+)*$", re.I)
_DATE_PART = re.compile(r"^20\d{6}$")
_SIZE_PART = re.compile(r"^\d+(?:\.\d+)?[bBmM]$")


def model_family(repo_id: str) -> str:
    """A stable family key: org + name, without quants, versions, or dates.

    Parameter-size tokens such as 7b or 13b are kept, so those stay distinct
    families. Version tokens such as v1 / v2 are stripped so a newer version
    can supersede an older one.
    """
    text = (repo_id or "").strip().lower()
    if text.endswith(".gguf"):
        text = text[:-5]
    org, sep, name = text.partition("/")
    if not sep:
        org, name = "", org
    parts = [p for p in re.split(r"[-_.]+", name) if p]
    kept: list[str] = []
    for part in parts:
        if _QUANT_PART.fullmatch(part) or _DATE_PART.fullmatch(part):
            continue
        if _VERSION_PART.fullmatch(part) and not _SIZE_PART.fullmatch(part):
            continue
        kept.append(part)
    stem = "-".join(kept) if kept else name
    return f"{org}/{stem}" if org else stem


def version_tuple(repo_id: str) -> tuple[int, ...] | None:
    text = (repo_id or "").strip().lower()
    name = text.split("/", 1)[-1]
    found: list[tuple[int, ...]] = []
    for part in re.split(r"[-_.]+", name):
        if _SIZE_PART.fullmatch(part):
            continue
        match = re.fullmatch(r"v(\d+(?:\.\d+)*)", part)
        if match:
            try:
                found.append(tuple(int(x) for x in match.group(1).split(".")))
            except ValueError:
                continue
    return found[-1] if found else None


def _item_date(item: dict, *, prefer_download: bool = False) -> datetime | None:
    keys = ("downloaded_at", "last_modified", "lastModified", "createdAt")
    if not prefer_download:
        keys = ("last_modified", "lastModified", "downloaded_at", "createdAt")
    for key in keys:
        parsed = parse_iso(item.get(key))
        if parsed:
            return parsed
    return None


def is_newer(a: dict, b: dict) -> bool | None:
    """True if `a` is a newer version/date than `b`. None if they look equal."""
    va, vb = version_tuple(str(a.get("id") or "")), version_tuple(str(b.get("id") or ""))
    if va and vb and va != vb:
        return va > vb
    da, db = _item_date(a), _item_date(b)
    if da and db and da != db:
        return da > db
    if va and not vb:
        return True
    if vb and not va:
        return False
    return None


def model_matches(
    model: dict,
    schedule: dict,
    *,
    already_ids: set[str] | None = None,
    cap: float | None = None,
) -> tuple[bool, str]:
    """Return (ok, reason). `reason` is a short skip reason when not ok."""
    schedule = normalize_schedule(schedule)
    mid = str(model.get("id") or "").strip()
    if not mid:
        return False, "missing id"
    if schedule["skip_already_local"] and already_ids:
        low = mid.lower()
        slashed = mid.replace("/", "__").lower()
        names = {low, slashed, mid.replace("__", "/", 1).lower()}
        file_name = str(model.get("file") or model.get("name") or "").lower()
        if file_name:
            names.add(file_name)
        already_low = {str(x).lower() for x in already_ids}
        if names & already_low:
            return False, "already on this computer"
    fmt = str(model.get("format") or "").lower()
    wanted = [f.lower() for f in (schedule.get("formats") or [])]
    if wanted and fmt and fmt not in wanted:
        return False, f"format {fmt} is not in the allowed list"
    downloads = int(model.get("downloads") or 0)
    likes = int(model.get("likes") or 0)
    if downloads < int(schedule["min_downloads"]):
        return False, "below the minimum downloads"
    if likes < int(schedule["min_likes"]):
        return False, "below the minimum likes"
    if schedule["must_fit"] and cap:
        size = float(model.get("size_gb") or model.get("stored_size_gb") or 0)
        if size and size > float(cap):
            return False, "over this computer's size cap"
        if model.get("over_cap"):
            return False, "over this computer's size cap"
    cats = list(schedule.get("categories") or [])
    include_top = bool(schedule.get("include_top_picks"))
    tag = str(model.get("tag") or "")
    source = str(model.get("source") or model.get("mode") or "")
    if cats or include_top:
        allowed_tags = set(cats)
        if include_top:
            allowed_tags.add("top_picks")
        if tag and allowed_tags and tag not in allowed_tags:
            if not (include_top and (not tag or tag == "top_picks" or source == "top_picks")):
                return False, "category not selected"
    return True, ""


def filter_candidates(
    models: Iterable[dict],
    schedule: dict,
    *,
    already_ids: set[str] | None = None,
    cap: float | None = None,
) -> tuple[list[dict], list[dict]]:
    """Split models into (kept, skipped-with-reason)."""
    kept: list[dict] = []
    skipped: list[dict] = []
    seen: set[str] = set()
    for model in models:
        mid = str(model.get("id") or "").strip()
        key = mid.lower()
        if key and key in seen:
            skipped.append({**model, "skip_reason": "duplicate"})
            continue
        ok, reason = model_matches(model, schedule, already_ids=already_ids, cap=cap)
        if not ok:
            skipped.append({**model, "skip_reason": reason})
            continue
        if key:
            seen.add(key)
        kept.append(dict(model))
    kept = W.sort_models(kept, schedule.get("sort") or "downloads")
    return kept, skipped


def apply_limits(
    models: list[dict],
    schedule: dict,
    *,
    current_count: int,
    current_disk_gb: float,
) -> tuple[list[dict], list[dict]]:
    """Enforce max-new, max-total, and max-disk. Returns (accepted, skipped)."""
    schedule = normalize_schedule(schedule)
    max_new = int(schedule["max_new_per_run"])
    max_total = int(schedule["max_total_auto"])
    max_disk = float(schedule["max_disk_gb"])
    slots = max(0, max_total - max(0, int(current_count)))
    budget = max(0.0, max_disk - max(0.0, float(current_disk_gb)))
    accepted: list[dict] = []
    skipped: list[dict] = []
    running_disk = 0.0
    for model in models:
        if len(accepted) >= max_new:
            skipped.append({**model, "skip_reason": "per-run limit reached"})
            continue
        if len(accepted) >= slots:
            skipped.append({**model, "skip_reason": "total auto-managed limit reached"})
            continue
        size = float(model.get("size_gb") or model.get("stored_size_gb") or 0)
        if size and (running_disk + size) > budget + 1e-9:
            skipped.append({**model, "skip_reason": "disk limit reached"})
            continue
        accepted.append(dict(model))
        running_disk += size
    return accepted, skipped

def incoming_category_counts(cfg: dict, machine: str, root: Path | None = None) -> dict[str, int]:
    """How many items sit in each incoming category folder for this machine."""
    counts: dict[str, int] = {}
    for row in W.list_installed(cfg, machine=machine, root=root):
        tag = str(row.get("tag") or "")
        if not tag:
            continue
        counts[tag] = counts.get(tag, 0) + 1
    return counts


def plan_cleanup(
    tracked: list[dict],
    schedule: dict,
    *,
    category_counts: dict[str, int] | None = None,
    download_in_progress: bool = False,
    downloads_failed: bool = False,
    now_dt: datetime | None = None,
    newly_downloaded: list[dict] | None = None,
) -> list[dict]:
    """Return tracked items that would be retired or deleted, with reasons.

    Safeguards:
    - nothing is planned when a download is in progress, or when downloads
      in this run failed (including an offline failure);
    - only tracked (auto-downloaded or opted-in) items are considered;
    - at least one model is left in each category that currently has any.
    """
    schedule = normalize_schedule(schedule)
    if not schedule["cleanup_enabled"]:
        return []
    if download_in_progress or downloads_failed:
        return []
    now_dt = now_dt or now()
    category_counts = dict(category_counts or {})
    pool = [dict(t) for t in tracked]
    newer_family: dict[str, dict] = {}
    for row in (newly_downloaded or []) + pool:
        fam = row.get("family") or model_family(str(row.get("id") or ""))
        prev = newer_family.get(fam)
        if prev is None or is_newer(row, prev):
            newer_family[fam] = row

    policy = schedule["cleanup_policy"]
    keep_n = int(schedule["keep_newest_n"])
    older_days = int(schedule["older_than_days"])
    cutoff = now_dt - timedelta(days=older_days)

    def _sort_key(item: dict):
        dt = _item_date(item, prefer_download=True) or datetime(1970, 1, 1, tzinfo=timezone.utc)
        ver = version_tuple(str(item.get("id") or "")) or ()
        return (dt, ver)

    candidates: list[dict] = []
    if policy == "keep_newest":
        by_tag: dict[str, list[dict]] = {}
        for item in pool:
            by_tag.setdefault(str(item.get("tag") or "misc"), []).append(item)
        for tag, rows in by_tag.items():
            rows = sorted(rows, key=_sort_key, reverse=True)
            for extra in rows[keep_n:]:
                candidates.append({**extra, "cleanup_reason": f"beyond the keep-newest-{keep_n} for {tag}"})
    elif policy == "older_than":
        for item in pool:
            dt = _item_date(item, prefer_download=True)
            if dt is not None and dt <= cutoff:
                candidates.append({**item, "cleanup_reason": f"older than {older_days} days"})
    elif policy == "superseded":
        for item in pool:
            fam = item.get("family") or model_family(str(item.get("id") or ""))
            leader = newer_family.get(fam)
            if not leader:
                continue
            same = (
                str(leader.get("id") or "") == str(item.get("id") or "")
                and str(leader.get("relative_path") or leader.get("name") or "")
                == str(item.get("relative_path") or item.get("name") or "")
                and str(leader.get("downloaded_at") or "") == str(item.get("downloaded_at") or "")
                and str(leader.get("path") or "") == str(item.get("path") or "")
            )
            if same:
                continue
            if is_newer(leader, item):
                leader_id = leader.get("id") or ""
                reason = "superseded by a newer version of the same family"
                if leader_id:
                    reason += f" ({leader_id})"
                candidates.append({**item, "cleanup_reason": reason})

    planned: list[dict] = []
    remaining = dict(category_counts)
    for item in sorted(candidates, key=_sort_key):
        tag = str(item.get("tag") or "misc")
        have = remaining.get(tag, 0)
        if have <= 1:
            continue
        planned.append(item)
        remaining[tag] = have - 1
    return planned


def delete_item(
    cfg: dict,
    machine: str,
    tag: str,
    relative_path: str,
    root: Path | None = None,
) -> Path:
    """Permanently delete one app-managed item. Same containment as retire.

    Never deletes anything outside the incoming root. Never follows a
    crafted relative path. Cleanup deletes from incoming only, the same
    place retire would move from.
    """
    import shutil

    machine = W.validate_machine_key(cfg, machine)
    tag = W._safe_component(tag, "category")
    rel = W._safe_relative(relative_path)
    incoming = W.incoming_dir(cfg, machine, tag, root).resolve()
    src = (incoming / rel).resolve()
    if incoming not in src.parents and src != incoming:
        raise ValueError("That file is not inside the incoming folder.")
    top = W.incoming_root(cfg, root).resolve()
    W.assert_contained(src, top)
    if src == top or src == incoming or src == incoming.parent:
        raise ValueError("Refusing to delete that folder.")
    if not src.exists():
        raise FileNotFoundError(f"Nothing to delete at {src}")
    if src.is_dir() and not src.is_symlink():
        shutil.rmtree(src)
    else:
        src.unlink()
    return src


def retire_or_delete(
    cfg: dict,
    item: dict,
    action: str,
    root: Path | None = None,
) -> Path:
    machine = str(item.get("machine") or "")
    tag = str(item.get("tag") or "")
    relative = str(item.get("relative_path") or item.get("name") or "")
    if action == "delete":
        return delete_item(cfg, machine, tag, relative, root)
    return W.retire_item(cfg, machine, tag, relative, root)


def is_offline_error(exc: BaseException | None) -> bool:
    if exc is None:
        return False
    if isinstance(exc, (ConnectionError, TimeoutError, ConnectionRefusedError, ConnectionResetError, BrokenPipeError)):
        return True
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    if "no space" in msg or "disk full" in msg or "errno 28" in msg:
        return False
    if "timeout" in name or "connection" in name or "nameresolution" in name:
        return True
    needles = (
        "failed to resolve", "name resolution", "getaddrinfo",
        "nodename nor servname", "name or service not known",
        "temporary failure in name resolution", "network is unreachable",
        "network unreachable", "no route to host", "connection refused",
        "connection reset", "connection aborted", "connection timed out",
        "timed out", "timeout", "offline", "no internet",
        "max retries exceeded", "failed to establish a new connection",
        "failed to establish", "errno 101", "errno 111", "errno -2", "errno -3",
    )
    return any(n in msg for n in needles)


def _flatten_collected(data: dict, *, default_tag: str | None = None, source: str = "") -> tuple[list[dict], list[str]]:
    rows: list[dict] = []
    errors: list[str] = []
    for tag in data.get("tags") or []:
        if tag.get("error"):
            errors.append(f"{tag.get('title') or tag.get('name')}: {tag['error']}")
        for model in tag.get("models") or []:
            item = dict(model)
            item["tag"] = item.get("tag") or tag.get("name") or default_tag or "misc"
            if source:
                item["source"] = source
            rows.append(item)
    for model in data.get("overall") or []:
        item = dict(model)
        item["tag"] = item.get("tag") or default_tag or "top_picks"
        item["source"] = source or "top_picks"
        rows.append(item)
    return rows, errors


def gather_candidates(
    cfg: dict,
    machine: str,
    schedule: dict,
    *,
    already_ids: set[str] | None = None,
    collect_fn: CollectFn | None = None,
    top_picks_fn: CollectFn | None = None,
    progress_cb: Callable[[str], None] | None = None,
    search_limit: int = 80,
) -> tuple[list[dict], list[str], bool]:
    """Collect matching models. Returns (models, errors, offline).

    `collect_fn` and `top_picks_fn` are injectable so tests never touch the
    network. When omitted, the real Hugging Face collectors are used.
    """
    schedule = normalize_schedule(schedule)
    machines = W.enabled_machines(cfg)
    if machine not in machines:
        raise ValueError(f"Unknown or disabled machine: {machine}")
    machine_cfg = machines[machine]
    cap = float(machine_cfg.get("max_model_gb") or 0)
    cats = list(schedule["categories"])
    if not cats:
        cats = list(machine_cfg.get("hf_categories") or W.all_hf_tags(cfg).keys())
    rows: list[dict] = []
    errors: list[str] = []
    offline = False
    keep = max(20, int(schedule["max_new_per_run"]) * 5)
    already = already_ids if schedule["skip_already_local"] else None
    cap_override = None if schedule["must_fit"] else max(cap, 10_000.0)

    def _call(fn, **kw):
        nonlocal offline
        try:
            return fn(**kw)
        except Exception as exc:  # noqa: BLE001
            if is_offline_error(exc):
                offline = True
                errors.append(f"Could not reach Hugging Face ({exc}).")
            else:
                errors.append(str(exc))
            return None

    if cats:
        fn = collect_fn or (
            lambda **kw: W.collect_suggestions(
                cfg, machine, progress_cb=progress_cb, search_limit=search_limit,
                categories=kw.get("categories"), sort=schedule["sort"],
                already_ids=already or set(), record_seen=False,
                keep_override=keep, cap_override=cap_override,
            )
        )
        data = _call(fn, categories=cats, sort=schedule["sort"], already_ids=already, keep=keep)
        if data:
            part, errs = _flatten_collected(data)
            rows.extend(part)
            errors.extend(errs)

    if schedule["include_top_picks"]:
        fn = top_picks_fn or (
            lambda **kw: W.collect_top_picks(
                cfg, machine, progress_cb=progress_cb, sort=schedule["sort"],
                categories=None, search_limit=min(search_limit, 50),
                keep=keep, already_ids=already or set(),
            )
        )
        data = _call(fn, sort=schedule["sort"], already_ids=already, keep=keep)
        if data:
            part, errs = _flatten_collected(data, default_tag="top_picks", source="top_picks")
            rows.extend(part)
            errors.extend(errs)

    kept, _skipped = filter_candidates(
        rows, schedule, already_ids=already_ids, cap=cap if schedule["must_fit"] else None
    )
    return kept, errors, offline


def _disk_of(tracked: list[dict]) -> float:
    total = 0.0
    for item in tracked:
        try:
            total += float(item.get("size_gb") or 0)
        except (TypeError, ValueError):
            continue
    return total

def _preview_message(downloads, cleanup, action, offline, errors) -> str:
    if offline:
        return (
            "Could not reach Hugging Face, so this preview has nothing new to "
            "download and cleanup would be skipped (the app never cleans up "
            "when a download or search fails)."
        )
    bits = [
        f"{len(downloads)} model" + ("s" if len(downloads) != 1 else "") + " would be downloaded"
    ]
    verb = "permanently deleted" if action == "delete" else "moved to the retired folder"
    bits.append(f"{len(cleanup)} would be {verb}")
    msg = ", and ".join(bits) + "."
    if errors:
        msg += " Some searches reported a problem; cleanup would be skipped if this were a real run."
    return msg


def build_preview(
    cfg: dict,
    machine: str,
    schedule: dict | None = None,
    *,
    inventory: dict | None = None,
    already_ids: set[str] | None = None,
    collect_fn: CollectFn | None = None,
    top_picks_fn: CollectFn | None = None,
    progress_cb: Callable[[str], None] | None = None,
    now_dt: datetime | None = None,
    root: Path | None = None,
    download_in_progress: bool = False,
) -> dict:
    """What a run would download and retire/delete, without changing anything."""
    schedule = normalize_schedule(schedule if schedule is not None else schedule_for_machine(cfg, machine))
    inventory = inventory if inventory is not None else load_inventory(current_path(root))
    tracked = tracked_for(inventory, machine)
    now_dt = now_dt or now()
    candidates, errors, offline = gather_candidates(
        cfg, machine, schedule, already_ids=already_ids,
        collect_fn=collect_fn, top_picks_fn=top_picks_fn, progress_cb=progress_cb,
    )
    would_download, skipped_limits = apply_limits(
        candidates, schedule, current_count=len(tracked), current_disk_gb=_disk_of(tracked),
    )
    search_failed = bool(offline or errors)
    counts = incoming_category_counts(cfg, machine, root)
    would_cleanup = plan_cleanup(
        tracked, schedule, category_counts=counts,
        download_in_progress=download_in_progress, downloads_failed=search_failed,
        now_dt=now_dt, newly_downloaded=would_download,
    )
    action = schedule["cleanup_action"] if schedule.get("delete_confirmed") else "retire"
    if schedule["cleanup_action"] == "delete" and not schedule.get("delete_confirmed"):
        action = "retire"
    return {
        "machine": machine,
        "dry_run": True,
        "offline": offline,
        "search_failed": search_failed,
        "errors": errors,
        "would_download": would_download,
        "would_cleanup": would_cleanup,
        "cleanup_action": action,
        "skipped": skipped_limits,
        "tracked_count": len(tracked),
        "tracked_disk_gb": round(_disk_of(tracked), 3),
        "generated_at": _iso(now_dt),
        "schedule": schedule,
        "message": _preview_message(would_download, would_cleanup, action, offline, errors),
    }


def verify_download(path: Path | None) -> bool:
    if not path:
        return False
    try:
        path = Path(path)
        if not path.exists():
            return False
        if path.is_file():
            return path.stat().st_size > 0
        if path.is_dir():
            for p in path.rglob("*"):
                if not p.is_file():
                    continue
                if ".cache" in p.parts or ".huggingface" in p.parts or ".git" in p.parts:
                    continue
                if p.stat().st_size > 0:
                    return True
            return False
    except OSError:
        return False
    return False


def default_download(cfg: dict, machine: str, item: dict, root: Path, progress: dict | None = None) -> Path:
    """Download one model into the app's incoming folder and verify it."""
    tag = W.validate_category_key(
        cfg, str(item.get("tag") or "misc"), scope="hf", extra=W.SPECIAL_HF_CATEGORIES,
    )
    W.validate_machine_key(cfg, machine)
    dest = W.incoming_dir(cfg, machine, tag, root)
    W.assert_contained(dest, W.incoming_root(cfg, root))
    repo_id = str(item.get("id") or "").strip()
    if not repo_id:
        raise ValueError("Missing model id.")
    token = W.load_hf_token(root) or None
    fmt = str(item.get("format") or "gguf").lower()
    file_path = str(item.get("file_path") or item.get("file") or "")
    whole = bool(item.get("whole_repo")) or file_path in {"", "(whole model)"}
    if fmt == "gguf" and file_path and file_path != "(whole model)" and not item.get("whole_repo"):
        path = W.download_gguf(repo_id, file_path, dest, progress=progress, token=token)
    else:
        ignore = [".git*"]
        if item.get("skip_bin_duplicates"):
            ignore.append("*.bin")
        path = W.download_repo(
            repo_id, dest, progress=progress, token=token,
            ignore_patterns=ignore, nested=(tag != "manual"),
        )
    companions = item.get("companions") or []
    if companions and item.get("download_companions", True) and not whole:
        for comp in companions:
            cpath = (comp.get("path") if isinstance(comp, dict) else "") or ""
            if not cpath:
                continue
            if Path(cpath).is_absolute() or ".." in Path(cpath).parts:
                continue
            W.download_gguf(repo_id, cpath, dest, progress=progress, token=token)
    if not verify_download(path):
        raise RuntimeError("The download finished but the files could not be verified.")
    return Path(path)


def _relative_of(path: Path, dest: Path) -> str:
    try:
        rel = path.resolve().relative_to(dest.resolve())
        parts = rel.parts
        return parts[0] if parts else path.name
    except (ValueError, OSError):
        return path.name


def _record_tracked(
    inventory: dict,
    *,
    machine: str,
    tag: str,
    repo_id: str,
    path: Path,
    dest: Path,
    item: dict,
    source: str,
    now_dt: datetime,
) -> dict:
    rel = _relative_of(path, dest)
    size = W._folder_or_file_size(path if path.exists() else dest / rel)
    row = {
        "id": repo_id,
        "machine": machine,
        "tag": tag,
        "name": rel,
        "relative_path": rel,
        "path": str((dest / rel) if not path.exists() else path),
        "size_gb": size / (1024 ** 3),
        "format": item.get("format") or "gguf",
        "family": model_family(repo_id),
        "downloaded_at": _iso(now_dt),
        "last_modified": item.get("lastModified") or item.get("last_modified") or _iso(now_dt),
        "source": source,
    }
    tracked = [
        t for t in (inventory.get("tracked") or [])
        if not (t.get("machine") == machine and t.get("tag") == tag and t.get("relative_path") == rel)
    ]
    tracked.append(row)
    inventory["tracked"] = tracked
    return row


def opt_in(
    cfg: dict,
    inventory: dict,
    *,
    machine: str,
    tag: str,
    relative_path: str,
    root: Path | None = None,
    now_dt: datetime | None = None,
    repo_id: str | None = None,
) -> dict:
    """Mark a model this app already holds as auto-managed (cleanup may touch it)."""
    machine = W.validate_machine_key(cfg, machine)
    tag = W._safe_component(tag, "category")
    rel = str(W._safe_relative(relative_path))
    incoming = W.incoming_dir(cfg, machine, tag, root).resolve()
    src = (incoming / rel).resolve()
    if incoming not in src.parents and src != incoming:
        raise ValueError("That file is not inside the incoming folder.")
    W.assert_contained(src, W.incoming_root(cfg, root))
    if not src.exists():
        raise FileNotFoundError("Nothing at that path to opt in.")
    size = W._folder_or_file_size(src)
    guessed_id = repo_id or rel.replace("__", "/", 1)
    fmt = ""
    try:
        fmt = W._format_of_path(src.name)
    except Exception:  # noqa: BLE001
        fmt = ""
    row = {
        "id": guessed_id,
        "machine": machine,
        "tag": tag,
        "name": rel,
        "relative_path": rel,
        "path": str(src),
        "size_gb": size / (1024 ** 3),
        "format": fmt,
        "family": model_family(guessed_id),
        "downloaded_at": _iso(now_dt or now()),
        "last_modified": _iso(datetime.fromtimestamp(src.stat().st_mtime).astimezone()),
        "source": "opt-in",
    }
    tracked = [
        t for t in (inventory.get("tracked") or [])
        if not (t.get("machine") == machine and t.get("tag") == tag and t.get("relative_path") == rel)
    ]
    tracked.append(row)
    inventory["tracked"] = tracked
    return row


def opt_out(
    inventory: dict,
    *,
    machine: str,
    tag: str,
    relative_path: str,
) -> bool:
    before = list(inventory.get("tracked") or [])
    after = [
        t for t in before
        if not (
            t.get("machine") == machine
            and t.get("tag") == tag
            and t.get("relative_path") == relative_path
        )
    ]
    inventory["tracked"] = after
    return len(after) < len(before)


def append_log(inventory: dict, entry: dict) -> dict:
    log = list(inventory.get("log") or [])
    log.append(entry)
    inventory["log"] = log[-LOG_LIMIT:]
    return inventory

def execute_run(
    cfg: dict,
    machine: str,
    schedule: dict | None = None,
    *,
    inventory: dict | None = None,
    already_ids: set[str] | None = None,
    collect_fn: CollectFn | None = None,
    top_picks_fn: CollectFn | None = None,
    download_fn: Callable[[dict], Path] | None = None,
    progress_cb: Callable[[str], None] | None = None,
    now_dt: datetime | None = None,
    root: Path | None = None,
    dry_run: bool = False,
    kind: str = "scheduled",
    download_in_progress: bool = False,
    allow_fn: Callable[[str], None] | None = None,
) -> dict:
    """Run (or dry-run) a scan + download + cleanup cycle.

    Downloads always finish and are verified before cleanup. If any download
    fails, or the search looks offline, cleanup is skipped. Enabling the
    schedule is standing approval: accepted models are added to the allow
    list (via `allow_fn` or directly) before they are fetched.
    """
    now_dt = now_dt or now()
    root = root or W.ROOT
    schedule = normalize_schedule(
        schedule if schedule is not None else schedule_for_machine(cfg, machine)
    )
    inventory = inventory if inventory is not None else load_inventory(current_path(root))
    if dry_run:
        preview = build_preview(
            cfg, machine, schedule, inventory=inventory, already_ids=already_ids,
            collect_fn=collect_fn, top_picks_fn=top_picks_fn, progress_cb=progress_cb,
            now_dt=now_dt, root=root, download_in_progress=download_in_progress,
        )
        preview["kind"] = "preview"
        preview["status"] = "preview"
        return preview

    if download_in_progress:
        result = {
            "machine": machine, "dry_run": False, "kind": kind, "status": "skipped",
            "message": "A download is already in progress, so this run was skipped.",
            "downloaded": [], "cleaned": [], "failed": [], "errors": [],
            "offline": False, "cleanup_skipped": True, "generated_at": _iso(now_dt),
        }
        append_log(inventory, {**result, "at": _iso(now_dt), "retired": [], "deleted": []})
        save_inventory(inventory, current_path(root))
        return result

    tracked = tracked_for(inventory, machine)
    candidates, errors, offline = gather_candidates(
        cfg, machine, schedule, already_ids=already_ids,
        collect_fn=collect_fn, top_picks_fn=top_picks_fn, progress_cb=progress_cb,
    )
    to_download, skipped_limits = apply_limits(
        candidates, schedule, current_count=len(tracked), current_disk_gb=_disk_of(tracked),
    )

    if offline:
        # Do not record last_run, so a brief outage is retried on the next tick.
        result = {
            "machine": machine, "dry_run": False, "kind": kind, "status": "offline",
            "message": (
                "Could not reach Hugging Face. Nothing was downloaded or cleaned up; "
                "the run will be tried again later."
            ),
            "downloaded": [], "cleaned": [], "failed": [], "errors": errors,
            "offline": True, "cleanup_skipped": True, "skipped": skipped_limits,
            "generated_at": _iso(now_dt), "schedule": schedule,
        }
        append_log(inventory, {
            "at": _iso(now_dt), "machine": machine, "kind": kind,
            "status": "offline", "message": result["message"], "errors": errors,
            "offline": True, "downloaded": [], "retired": [], "deleted": [],
        })
        save_inventory(inventory, current_path(root))
        return result

    downloaded: list[dict] = []
    failed: list[dict] = []
    for item in to_download:
        repo_id = str(item.get("id") or "")
        tag = str(item.get("tag") or "misc")
        if progress_cb:
            progress_cb(f"Downloading {repo_id}…")
        try:
            if allow_fn:
                allow_fn(repo_id)
            else:
                W.add_to_allow_list(cfg, repo_id)
            path = Path(download_fn(item) if download_fn else default_download(cfg, machine, item, root))
            if not verify_download(path):
                raise RuntimeError("The download finished but the files could not be verified.")
            dest = W.incoming_dir(cfg, machine, tag, root)
            row = _record_tracked(
                inventory, machine=machine, tag=tag, repo_id=repo_id,
                path=path, dest=dest, item=item, source="auto", now_dt=now_dt,
            )
            downloaded.append(row)
        except Exception as exc:  # noqa: BLE001
            failed.append({**item, "error": str(exc), "offline": is_offline_error(exc)})
            errors.append(f"{repo_id}: {exc}")

    downloads_failed = bool(failed)
    cleaned: list[dict] = []
    cleanup_skipped = downloads_failed
    action = "retire"
    if schedule["cleanup_action"] == "delete" and schedule.get("delete_confirmed"):
        action = "delete"
    if not downloads_failed and schedule["cleanup_enabled"]:
        if progress_cb:
            progress_cb("Cleaning up out-of-date auto-managed models…")
        counts = incoming_category_counts(cfg, machine, root)
        planned = plan_cleanup(
            tracked_for(inventory, machine), schedule, category_counts=counts,
            download_in_progress=False, downloads_failed=False,
            now_dt=now_dt, newly_downloaded=downloaded,
        )
        just_ids = {(d.get("machine"), d.get("tag"), d.get("relative_path")) for d in downloaded}
        for item in planned:
            key = (item.get("machine"), item.get("tag"), item.get("relative_path"))
            if key in just_ids:
                continue
            try:
                dest = retire_or_delete(cfg, item, action, root)
                cleaned.append({**item, "action": action, "destination": str(dest)})
                opt_out(
                    inventory,
                    machine=str(item.get("machine") or machine),
                    tag=str(item.get("tag") or ""),
                    relative_path=str(item.get("relative_path") or item.get("name") or ""),
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"Cleanup of {item.get('id') or item.get('name')}: {exc}")

    if failed and not downloaded:
        status = "error"
    elif failed:
        status = "partial"
    else:
        status = "ok"

    verb_clean = "permanently deleted" if action == "delete" else "moved to the retired folder"
    message_parts = [
        f"Downloaded {len(downloaded)} model" + ("s" if len(downloaded) != 1 else "") + ".",
    ]
    if cleanup_skipped:
        message_parts.append("Cleanup was skipped because a download did not finish.")
    else:
        n = len(cleaned)
        message_parts.append(
            f"{n} auto-managed model" + ("s were " if n != 1 else " was ") + verb_clean + "."
        )
    if failed:
        message_parts.append(f"{len(failed)} download(s) failed.")

    result = {
        "machine": machine, "dry_run": False, "kind": kind, "status": status,
        "message": " ".join(message_parts),
        "downloaded": downloaded, "cleaned": cleaned, "failed": failed,
        "errors": errors, "offline": False, "cleanup_skipped": cleanup_skipped,
        "cleanup_action": action, "skipped": skipped_limits,
        "generated_at": _iso(now_dt), "schedule": schedule,
    }
    set_machine_state(
        inventory, machine,
        last_run=_iso(now_dt), last_run_status=status, last_message=result["message"],
    )
    retired = [c for c in cleaned if c.get("action") != "delete"]
    deleted = [c for c in cleaned if c.get("action") == "delete"]
    append_log(inventory, {
        "at": _iso(now_dt), "machine": machine, "kind": kind, "status": status,
        "message": result["message"],
        "downloaded": [{"id": d.get("id"), "tag": d.get("tag"), "name": d.get("name")} for d in downloaded],
        "retired": [{"id": c.get("id"), "tag": c.get("tag"), "name": c.get("name")} for c in retired],
        "deleted": [{"id": c.get("id"), "tag": c.get("tag"), "name": c.get("name")} for c in deleted],
        "errors": errors, "offline": False,
    })
    save_inventory(inventory, current_path(root))
    result["inventory"] = inventory
    return result


def public_state(
    cfg: dict,
    machine: str,
    *,
    inventory: dict | None = None,
    root: Path | None = None,
    now_dt: datetime | None = None,
) -> dict:
    """JSON-safe schedule view for the local API and the app page."""
    now_dt = now_dt or now()
    schedule = schedule_for_machine(cfg, machine)
    inventory = inventory if inventory is not None else load_inventory(current_path(root))
    state = machine_state(inventory, machine)
    nxt = next_run_at(schedule, now_dt, state)
    tracked = tracked_for(inventory, machine)
    log = [e for e in (inventory.get("log") or []) if isinstance(e, dict)]
    machine_log = [e for e in log if e.get("machine") == machine] or list(log)
    machine_log = list(reversed(machine_log[-20:]))
    return {
        "settings": schedule,
        "enabled": bool(schedule["enabled"]),
        "last_run": state.get("last_run"),
        "last_run_status": state.get("last_run_status"),
        "last_message": state.get("last_message"),
        "enabled_at": state.get("enabled_at"),
        "next_run": _iso(nxt) if nxt else None,
        "due": is_due(schedule, now_dt, state),
        "tracked_count": len(tracked),
        "tracked_disk_gb": round(_disk_of(tracked), 3),
        "tracked": tracked,
        "log": machine_log,
        "delete_phrase": DELETE_CONFIRM_PHRASE,
        "frequencies": [
            {"key": "daily", "name": "Every day"},
            {"key": "weekly", "name": "Every week"},
            {"key": "every_n_days", "name": "Every N days"},
        ],
        "weekdays": [{"key": k, "name": k.capitalize()} for k in WEEKDAYS],
        "policies": [
            {"key": "keep_newest", "name": "Keep the newest N per category"},
            {"key": "older_than", "name": "Older than a number of days"},
            {"key": "superseded", "name": "Superseded by a newer version of the same family"},
        ],
        "actions": [
            {"key": "retire", "name": "Retire (move to the retired folder)"},
            {"key": "delete", "name": "Delete permanently"},
        ],
    }


def note_enabled_transition(
    inventory: dict,
    machine: str,
    was_enabled: bool,
    now_enabled: bool,
    now_dt: datetime | None = None,
) -> dict:
    """Record enabled_at when the schedule is turned on (including re-enable)."""
    now_dt = now_dt or now()
    if now_enabled and not was_enabled:
        set_machine_state(inventory, machine, enabled_at=_iso(now_dt))
    return inventory
