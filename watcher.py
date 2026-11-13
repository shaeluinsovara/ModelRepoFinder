#!/usr/bin/env python3
"""ModelRepoFinder — search, pick, and download Hugging Face models.

Keeps the original rules:
- GGUF search sorted by downloads
- Companion files (mmproj / projector / clip / vision-encoder) are offered
  with multimodal models rather than skipped globally
- Pick a quant near 70% of the machine cap, preferring Q4_K_M / Q5_K_M / Q4_K_S / Q5_K_S / Q6_K
- Tags with keep counts
- Allow-list gate for downloads
- Retired models are moved, never deleted
- Whole-repo downloads keep license and README files (only .git is skipped)
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

try:
    import yaml
except ImportError:
    print("Install PyYAML: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

try:
    from huggingface_hub import HfApi, hf_hub_download, snapshot_download
    from huggingface_hub.hf_api import RepoFile
    from huggingface_hub.utils import HfHubHTTPError
except ImportError:
    print("Install huggingface_hub: pip install huggingface_hub", file=sys.stderr)
    sys.exit(1)

try:
    from tqdm.auto import tqdm
except ImportError:
    tqdm = None  # type: ignore


ROOT = Path(__file__).resolve().parent
HF_TOKEN_FILE = ROOT / ".hf_token"
VERSION = "1.6.1"
SNAPSHOT_IGNORE_DEFAULT = [".git*"]
USER_AGENT = f"model-repo-finder/{VERSION}"
HF = HfApi(headers={"User-Agent": USER_AGENT})

SKIP_NAME_RE = re.compile(
    r"(^|[/_.-])(mmproj|projector|clip|vision.encoder)([/_.-]|$)", re.I
)
PREFERRED_QUANTS = re.compile(r"Q4_K_M|Q5_K_M|Q4_K_S|Q5_K_S|Q6_K")
WEIGHT_SUFFIXES = {
    ".safetensors",
    ".bin",
    ".pt",
    ".pth",
    ".ckpt",
    ".onnx",
    ".gguf",
    ".npz",
    ".mlx",
    ".msgpack",
    ".sft",
    ".pkl",
    ".ncnn",
    ".param",
    ".mlmodel",
    ".mlpackage",
    ".tflite",
    ".coreml",
}

BUILTIN_HF_KEYS = (
    "coding",
    "relational",
    "spiritual",
    "vision",
    "music",
    "speech",
    "background",
    "embedding",
)
BUILTIN_GH_KEYS = (
    "runners",
    "memory_agents",
    "voice",
    "music",
    "vision",
    "home_automation",
    "coding_tools",
)

ProgressCb = Callable[[str], None]


def load_config(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def save_config(path: Path, cfg: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, sort_keys=False, allow_unicode=True)


def migrate_config(cfg: dict, default_path: Path | None = None) -> bool:
    """Add any missing built-in fields without overwriting the user's choices.

    Returns True if the in-memory config was changed (caller may save).
    """
    changed = False
    defaults = {}
    if default_path and default_path.exists():
        try:
            defaults = load_config(default_path)
        except Exception:  # noqa: BLE001
            defaults = {}

    default_tags = defaults.get("tags") or {}
    tags = dict(cfg.get("tags") or {})
    for key in BUILTIN_HF_KEYS:
        if key not in tags and key in default_tags:
            tags[key] = default_tags[key]
            changed = True
    if tags != (cfg.get("tags") or {}):
        cfg["tags"] = tags
        changed = True

    default_gh = defaults.get("github_tags") or {}
    gh_tags = dict(cfg.get("github_tags") or {})
    for key in BUILTIN_GH_KEYS:
        if key not in gh_tags and key in default_gh:
            gh_tags[key] = default_gh[key]
            changed = True
    if gh_tags and gh_tags != (cfg.get("github_tags") or {}):
        cfg["github_tags"] = gh_tags
        changed = True

    if "custom_tags" not in cfg:
        cfg["custom_tags"] = {}
        changed = True
    if "github_custom_tags" not in cfg:
        cfg["github_custom_tags"] = {}
        changed = True
    if "github_allow_list" not in cfg:
        cfg["github_allow_list"] = []
        changed = True

    hf_keys = list(all_hf_tags(cfg).keys())
    gh_keys = list(all_github_tags(cfg).keys())
    for key, machine in (cfg.get("machines") or {}).items():
        if machine.get("enabled") is False:
            continue
        if not machine.get("hf_categories"):
            machine["hf_categories"] = [k for k in BUILTIN_HF_KEYS if k in hf_keys]
            changed = True
        if not machine.get("github_categories"):
            machine["github_categories"] = [k for k in BUILTIN_GH_KEYS if k in gh_keys]
            changed = True
    return changed


def detect_os() -> str:
    import platform

    name = platform.system()
    if name == "Darwin":
        return "macOS"
    if name == "Windows":
        return "Windows"
    return name or "unknown"


def suggest_machine(cfg: dict | None = None) -> str:
    """Pick a sensible preset for this computer from machines.yaml."""
    os_name = detect_os()
    machines = enabled_machines(cfg or {})
    keys = list(machines.keys())

    def first_prefix(prefix: str) -> str | None:
        for key in keys:
            if key.startswith(prefix):
                return key
        return None

    if os_name == "macOS":
        return first_prefix("mac_") or (keys[0] if keys else "mac_16gb")
    return first_prefix("pc_") or (keys[0] if keys else "pc_32gb_gpu")


def enabled_machines(cfg: dict) -> dict[str, dict]:
    out = {}
    for key, machine in (cfg.get("machines") or {}).items():
        if machine.get("enabled") is False:
            continue
        out[key] = machine
    return out


def all_hf_tags(cfg: dict) -> dict[str, dict]:
    tags = dict(cfg.get("tags") or {})
    for key, val in (cfg.get("custom_tags") or {}).items():
        item = dict(val or {})
        item["custom"] = True
        tags[key] = item
    return tags


def all_github_tags(cfg: dict) -> dict[str, dict]:
    tags = dict(cfg.get("github_tags") or {})
    for key, val in (cfg.get("github_custom_tags") or {}).items():
        item = dict(val or {})
        item["custom"] = True
        tags[key] = item
    return tags


def category_payload(cfg: dict, machine_key: str) -> dict:
    machine = (cfg.get("machines") or {}).get(machine_key) or {}
    selected_hf = list(machine.get("hf_categories") or [])
    selected_gh = list(machine.get("github_categories") or [])
    hf = []
    for key, tag in all_hf_tags(cfg).items():
        hf.append(_cat_brief(key, tag, selected_hf, scope="hf"))
    gh = []
    for key, tag in all_github_tags(cfg).items():
        gh.append(_cat_brief(key, tag, selected_gh, scope="github"))
    return {"hf": hf, "github": gh}


def _cat_brief(key: str, tag: dict, selected: list[str], scope: str) -> dict:
    return {
        "key": key,
        "name": tag.get("name") or key.replace("_", " ").title(),
        "description": tag.get("description") or "",
        "keep": int(tag.get("keep") or 1),
        "selected": key in selected if selected else False,
        "custom": bool(tag.get("custom")),
        "allow_nongguf": bool(tag.get("allow_nongguf")),
        "scope": scope,
        "search": " ".join(tag.get("hf_queries") or tag.get("queries") or [tag.get("hf_search") or ""]),
    }


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")
    return slug or "custom"


def add_custom_category(
    cfg: dict,
    scope: str,
    name: str,
    search: str,
    keep: int = 2,
    allow_nongguf: bool = False,
) -> str:
    name = (name or "").strip()
    search = (search or "").strip()
    if not name:
        raise ValueError("Please give the category a name.")
    if not search:
        raise ValueError("Please add a few search words.")
    keep = max(1, min(int(keep or 1), 10))
    slug = slugify(name)
    bucket = "custom_tags" if scope == "hf" else "github_custom_tags"
    existing = dict(cfg.get(bucket) or {})
    # Avoid colliding with built-ins or an existing custom key.
    all_keys = set(all_hf_tags(cfg) if scope == "hf" else all_github_tags(cfg))
    base = slug
    n = 2
    while slug in all_keys:
        slug = f"{base}_{n}"
        n += 1
    if scope == "hf":
        words = [w for w in re.split(r"\s+", search) if w]
        existing[slug] = {
            "name": name,
            "description": "A category you added.",
            "hf_search": search,
            "hf_queries": [search],
            "keywords": words,
            "fuzzy_card": True,
            "allow_nongguf": bool(allow_nongguf),
            "keep": keep,
            "custom": True,
        }
    else:
        existing[slug] = {
            "name": name,
            "description": "A category you added.",
            "queries": [search],
            "keep": keep,
            "custom": True,
        }
    cfg[bucket] = existing
    return slug


def remove_custom_category(cfg: dict, scope: str, key: str) -> None:
    bucket = "custom_tags" if scope == "hf" else "github_custom_tags"
    existing = dict(cfg.get(bucket) or {})
    if key not in existing:
        raise ValueError("Only categories you added can be removed. Untick a built-in one to skip it.")
    existing.pop(key, None)
    cfg[bucket] = existing
    for machine in (cfg.get("machines") or {}).values():
        for field in ("hf_categories", "github_categories"):
            if field in machine and key in (machine.get(field) or []):
                machine[field] = [k for k in machine[field] if k != key]


def set_machine_categories(cfg: dict, machine_key: str, scope: str, keys: list[str]) -> None:
    machines = cfg.get("machines") or {}
    if machine_key not in machines:
        raise ValueError(f"Unknown machine: {machine_key}")
    valid = set(all_hf_tags(cfg) if scope == "hf" else all_github_tags(cfg))
    clean = [k for k in keys if k in valid]
    field = "hf_categories" if scope == "hf" else "github_categories"
    machines[machine_key][field] = clean


def base_dir(cfg: dict, root: Path | None = None) -> Path:
    root = root or ROOT
    paths = cfg.get("paths") or {}
    raw = (paths.get("base") or "").strip()
    if not raw:
        return root
    return Path(raw).expanduser()


def incoming_dir(cfg: dict, machine: str, tag: str, root: Path | None = None) -> Path:
    machine = _safe_component(machine, "computer")
    tag = _safe_component(tag, "category")
    dest = incoming_root(cfg, root) / machine / tag
    assert_contained(dest, incoming_root(cfg, root))
    return dest


def retired_dir(cfg: dict, machine: str, tag: str, root: Path | None = None) -> Path:
    machine = _safe_component(machine, "computer")
    tag = _safe_component(tag, "category")
    dest = retired_root(cfg, root) / machine / tag
    assert_contained(dest, retired_root(cfg, root))
    return dest


def github_incoming_dir(cfg: dict, category: str, root: Path | None = None) -> Path:
    category = _safe_component(category, "category")
    dest = incoming_root(cfg, root) / "github" / category
    assert_contained(dest, incoming_root(cfg, root))
    return dest


def github_retired_dir(cfg: dict, category: str, root: Path | None = None) -> Path:
    category = _safe_component(category, "category")
    dest = retired_root(cfg, root) / "github" / category
    assert_contained(dest, retired_root(cfg, root))
    return dest


def is_sidecar(path: str) -> bool:
    """True if this looks like a multimodal companion (mmproj, projector, CLIP, vision-encoder)."""
    return companion_role(path) is not None


def companion_role(path: str) -> str | None:
    """Return the companion kind, or None if this is an ordinary file."""
    name = (path or "").rsplit("/", 1)[-1]
    if not name:
        return None
    if re.search(r"(^|[/_.-])mmproj([/_.-]|$)", name, re.I):
        return "mmproj"
    if re.search(r"(^|[/_.-])projector([/_.-]|$)", name, re.I):
        return "projector"
    if re.search(r"(^|[/_.-])vision[_.-]?encoder([/_.-]|$)", name, re.I):
        return "vision-encoder"
    if re.search(r"(^|[/_.-])clip([/_.-]|$)", name, re.I):
        return "clip"
    return None


# Extra destination buckets the UI may send that are not in machines.yaml.
SPECIAL_HF_CATEGORIES = frozenset({"manual", "misc", "top_picks"})
SPECIAL_GH_CATEGORIES = frozenset({"misc"})


def is_safe_path_component(name: str) -> bool:
    """True if `name` is a single folder name with no traversal or absolute-path tricks."""
    if not name or not isinstance(name, str):
        return False
    if name in {".", ".."}:
        return False
    if "/" in name or "\\" in name:
        return False
    if any(ord(c) < 32 or c == "\x7f" for c in name):
        return False
    if ".." in name:
        return False
    # Windows drive letters and UNC-style prefixes.
    if len(name) >= 2 and name[1] == ":":
        return False
    try:
        if Path(name).is_absolute():
            return False
        if Path(name).parts != (name,):
            return False
    except (OSError, ValueError):
        return False
    return True


def _safe_component(name: str, kind: str = "folder") -> str:
    name = (name or "").strip()
    if not is_safe_path_component(name):
        raise ValueError(f"That {kind} name is not allowed.")
    return name


def validate_category_key(
    cfg: dict,
    key: str,
    scope: str = "hf",
    extra: set[str] | frozenset[str] | None = None,
) -> str:
    """Reject path tricks and unknown category keys. Returns the cleaned key."""
    key = _safe_component(key, "category")
    known = set(all_hf_tags(cfg) if scope == "hf" else all_github_tags(cfg))
    if extra:
        known |= set(extra)
    else:
        known |= set(SPECIAL_HF_CATEGORIES if scope == "hf" else SPECIAL_GH_CATEGORIES)
    if key not in known:
        raise ValueError("Unknown category.")
    return key


def validate_machine_key(cfg: dict, key: str, *, must_be_enabled: bool = False) -> str:
    """Reject path tricks and unknown computer keys."""
    key = _safe_component(key, "computer")
    machines = cfg.get("machines") or {}
    if key not in machines:
        raise ValueError(f"Unknown machine: {key}")
    if must_be_enabled and key not in enabled_machines(cfg):
        raise ValueError(f"Unknown machine: {key}")
    return key


def assert_contained(path: Path, root: Path) -> Path:
    """Resolve `path` and require it to stay under `root`. Raises ValueError if not."""
    resolved = path.resolve()
    root_res = root.resolve()
    if resolved != root_res and root_res not in resolved.parents:
        raise ValueError("That path is not inside the intended folder.")
    return resolved


def incoming_root(cfg: dict, root: Path | None = None) -> Path:
    paths = cfg.get("paths") or {}
    incoming = _safe_component(paths.get("incoming") or "incoming", "folder")
    return base_dir(cfg, root) / incoming


def retired_root(cfg: dict, root: Path | None = None) -> Path:
    paths = cfg.get("paths") or {}
    retired = _safe_component(paths.get("retired") or "retired", "folder")
    return base_dir(cfg, root) / retired


def find_companions(files: list[dict], pick: dict | None) -> list[dict]:
    """If a repo has multimodal companion files, pick the ones that match `pick`.

    Ordinary text GGUFs have no companions and nothing is added. When mmproj
    (or projector / CLIP / vision-encoder) files are present, the matching
    companion for the recommended quant is returned so it can be offered and
    downloaded together.
    """
    if not pick:
        return []
    pick_path = pick.get("path") or ""
    if not pick_path or pick_path == "(whole model)" or companion_role(pick_path):
        return []
    by_role: dict[str, list[dict]] = {}
    for f in files or []:
        path = f.get("path") or ""
        role = companion_role(path)
        if not role:
            continue
        row = dict(f)
        row["role"] = role
        by_role.setdefault(role, []).append(row)
    if not by_role:
        return []

    pick_name = pick_path.rsplit("/", 1)[-1].lower()
    pick_dir = "/".join(pick_path.split("/")[:-1])

    def _score(entry: dict) -> tuple:
        path = entry.get("path") or ""
        name = path.rsplit("/", 1)[-1].lower()
        same_dir = 1 if "/".join(path.split("/")[:-1]) == pick_dir else 0
        prefix = 0
        for a, b in zip(pick_name, name):
            if a == b:
                prefix += 1
            else:
                break
        # Typical multimodal packs ship an f16 mmproj.
        pref = 1 if re.search(r"(^|[/_.-])(f16|fp16)([/_.-]|$)", name) else 0
        return (same_dir, prefix, pref, -(entry.get("size") or 0))

    chosen: list[dict] = []
    for role in ("mmproj", "projector", "clip", "vision-encoder"):
        group = by_role.get(role) or []
        if not group:
            continue
        if len(group) == 1:
            chosen.append(group[0])
        else:
            chosen.append(max(group, key=_score))
    return chosen


def estimate_runtime_memory_gb(stored_gb: float, fmt: str = "gguf") -> float:
    """Very rough estimated RAM/VRAM to load and run the weights.

    This is a first approximation: the stored file size plus a little room
    for context (the KV cache) and the runtime itself. Actual use depends
    on context length, offload, architecture, and the app. Always label
    the result as a rough estimate in the interface.
    """
    stored_gb = max(0.0, float(stored_gb or 0))
    fmt = (fmt or "").lower()
    if fmt in {"gguf", "mlx"}:
        return round(stored_gb * 1.15 + 0.5, 1)
    return round(stored_gb * 1.25 + 0.5, 1)


def describe_fit(
    stored_gb: float,
    max_gb: float | None,
    estimated_ram_gb: float | None = None,
    *,
    whole_gb: float | None = None,
    pick_fits: bool | None = None,
    fmt: str = "gguf",
) -> dict:
    """Distinguish on-disk size from a rough estimated memory need."""
    stored_gb = float(stored_gb or 0)
    if estimated_ram_gb is None:
        estimated_ram_gb = estimate_runtime_memory_gb(stored_gb, fmt)
    over_cap = bool(max_gb and stored_gb > float(max_gb))
    whole_over = bool(max_gb and whole_gb and float(whole_gb) > float(max_gb))
    warning = ""
    if max_gb and (over_cap or whole_over):
        if pick_fits and whole_over and not over_cap:
            warning = (
                f"On disk the whole folder is about {_size_label(float(whole_gb))}, "
                f"which is over this computer's {_size_label(float(max_gb))} size cap. "
                f"The suggested GGUF file is {_size_label(stored_gb)} on disk and should fit. "
                f"Estimated memory to run that file: about {_size_label(float(estimated_ram_gb))} "
                f"(rough estimate). You can still download either one."
            )
        else:
            warning = (
                f"On disk this is about {_size_label(stored_gb)} "
                f"(this computer's size cap is {_size_label(float(max_gb))}). "
                f"Estimated memory to run: about {_size_label(float(estimated_ram_gb))} "
                f"(rough estimate). You can still download it if you have the disk space, "
                f"but it may not run well here. Actual memory use also depends on "
                f"context length and the app you run it in."
            )
    return {
        "stored_size_gb": stored_gb,
        "estimated_ram_gb": estimated_ram_gb,
        "estimate_label": "rough estimate",
        "over_cap": over_cap,
        "whole_over_cap": whole_over,
        "warning": warning,
    }


def id_blocked(model_id: str, exclude_id_substrings: list[str]) -> bool:
    low = model_id.lower()
    return any(s.lower() in low for s in exclude_id_substrings)


def _retry(fn, tries: int = 3, delay: float = 1.5):
    last = None
    for i in range(tries):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            last = exc
            if i == tries - 1:
                raise
            time.sleep(delay * (i + 1))
    raise last  # pragma: no cover



# Hugging Face list_models sort keys. "trending" uses trendingScore when the
# API accepts it (confirmed against the public API in November 2026) and
# falls back to likes if a given server rejects it. "newest" uses lastModified.
HF_SORTS = {
    "downloads": "downloads",
    "likes": "likes",
    "trending": "trendingScore",
    "newest": "lastModified",
}
HF_SORT_ALIASES = {
    "download": "downloads",
    "downloads": "downloads",
    "most_downloaded": "downloads",
    "most-downloaded": "downloads",
    "like": "likes",
    "likes": "likes",
    "most_liked": "likes",
    "most-liked": "likes",
    "trending": "trending",
    "trendingscore": "trending",
    "trending_score": "trending",
    "trending-score": "trending",
    "newest": "newest",
    "new": "newest",
    "lastmodified": "newest",
    "last_modified": "newest",
    "last-modified": "newest",
    "created": "newest",
    "createdat": "newest",
    "created_at": "newest",
    "created-at": "newest",
}
# None = not yet tried, True/False after the first trendingScore call.
_HF_TRENDING_OK: bool | None = None
_HF_SORT_FALLBACK: str | None = None

POPULARITY_DISCLAIMER = (
    "These are the models Hugging Face users have downloaded, liked, or that "
    "are trending right now, limited to ones that should fit this computer. "
    "Popularity is a community signal, not a guarantee of quality."
)


def normalize_hf_sort(sort: str | None) -> str:
    key = (sort or "downloads").strip().lower().replace(" ", "_")
    return HF_SORT_ALIASES.get(key, "downloads")


def hf_api_sort(sort_key: str) -> str:
    return HF_SORTS.get(sort_key, "downloads")


def reset_hf_sort_state() -> None:
    """Clear the trending-support cache. Used by tests."""
    global _HF_TRENDING_OK, _HF_SORT_FALLBACK
    _HF_TRENDING_OK = None
    _HF_SORT_FALLBACK = None


def _date_value(text: str | None) -> str:
    return (text or "").strip()


def sort_model_key(sort_key: str):
    """Return a key function for sorting model dicts (lower is better when used with default sort? No — we sort reverse via negative numbers)."""

    def _key(item: dict):
        if sort_key == "likes":
            return (-(item.get("likes") or 0), -(item.get("downloads") or 0), item.get("id") or "")
        if sort_key == "trending":
            return (
                -(item.get("trending_score") or 0),
                -(item.get("likes") or 0),
                -(item.get("downloads") or 0),
                item.get("id") or "",
            )
        if sort_key == "newest":
            return (
                _date_value(item.get("lastModified") or item.get("createdAt")),
                -(item.get("downloads") or 0),
                item.get("id") or "",
            )
        return (-(item.get("downloads") or 0), -(item.get("likes") or 0), item.get("id") or "")

    return _key


def sort_models(models: list[dict], sort_key: str) -> list[dict]:
    if sort_key == "newest":
        # ISO dates sort lexicographically; reverse so the newest is first.
        # Downloads stay as a tie-breaker with ordinary descending order.
        return sorted(
            models,
            key=lambda item: (
                _date_value(item.get("lastModified") or item.get("createdAt")),
                item.get("downloads") or 0,
                item.get("id") or "",
            ),
            reverse=True,
        )
    return sorted(models, key=sort_model_key(sort_key))


def search_models(
    query: str,
    limit: int = 40,
    pipeline_tag: str | None = None,
    gguf_only: bool = False,
    author: str | None = None,
    sort: str | None = "downloads",
    library: str | None = None,
) -> list[dict]:
    """Search (or list) Hugging Face models. An empty query lists by sort only."""
    global _HF_TRENDING_OK, _HF_SORT_FALLBACK
    sort_key = normalize_hf_sort(sort)
    api_sort = hf_api_sort(sort_key)
    if sort_key == "trending" and _HF_TRENDING_OK is False:
        api_sort = "likes"
        _HF_SORT_FALLBACK = "likes"
    limit = max(1, min(int(limit or 40), 150))

    def _call(use_sort: str):
        kwargs: dict[str, Any] = {
            "limit": limit,
            "sort": use_sort,
        }
        if query:
            kwargs["search"] = query
        if gguf_only:
            kwargs["filter"] = "gguf"
        if library:
            kwargs["library"] = library
        if pipeline_tag:
            kwargs["pipeline_tag"] = pipeline_tag
        if author:
            kwargs["author"] = author
        return list(HF.list_models(**kwargs))

    def _attempt():
        global _HF_TRENDING_OK, _HF_SORT_FALLBACK
        try:
            models = _call(api_sort)
            if api_sort == "trendingScore":
                _HF_TRENDING_OK = True
                _HF_SORT_FALLBACK = None
            return models
        except Exception as exc:  # noqa: BLE001
            if api_sort == "trendingScore":
                _HF_TRENDING_OK = False
                _HF_SORT_FALLBACK = "likes"
                return _call("likes")
            raise exc

    models = _retry(_attempt)
    briefs = [_model_brief(m) for m in models]
    # The API already ordered them; re-sort locally so a merged pool stays consistent.
    return sort_models(briefs, sort_key)


def search_gguf_models(
    query: str,
    limit: int = 40,
    pipeline_tag: str | None = None,
    sort: str | None = "downloads",
) -> list[dict]:
    return search_models(query, limit=limit, pipeline_tag=pipeline_tag, gguf_only=True, sort=sort)


def search_mlx_models(
    query: str, limit: int = 40, sort: str | None = "downloads"
) -> list[dict]:
    return search_models(query, limit=limit, author="mlx-community", sort=sort)


def _model_brief(m: Any) -> dict:
    mid = getattr(m, "id", None) or ""
    tags = list(getattr(m, "tags", None) or [])
    trending = getattr(m, "trending_score", None)
    if trending is None:
        trending = getattr(m, "trendingScore", None) or 0
    last_mod = getattr(m, "last_modified", None) or getattr(m, "lastModified", None) or ""
    created = getattr(m, "created_at", None) or getattr(m, "createdAt", None) or ""
    return {
        "id": mid,
        "downloads": getattr(m, "downloads", None) or 0,
        "likes": getattr(m, "likes", None) or 0,
        "trending_score": trending or 0,
        "lastModified": str(last_mod or ""),
        "createdAt": str(created or ""),
        "card_url": f"https://huggingface.co/{mid}",
        "tags": tags,
        "pipeline_tag": getattr(m, "pipeline_tag", None) or "",
    }


def list_gguf_files(model_id: str) -> list[dict]:
    def _call():
        return list(HF.list_repo_tree(model_id, recursive=True))

    try:
        tree = _retry(_call)
    except HfHubHTTPError as exc:
        if getattr(exc, "response", None) is not None and exc.response.status_code == 404:
            return []
        raise
    out = []
    for item in tree:
        path = getattr(item, "path", None) or ""
        size = getattr(item, "size", None)
        if not path.lower().endswith(".gguf"):
            continue
        if not isinstance(size, (int, float)):
            continue
        out.append({"path": path, "size": int(size), "size_gb": size / (1024**3), "format": "gguf"})
    return out


def list_repo_weights(model_id: str) -> dict | None:
    """Sum weight files for a non-GGUF (or mixed) repo, and guess the format."""

    def _call():
        return list(HF.list_repo_tree(model_id, recursive=True))

    try:
        tree = _retry(_call)
    except HfHubHTTPError as exc:
        if getattr(exc, "response", None) is not None and exc.response.status_code == 404:
            return None
        raise
    total = 0
    files = 0
    names: list[str] = []
    for item in tree:
        path = getattr(item, "path", None) or ""
        size = getattr(item, "size", None)
        if not isinstance(size, (int, float)):
            continue
        suffix = Path(path).suffix.lower()
        name = path.rsplit("/", 1)[-1].lower()
        if name in {".gitattributes", "license", "license.md", "license.txt"}:
            continue
        if suffix in WEIGHT_SUFFIXES or name.endswith(".safetensors") or "model.safetensors" in name:
            total += int(size)
            files += 1
            names.append(path)
    if files == 0 or total <= 0:
        return None
    fmt = _guess_format_from_files(names)
    return {
        "path": "(whole model)",
        "size": total,
        "size_gb": total / (1024**3),
        "format": fmt,
        "file_count": files,
    }


def list_mlx_repo(model_id: str) -> dict | None:
    repo = list_repo_weights(model_id)
    if not repo:
        return None
    repo["format"] = "mlx"
    return repo


def _guess_format_from_files(paths: list[str]) -> str:
    joined = " ".join(paths).lower()
    if any(p.endswith(".gguf") for p in paths) and all(
        Path(p).suffix.lower() in {".gguf", ""} or True for p in paths
    ):
        if any(p.endswith(".gguf") for p in paths) and not any(
            Path(p).suffix.lower() in {".safetensors", ".bin", ".pt", ".onnx"} for p in paths
        ):
            return "gguf"
    suffixes = {Path(p).suffix.lower() for p in paths}
    if ".gguf" in suffixes and not (suffixes & {".safetensors", ".bin", ".pt", ".onnx"}):
        return "gguf"
    if ".mlmodel" in suffixes or "coreml" in joined or ".mlpackage" in joined:
        return "coreml"
    if ".onnx" in suffixes:
        return "onnx"
    if ".safetensors" in suffixes:
        return "safetensors"
    if ".bin" in suffixes or ".pt" in suffixes or ".pth" in suffixes:
        return "pytorch"
    if ".tflite" in suffixes:
        return "tflite"
    return "weights"


def guess_format_and_note(model: dict, pick: dict | None = None) -> tuple[str, str]:
    """Return (format, one-line note about what app can run it)."""
    mid = (model.get("id") or "").lower()
    tags = [t.lower() for t in (model.get("tags") or [])]
    pipe = (model.get("pipeline_tag") or "").lower()
    fmt = (pick or {}).get("format") or ""
    blob = " ".join([mid, " ".join(tags), pipe, fmt])

    if "ace-step" in blob or "acestep" in blob:
        return fmt or "ace-step", "Music generation. Usually run with the ACE-Step app or ComfyUI."
    if re.search(r"yue", blob):
        return fmt or "yue", "Song generation. Usually run with the YuE project tools."
    if "musicgen" in blob or "audiocraft" in blob:
        return fmt or "musicgen", "Music generation. Often run with Audiocraft, ComfyUI, or the model's own app."
    if "rvc" in blob or "so-vits" in blob or "sovits" in blob or "voice conversion" in blob:
        return fmt or "rvc", "Voice conversion / cloning. Often run with RVC, so-vits-svc, or Applio."
    if "whisper" in blob or pipe == "automatic-speech-recognition":
        if "coreml" in blob or "whisperkit" in blob:
            return "coreml", "Speech-to-text for Apple Silicon (WhisperKit / Core ML)."
        return fmt or "whisper", "Speech-to-text. Try whisper.cpp, faster-whisper, or a whisper GUI."
    if "piper" in blob:
        return fmt or "piper", "Text-to-speech. Runs in Piper and many voice apps that support it."
    if "kokoro" in blob:
        return fmt or "tts", "Text-to-speech. Usually run with Kokoro or a Kokoro GUI."
    if "xtts" in blob or "text-to-speech" in pipe or "tts" in tags or pipe == "text-to-speech":
        return fmt or "tts", "Text-to-speech. The app depends on the model (Piper, Kokoro, XTTS, and others)."
    if fmt == "mlx" or mid.startswith("mlx-community/"):
        return "mlx", "Made for Apple Silicon. Runs in LM Studio or MLX."
    if fmt == "gguf" or "gguf" in tags:
        return "gguf", "Runs in Ollama, LM Studio, or koboldcpp."
    if fmt == "coreml" or "coreml" in blob:
        return "coreml", "Apple Core ML format. Runs on a Mac with Core ML compatible apps."
    if fmt == "onnx":
        return "onnx", "Runs in various apps that support ONNX."
    if "diffusers" in tags or "comfyui" in blob or pipe in {"text-to-audio", "text-to-image"}:
        return fmt or "diffusers", "Usually run with ComfyUI, Diffusers, or the model's own app."
    if fmt == "safetensors":
        return "safetensors", "Often used with Python tools such as Transformers or ComfyUI."
    if fmt == "pytorch":
        return "pytorch", "Often used with Python tools such as Transformers."
    if fmt:
        return fmt, "Check the model card for the app this one is meant to run in."
    return "weights", "Check the model card for the app this one is meant to run in."


def pick_fit(files: list[dict], max_gb: float) -> dict | None:
    """Prefer a mid quant that fits; fall back to largest that fits under cap."""
    primary = [f for f in files if not is_sidecar(f["path"])]
    pool_src = primary or files
    fitting = [f for f in pool_src if f["size_gb"] <= max_gb]
    if not fitting:
        return None
    preferred = []
    for f in fitting:
        name = f["path"].upper()
        if PREFERRED_QUANTS.search(name):
            preferred.append(f)
    pool = preferred or fitting
    target = max_gb * 0.7
    return min(pool, key=lambda f: abs(f["size_gb"] - target))


def _keyword_blob(model: dict, extra: str = "") -> str:
    parts = [
        model.get("id") or "",
        " ".join(model.get("tags") or []),
        model.get("pipeline_tag") or "",
        extra,
    ]
    return " ".join(parts).lower()


def matches_keywords(model: dict, keywords: list[str], extra: str = "") -> bool:
    if not keywords:
        return True
    blob = _keyword_blob(model, extra)
    for kw in keywords:
        kw = (kw or "").lower().strip()
        if kw and kw in blob:
            return True
    return False


def _fetch_card_text(model_id: str) -> str:
    try:
        info = HF.model_info(model_id)
    except Exception:  # noqa: BLE001
        return ""
    bits = []
    tags = getattr(info, "tags", None) or []
    bits.extend(str(t) for t in tags)
    card = getattr(info, "card_data", None)
    if card is not None:
        try:
            bits.append(str(card))
        except Exception:  # noqa: BLE001
            pass
    return " ".join(bits).lower()


def collect_suggestions(
    cfg: dict,
    machine_key: str,
    progress_cb: ProgressCb | None = None,
    search_limit: int = 80,
    categories: list[str] | None = None,
    *,
    sort: str | None = "downloads",
    already_ids: set[str] | None = None,
    seen_path: Path | None = None,
    record_seen: bool = True,
    keep_override: int | None = None,
    cap_override: float | None = None,
) -> dict:
    """Build a shortlist for one machine, only for the chosen categories.

    Each call returns the next fresh batch: models already shown (or already
    on this computer / already approved) are skipped, and a larger search
    pool is used so Refresh has something new to offer.
    """
    import seen_store

    machines = enabled_machines(cfg)
    if machine_key not in machines:
        raise ValueError(f"Unknown or disabled machine: {machine_key}")
    machine = machines[machine_key]
    formats = [f.lower() for f in (machine.get("formats") or ["gguf"])]
    cap = float(machine.get("max_model_gb") or 0)
    if cap_override is not None:
        cap = float(cap_override)
    tags = all_hf_tags(cfg)
    selected = categories if categories is not None else list(machine.get("hf_categories") or tags.keys())
    selected = [k for k in selected if k in tags]
    allow = set(cfg.get("allow_list") or [])
    exclude = set(already_ids or set()) | {a.lower() for a in allow}
    # Keep original-case allow ids too so comparisons can be exact or lowercased.
    exclude |= set(allow)
    sort_key = normalize_hf_sort(sort)
    search_limit = max(10, min(int(search_limit or 80), 150))

    result_tags = []
    for tag_name in selected:
        tag_cfg = tags[tag_name]
        result_tags.append(
            _collect_one_tag(
                tag_name=tag_name,
                tag_cfg=tag_cfg,
                formats=formats,
                cap=cap,
                allow=allow,
                search_limit=search_limit,
                progress_cb=progress_cb,
                sort=sort_key,
                machine_key=machine_key,
                exclude_ids=exclude,
                seen_path=seen_path,
                record_seen=record_seen,
                keep_override=keep_override,
            )
        )

    return {
        "machine": machine_key,
        "machine_name": machine.get("name") or machine_key,
        "max_model_gb": cap,
        "formats": formats,
        "sort": sort_key,
        "sort_fallback": _HF_SORT_FALLBACK,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "tags": result_tags,
    }


def _collect_one_tag(
    tag_name: str,
    tag_cfg: dict,
    formats: list[str],
    cap: float,
    allow: set[str],
    search_limit: int,
    progress_cb: ProgressCb | None,
    sort: str = "downloads",
    machine_key: str = "",
    exclude_ids: set[str] | None = None,
    seen_path: Path | None = None,
    record_seen: bool = True,
    keep_override: int | None = None,
) -> dict:
    import seen_store

    display = tag_cfg.get("name") or tag_name
    keep = int(tag_cfg.get("keep") or 1)
    if keep_override is not None:
        keep = max(keep, int(keep_override))
    cap_this = cap
    if tag_cfg.get("cap_gb"):
        cap_this = min(cap_this, float(tag_cfg["cap_gb"]))
    pipeline_tag = tag_cfg.get("pipeline_tag")
    use_pt = bool(tag_cfg.get("use_pipeline_tag"))
    blocked = list(tag_cfg.get("exclude_id_substrings") or [])
    allow_nongguf = bool(tag_cfg.get("allow_nongguf"))
    fuzzy = bool(tag_cfg.get("fuzzy_card"))
    keywords = list(tag_cfg.get("keywords") or [])
    queries = list(tag_cfg.get("hf_queries") or [])
    if not queries:
        queries = [tag_cfg.get("hf_search") or tag_name]
    tag_out: dict[str, Any] = {
        "name": tag_name,
        "title": display,
        "keep": keep,
        "description": tag_cfg.get("description") or "",
        "models": [],
        "error": None,
        "wrapped": False,
        "skipped_seen": 0,
        "skipped_already": 0,
        "sort": sort,
    }

    def note(msg: str) -> None:
        if progress_cb:
            progress_cb(msg)

    groups: list[tuple[str, list[dict]]] = []
    try:
        if "gguf" in formats:
            merged: dict[str, dict] = {}
            for q in queries:
                note(f"Searching Hugging Face for {display} (GGUF, “{q}”)…")
                for m in search_gguf_models(
                    q,
                    limit=search_limit,
                    pipeline_tag=pipeline_tag if use_pt else None,
                    sort=sort,
                ):
                    merged.setdefault(m["id"], m)
            groups.append(("gguf", list(merged.values())))
        if allow_nongguf:
            merged_any: dict[str, dict] = {}
            for q in queries:
                note(f"Searching Hugging Face for {display} (“{q}”)…")
                for m in search_models(
                    q,
                    limit=search_limit,
                    pipeline_tag=pipeline_tag if use_pt else None,
                    gguf_only=False,
                    sort=sort,
                ):
                    merged_any.setdefault(m["id"], m)
            groups.append(("any", list(merged_any.values())))
        if "mlx" in formats:
            merged_mlx: dict[str, dict] = {}
            for q in queries:
                note(f"Searching mlx-community for {display} (“{q}”)…")
                for m in search_mlx_models(q, limit=search_limit, sort=sort):
                    merged_mlx.setdefault(m["id"], m)
            groups.append(("mlx", list(merged_mlx.values())))
    except Exception as exc:  # noqa: BLE001
        tag_out["error"] = f"Search failed: {exc}"
        return tag_out

    seen: list[str] = []
    if machine_key:
        try:
            seen = seen_store.get_seen("hf", machine_key, tag_name, path=seen_path)
        except ValueError:
            seen = []

    all_fits: list[dict] = []
    for fmt, models in groups:
        ranked = _rank_models(
            models=models,
            fmt=fmt,
            cap=cap_this,
            keep=max(keep * 8, 16),  # inspect a larger fitting pool, then rotate
            exclude_ids=blocked,
            allow=allow,
            tag_name=tag_name,
            keywords=keywords if fuzzy else [],
            fuzzy=fuzzy,
            progress_cb=progress_cb,
            display=display,
            sort=sort,
            already_ids=exclude_ids,
        )
        have = {m["id"] for m in all_fits}
        for row in ranked:
            if row["id"] not in have:
                all_fits.append(row)
                have.add(row["id"])

    all_fits = sort_models(all_fits, sort)
    exclude_lower = {(e or "").lower() for e in (exclude_ids or set()) if e}
    skipped_already = 0
    # Count search hits we never inspected because they are already local or approved.
    raw_ids = {m.get("id") for _, models in groups for m in models if m.get("id")}
    skipped_already = sum(1 for i in raw_ids if i.lower() in exclude_lower)

    max_keep = keep * max(1, min(len(groups), 2))
    picked, meta = seen_store.pick_fresh(
        all_fits,
        id_of=lambda row: (row.get("id") or "").lower(),
        seen={(s or "").lower() for s in seen},
        exclude=exclude_lower,
        keep=max_keep,
    )
    chosen = sort_models(picked, sort)

    tag_out["models"] = chosen
    tag_out["wrapped"] = bool(meta.get("wrapped"))
    tag_out["skipped_seen"] = int(meta.get("skipped_seen") or 0)
    tag_out["skipped_already"] = skipped_already + int(meta.get("skipped_already") or 0)
    if tag_out["wrapped"]:
        tag_out["wrap_note"] = (
            "You've seen every current match in this category; starting the list over."
        )

    if record_seen and machine_key and chosen:
        try:
            seen_store.record_batch(
                "hf",
                machine_key,
                tag_name,
                [m["id"] for m in chosen],
                wrapped=bool(meta.get("wrapped")),
                path=seen_path,
            )
        except ValueError:
            pass
    return tag_out


def _rank_models(
    models: list[dict],
    fmt: str,
    cap: float,
    keep: int,
    exclude_ids: list[str],
    allow: set[str],
    tag_name: str,
    keywords: list[str],
    fuzzy: bool,
    progress_cb: ProgressCb | None,
    display: str = "",
    sort: str = "downloads",
    already_ids: set[str] | None = None,
) -> list[dict]:
    already = {a.lower() for a in (already_ids or set())}
    candidates = []
    for m in models:
        mid = m.get("id") or ""
        if not mid or id_blocked(mid, exclude_ids):
            continue
        if mid.lower() in already:
            continue
        if keywords and not matches_keywords(m, keywords):
            if not fuzzy:
                continue
            m = dict(m)
            m["_needs_card"] = True
        candidates.append(m)

    # Prefer obvious keyword hits, then the requested sort.
    def _pref(item: dict):
        sort_k = sort_model_key(sort)(item)
        # keyword misses (needs card) go after definite hits
        flag = 1 if item.get("_needs_card") else 0
        return (flag, sort_k)

    reverse = sort == "newest"
    # For newest, sort_model_key is a date string (ascending unless reverse).
    # _pref puts flag first (0 before 1). Use a stable two-step:
    definite = [m for m in candidates if not m.get("_needs_card")]
    needs_card = [m for m in candidates if m.get("_needs_card")]
    definite = sort_models(definite, sort)
    needs_card = sort_models(needs_card, sort)
    inspect_cap = 50
    extras = needs_card[:8]
    candidates = (definite + extras)[:inspect_cap]

    total = len(candidates)
    label = {"gguf": "GGUF", "mlx": "MLX", "any": "models"}.get(fmt, fmt)

    def inspect_one(m: dict) -> dict | None:
        mid = m["id"]
        extra_text = ""
        if m.get("_needs_card"):
            extra_text = _fetch_card_text(mid)
            if not matches_keywords(m, keywords, extra_text):
                return None
        files: list[dict] = []
        try:
            if fmt == "gguf":
                files = list_gguf_files(mid)
                primary = [f for f in files if not is_sidecar(f["path"])]
                if not primary:
                    return None
                pick = pick_fit(files, cap)
            elif fmt == "mlx":
                repo = list_mlx_repo(mid)
                if not repo or repo["size_gb"] > cap:
                    return None
                pick = repo
            else:
                # Non-GGUF / mixed: prefer a GGUF pick if the repo has one that fits,
                # otherwise the whole-repo weight size must fit the cap.
                files = list_gguf_files(mid)
                primary = [f for f in files if not is_sidecar(f["path"])]
                pick = pick_fit(files, cap) if primary else None
                if not pick:
                    repo = list_repo_weights(mid)
                    if not repo or repo["size_gb"] > cap:
                        return None
                    pick = repo
        except Exception:  # noqa: BLE001
            return None
        if not pick:
            return None
        fmt_name, app_note = guess_format_and_note(m, pick)
        companions = find_companions(files, pick)
        estimated = estimate_runtime_memory_gb(pick["size_gb"], fmt_name)
        return {
            "id": mid,
            "downloads": m.get("downloads") or 0,
            "likes": m.get("likes") or 0,
            "trending_score": m.get("trending_score") or 0,
            "lastModified": m.get("lastModified") or "",
            "createdAt": m.get("createdAt") or "",
            "card_url": m.get("card_url") or f"https://huggingface.co/{mid}",
            "file": (
                pick["path"].rsplit("/", 1)[-1]
                if pick["path"] != "(whole model)"
                else "(whole model)"
            ),
            "file_path": pick["path"],
            "size": pick.get("size") or 0,
            "size_gb": pick["size_gb"],
            "stored_size_gb": pick["size_gb"],
            "estimated_ram_gb": estimated,
            "estimate_label": "rough estimate",
            "format": fmt_name,
            "app_note": app_note,
            "companions": companions,
            "is_multimodal": bool(companions),
            "allowed": mid in allow,
            "tag": tag_name,
            "error": None,
        }

    picks: list[dict] = []
    done = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = {pool.submit(inspect_one, m): m for m in candidates}
        for fut in as_completed(futs):
            done += 1
            if progress_cb:
                progress_cb(f"Checking {display or tag_name} {label}… {done}/{total}")
            row = fut.result()
            if not row:
                continue
            picks.append(row)

    picks = sort_models(picks, sort)
    return picks[:keep]


def collect_top_picks(
    cfg: dict,
    machine_key: str,
    progress_cb: ProgressCb | None = None,
    sort: str | None = "downloads",
    categories: list[str] | None = None,
    search_limit: int = 50,
    keep: int = 8,
    already_ids: set[str] | None = None,
) -> dict:
    """List popular Hugging Face models that should fit this computer.

    This is a no-keyword view: models are ordered by community popularity
    (downloads, likes, or trending) or by how recently they were updated,
    then filtered by the computer's size cap and by GGUF / MLX where those
    formats are what the computer runs. Pass category keys to also include
    a shortlist for each one (using that category's pipeline tag or name
    as a type filter, not as a keyword search when a pipeline tag exists).
    """
    machines = enabled_machines(cfg)
    if machine_key not in machines:
        raise ValueError(f"Unknown or disabled machine: {machine_key}")
    machine = machines[machine_key]
    formats = [f.lower() for f in (machine.get("formats") or ["gguf"])]
    cap = float(machine.get("max_model_gb") or 0)
    allow = set(cfg.get("allow_list") or [])
    sort_key = normalize_hf_sort(sort)
    search_limit = max(10, min(int(search_limit or 50), 150))
    keep = max(1, min(int(keep or 8), 20))

    def note(msg: str) -> None:
        if progress_cb:
            progress_cb(msg)

    def _fits_for(models: list[dict], tag_name: str, display: str, how_many: int) -> list[dict]:
        groups: list[tuple[str, list[dict]]] = []
        if "gguf" in formats:
            groups.append(("gguf", [m for m in models if _looks_gguf(m)]))
            # If the list was already requested with filter=gguf, keep them all.
            if not groups[-1][1] and models:
                groups[-1] = ("gguf", list(models))
        if "mlx" in formats:
            mlx = [m for m in models if "mlx" in (m.get("id") or "").lower() or "mlx" in " ".join(m.get("tags") or []).lower()]
            if mlx:
                groups.append(("mlx", mlx))
        # If we didn't classify, still try as GGUF when that's the machine format.
        if not groups and "gguf" in formats:
            groups.append(("gguf", list(models)))
        elif not groups and formats:
            groups.append(("any", list(models)))
        collected: list[dict] = []
        have: set[str] = set()
        for fmt, group in groups:
            ranked = _rank_models(
                models=group,
                fmt=fmt if fmt != "any" else "any",
                cap=cap,
                keep=how_many,
                exclude_ids=[],
                allow=allow,
                tag_name=tag_name,
                keywords=[],
                fuzzy=False,
                progress_cb=progress_cb,
                display=display,
                sort=sort_key,
                already_ids=None,  # mark later; do not hide popular models already on disk
            )
            for row in ranked:
                if row["id"] not in have:
                    collected.append(row)
                    have.add(row["id"])
        return sort_models(collected, sort_key)[:how_many]

    overall_models: list[dict] = []
    try:
        if "gguf" in formats:
            note("Looking up popular GGUF models on Hugging Face…")
            overall_models.extend(search_models("", limit=search_limit, gguf_only=True, sort=sort_key))
        if "mlx" in formats:
            note("Looking up popular MLX models…")
            for m in search_mlx_models("", limit=search_limit, sort=sort_key):
                if m["id"] not in {x["id"] for x in overall_models}:
                    overall_models.append(m)
        if not overall_models:
            note("Looking up popular models on Hugging Face…")
            overall_models = search_models("", limit=search_limit, sort=sort_key)
    except Exception as exc:  # noqa: BLE001
        overall_error = f"Search failed: {exc}"
        overall = []
    else:
        overall_error = None
        overall = _fits_for(overall_models, "top_picks", "Top picks", keep)

    category_slots: list[dict] = []
    tags = all_hf_tags(cfg)
    selected = [k for k in (categories or []) if k in tags]
    for tag_name in selected:
        tag_cfg = tags[tag_name]
        display = tag_cfg.get("name") or tag_name
        pipeline_tag = tag_cfg.get("pipeline_tag") if tag_cfg.get("use_pipeline_tag") else None
        # When the category has a pipeline tag we can use it as a type filter
        # without keyword search. Otherwise we list popular GGUF models and
        # rely on the pipeline_tag field when present, falling back to a
        # single broad query so the section is not empty.
        slot: dict[str, Any] = {
            "name": tag_name,
            "title": display,
            "keep": min(int(tag_cfg.get("keep") or 2), keep),
            "description": tag_cfg.get("description") or "",
            "models": [],
            "error": None,
        }
        try:
            note(f"Looking up popular {display} models…")
            found: dict[str, dict] = {}
            if pipeline_tag:
                for m in search_models(
                    "",
                    limit=search_limit,
                    pipeline_tag=pipeline_tag,
                    gguf_only="gguf" in formats,
                    sort=sort_key,
                ):
                    found.setdefault(m["id"], m)
            else:
                # Use the first query only as a mild filter so "coding" top
                # picks stay in the coding neighbourhood, still sorted by
                # popularity rather than by keyword score.
                queries = list(tag_cfg.get("hf_queries") or [tag_cfg.get("hf_search") or display])
                q = queries[0] if queries else display
                for m in search_gguf_models(q, limit=search_limit, sort=sort_key) if "gguf" in formats else search_models(q, limit=search_limit, sort=sort_key):
                    found.setdefault(m["id"], m)
            slot["models"] = _fits_for(list(found.values()), tag_name, display, slot["keep"])
        except Exception as exc:  # noqa: BLE001
            slot["error"] = f"Search failed: {exc}"
        category_slots.append(slot)

    return {
        "machine": machine_key,
        "machine_name": machine.get("name") or machine_key,
        "max_model_gb": cap,
        "formats": formats,
        "sort": sort_key,
        "sort_fallback": _HF_SORT_FALLBACK,
        "disclaimer": POPULARITY_DISCLAIMER,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "overall": overall,
        "overall_error": overall_error,
        "tags": category_slots,
        "mode": "top_picks",
    }


def _looks_gguf(model: dict) -> bool:
    tags = [t.lower() for t in (model.get("tags") or [])]
    ident = (model.get("id") or "").lower()
    return "gguf" in tags or ident.endswith("-gguf") or "gguf" in ident


class DownloadProgress(tqdm if tqdm is not None else object):  # type: ignore[valid-type]
    """tqdm subclass that reports byte progress to a shared dict."""

    sink: dict | None = None
    _lock = threading.Lock()

    def __init__(self, *args, **kwargs):
        if tqdm is None:
            self.n = 0
            self.total = kwargs.get("total") or 0
            return
        super().__init__(*args, **kwargs)
        self._publish()

    def update(self, n=1):
        if tqdm is None:
            self.n += n
            self._publish()
            return
        result = super().update(n)
        self._publish()
        return result

    def _publish(self):
        sink = DownloadProgress.sink
        if not sink:
            return
        with DownloadProgress._lock:
            sink["downloaded_bytes"] = int(getattr(self, "n", 0) or 0)
            sink["total_bytes"] = int(getattr(self, "total", 0) or 0)
            total = sink["total_bytes"]
            if total:
                sink["percent"] = round(100.0 * sink["downloaded_bytes"] / total, 1)
            else:
                sink["percent"] = None



# --- resilient downloads -------------------------------------------------
# Test-only hooks. Leave these alone unless you are running the built-in tests.
_TEST_HOOKS: dict = {
    "fail_times": 0,
    "error": None,
    "sleep": None,
    "xet_disabled": False,
}

XET_ERROR_HINTS = (
    "cas client error",
    "file reconstruction error",
    "request middleware error",
    "hf_xet",
    "/xorbs/",
    "cdn.hf.co",
    "xet storage",
    "xet-cas",
    "xet_cas",
)
RETRYABLE_HINTS = (
    "timeout",
    "timed out",
    "temporarily",
    "connection reset",
    "connection aborted",
    "connection refused",
    "broken pipe",
    "reset by peer",
    "failed to establish",
    "network",
    "temporarily unavailable",
    "server error",
    "502",
    "503",
    "504",
    "429",
    "ssl",
    "remote disconnected",
    "incomplete read",
)
NON_RETRYABLE_HINTS = (
    "401",
    "403",
    "404",
    "gated",
    "not found",
    "no space",
    "not enough space",
    "disk full",
    "permission denied",
    "invalid username or password",
)


def disable_hf_xet() -> None:
    """Stop using Hugging Face's Xet downloader for the rest of this session.

    HF_HUB_DISABLE_XET is normally read at import time, so we also flip the
    live constant that huggingface_hub checks on each download.
    """
    import os

    os.environ["HF_HUB_DISABLE_XET"] = "1"
    try:
        import huggingface_hub.constants as hf_constants

        hf_constants.HF_HUB_DISABLE_XET = True
    except Exception:  # noqa: BLE001
        pass
    _TEST_HOOKS["xet_disabled"] = True


def _is_xet_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    name = type(exc).__name__.lower()
    blob = f"{name} {msg}"
    return any(h in blob for h in XET_ERROR_HINTS)


def _http_status(exc: BaseException):
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    response = getattr(exc, "response", None)
    if response is not None:
        status = getattr(response, "status_code", None)
        if isinstance(status, int):
            return status
    return None


def _is_retryable(exc: BaseException) -> bool:
    if _is_xet_error(exc):
        return True
    status = _http_status(exc)
    if status is not None:
        return status in {408, 409, 425, 429, 500, 502, 503, 504} or status >= 500
    msg = str(exc).lower()
    fatal = any(
        h in msg
        for h in (
            "gated",
            "permission denied",
            "no space",
            "disk full",
            "not enough space",
            "invalid username or password",
            "401",
            "403",
            "unauthorized",
            "forbidden",
        )
    ) or ("404" in msg and "not found" in msg)
    if fatal:
        return False
    if isinstance(exc, (ConnectionError, TimeoutError, InterruptedError)):
        return True
    # Broken pipes and resets are ConnectionError; leave other OS errors alone
    # (for example permission or disk-full) unless the message looks transient.
    return any(h in msg for h in RETRYABLE_HINTS)


def _download_log(progress: dict | None, message: str) -> None:
    if progress is None:
        print(message, file=sys.stderr)
        return
    log = progress.get("log")
    if isinstance(log, list):
        log.append(message)
    progress["message"] = message


def _maybe_inject_test_failure() -> None:
    remaining = int(_TEST_HOOKS.get("fail_times") or 0)
    if remaining <= 0:
        return
    _TEST_HOOKS["fail_times"] = remaining - 1
    err = _TEST_HOOKS.get("error") or "Connection hiccup (test)"
    if isinstance(err, BaseException):
        raise err
    raise ConnectionError(str(err))


def download_with_retries(fn, *, progress: dict | None = None, attempts: int = 4):
    """Run a download a few times, resuming when possible, and fall back off Xet."""
    last: BaseException | None = None
    xet_hits = 0
    switched = False
    total = max(1, int(attempts))
    for i in range(1, total + 1):
        try:
            _maybe_inject_test_failure()
            return fn()
        except Exception as exc:  # noqa: BLE001
            last = exc
            retryable = _is_retryable(exc) and i < total
            if _is_xet_error(exc):
                xet_hits += 1
                if xet_hits >= 2 and not switched:
                    disable_hf_xet()
                    switched = True
                    _download_log(
                        progress,
                        "Those errors look like a Hugging Face connection glitch. Switching to a simpler download method…",
                    )
            if not retryable:
                raise
            nxt = i + 1
            _download_log(progress, f"Connection hiccup, retrying ({nxt}/{total})…")
            delay = _TEST_HOOKS.get("sleep")
            if delay is None:
                delay = 2 ** i  # 2, 4, 8 seconds
            time.sleep(float(delay))
    assert last is not None
    raise last


def download_gguf(
    repo_id: str,
    filename: str,
    dest_dir: Path,
    progress: dict | None = None,
    token: str | None = None,
) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)

    def _do() -> Path:
        DownloadProgress.sink = progress
        try:
            path = hf_hub_download(
                repo_id=repo_id,
                filename=filename,
                local_dir=str(dest_dir),
                tqdm_class=DownloadProgress if tqdm is not None else None,
                user_agent=USER_AGENT,
                token=token or None,
            )
        finally:
            DownloadProgress.sink = None
        return Path(path)

    return download_with_retries(_do, progress=progress)


def download_repo(
    repo_id: str,
    dest_dir: Path,
    progress: dict | None = None,
    token: str | None = None,
    ignore_patterns: list[str] | None = None,
    nested: bool = True,
) -> Path:
    """Download a whole model repo (MLX, music, TTS, paste-a-link, and other formats)."""
    if nested:
        target = dest_dir / repo_id.replace("/", "__")
    else:
        target = dest_dir
    target.mkdir(parents=True, exist_ok=True)
    # Only skip .git. License files and README model cards stay with the weights.
    patterns = list(ignore_patterns) if ignore_patterns is not None else list(SNAPSHOT_IGNORE_DEFAULT)

    def _do() -> Path:
        DownloadProgress.sink = progress
        try:
            path = snapshot_download(
                repo_id=repo_id,
                local_dir=str(target),
                tqdm_class=DownloadProgress if tqdm is not None else None,
                user_agent=USER_AGENT,
                token=token or None,
                ignore_patterns=patterns,
            )
        finally:
            DownloadProgress.sink = None
        return Path(path)

    return download_with_retries(_do, progress=progress)


def download_mlx(
    repo_id: str,
    dest_dir: Path,
    progress: dict | None = None,
) -> Path:
    return download_repo(repo_id, dest_dir, progress=progress)




# Service / username used if the optional `keyring` package is installed.
_KEYRING_SERVICE = "ModelRepoFinder"
_KEYRING_HF = "hf_token"
_KEYRING_GH = "github_token"


def _log_secret_issue(message: str) -> None:
    print(message, file=sys.stderr)


def _keyring_get(username: str) -> str:
    try:
        import keyring  # optional; not a required dependency
    except Exception:
        return ""
    try:
        value = keyring.get_password(_KEYRING_SERVICE, username)
    except Exception as exc:  # noqa: BLE001
        _log_secret_issue(f"Could not read the token from the system keychain: {exc}")
        return ""
    return (value or "").strip()


def _keyring_set(username: str, token: str) -> bool:
    """Save or delete a token in the system keychain. Returns True on success."""
    try:
        import keyring
    except Exception:
        return False
    try:
        if token:
            keyring.set_password(_KEYRING_SERVICE, username, token)
        else:
            try:
                keyring.delete_password(_KEYRING_SERVICE, username)
            except Exception:
                # Nothing stored is fine.
                pass
        return True
    except Exception as exc:  # noqa: BLE001
        _log_secret_issue(f"Could not store the token in the system keychain: {exc}")
        return False


def harden_secret_file(path: Path) -> None:
    """Restrict a token file to the current user. Failures are logged, not fatal.

    On macOS and Linux this is chmod 0600. On Windows, icacls is used to
    drop inherited ACLs and grant only the current user full access.
    """
    import os
    import subprocess

    try:
        os.chmod(path, 0o600)
    except OSError as exc:
        _log_secret_issue(f"Could not set permissions on {path.name}: {exc}")
    if sys.platform != "win32":
        return
    user = (
        os.environ.get("USERNAME")
        or os.environ.get("USER")
        or ""
    )
    if not user:
        try:
            user = os.getlogin()
        except OSError:
            user = ""
    if not user:
        _log_secret_issue("Could not harden the token file ACL: no current user name.")
        return
    try:
        result = subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "icacls failed").strip()
            _log_secret_issue(f"Could not harden the token file ACL: {err[:300]}")
    except Exception as exc:  # noqa: BLE001
        _log_secret_issue(f"Could not harden the token file ACL: {exc}")


def _read_token_file(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        _log_secret_issue(f"Could not read {path.name}: {exc}")
        return ""


def _write_token_file(path: Path, token: str) -> None:
    token = (token or "").strip()
    if not token:
        if path.exists():
            try:
                path.unlink()
            except OSError as exc:
                _log_secret_issue(f"Could not remove {path.name}: {exc}")
        return
    path.write_text(token + "\n", encoding="utf-8")
    harden_secret_file(path)


def load_secret(filename: str, keyring_name: str, root: Path | None = None) -> str:
    """Load a token from the optional keychain, falling back to a local file."""
    value = _keyring_get(keyring_name)
    if value:
        return value
    return _read_token_file((root or ROOT) / filename)


def save_secret(filename: str, keyring_name: str, token: str, root: Path | None = None) -> None:
    """Prefer the system keychain when `keyring` is installed; otherwise a hardened file."""
    path = (root or ROOT) / filename
    token = (token or "").strip()
    if _keyring_set(keyring_name, token):
        # Migrated to the keychain — do not leave a plaintext copy behind.
        if path.exists():
            try:
                path.unlink()
            except OSError as exc:
                _log_secret_issue(f"Could not remove the old token file {path.name}: {exc}")
        return
    _write_token_file(path, token)


def load_hf_token(root: Path | None = None) -> str:
    return load_secret(".hf_token", _KEYRING_HF, root)


def save_hf_token(token: str, root: Path | None = None) -> None:
    save_secret(".hf_token", _KEYRING_HF, token, root)


def _hf_api(token: str | None = None) -> HfApi:
    return HfApi(headers={"User-Agent": USER_AGENT}, token=token or None)


HF_HOSTS = {"huggingface.co", "www.huggingface.co", "hf.co", "www.hf.co"}
_HF_SKIP_FIRST = {
    "login",
    "join",
    "settings",
    "docs",
    "blog",
    "organizations",
    "tasks",
    "pricing",
    "datasets",
    "spaces",
    "models",
}


def _valid_hf_name_part(part: str) -> bool:
    if not part or part in {".", ".."} or ".." in part:
        return False
    return bool(re.match(r"^[A-Za-z0-9._-]+$", part))


def parse_hf_input(text: str) -> str:
    """Turn a pasted Hugging Face URL or org/name into a repo id."""
    from urllib.parse import urlparse

    raw = (text or "").strip().strip("\"").strip("'").strip()
    if not raw:
        raise ValueError("Please paste a Hugging Face link or an org/name, like openai/whisper-tiny.")
    lower = raw.lower()
    looks_like_url = (
        "://" in raw
        or lower.startswith("huggingface.co")
        or lower.startswith("www.huggingface.co")
        or lower.startswith("hf.co")
        or lower.startswith("www.hf.co")
    )
    if looks_like_url:
        if "://" not in raw:
            raw = "https://" + raw
        parsed = urlparse(raw)
        host = (parsed.netloc or "").lower().split(":")[0]
        if host not in HF_HOSTS:
            raise ValueError(
                "That does not look like a Hugging Face link. Paste a huggingface.co URL, or org/name."
            )
        parts = [p for p in (parsed.path or "").split("/") if p]
        if parts and parts[0] == "models":
            parts = parts[1:]
        if parts and parts[0] in {"datasets", "spaces", "dataset", "space"}:
            kind = "dataset" if "dataset" in parts[0] else "space"
            raise ValueError(
                f"That looks like a Hugging Face {kind} page. This box is for model pages, like https://huggingface.co/org/name."
            )
        if len(parts) < 2:
            raise ValueError("That link is missing the model name. It should look like huggingface.co/org/name.")
        org, name = parts[0], parts[1]
        if org in _HF_SKIP_FIRST:
            raise ValueError("That does not look like a model page. Paste a link like https://huggingface.co/org/name.")
        if not _valid_hf_name_part(org) or not _valid_hf_name_part(name):
            raise ValueError("That does not look like a model name. Paste a link or org/name.")
        return f"{org}/{name}"

    raw = raw.strip("/")
    if raw.lower().startswith("models/"):
        raw = raw[7:]
    parts = [p for p in raw.split("/") if p]
    if len(parts) < 2:
        raise ValueError("Please paste a Hugging Face link or an org/name, like openai/whisper-tiny.")
    org, name = parts[0], parts[1]
    if not _valid_hf_name_part(org) or not _valid_hf_name_part(name):
        raise ValueError("That does not look like a model name. Paste a link or org/name.")
    return f"{org}/{name}"


def _license_from_info(info: Any) -> str:
    lic = getattr(info, "license", None)
    if lic and str(lic) not in {"None", "unknown", "UNKNOWN"}:
        return str(lic)
    for tag in getattr(info, "tags", None) or []:
        s = str(tag)
        if s.lower().startswith("license:"):
            return s.split(":", 1)[1]
    card = getattr(info, "card_data", None)
    if card is not None:
        val = getattr(card, "license", None)
        if val is None and hasattr(card, "get"):
            try:
                val = card.get("license")
            except Exception:  # noqa: BLE001
                val = None
        if val:
            return str(val)
    return "Unknown"


def _is_gated(info: Any) -> bool:
    val = getattr(info, "gated", False)
    if val in {None, False, "false", "False", ""}:
        return False
    return True


def _format_of_path(path: str) -> str:
    suffix = Path(path).suffix.lower()
    name = path.rsplit("/", 1)[-1].lower()
    joined = path.lower()
    if suffix == ".gguf":
        return "gguf"
    if suffix == ".safetensors":
        return "safetensors"
    if suffix in {".onnx"}:
        return "onnx"
    if suffix in {".mlmodel", ".mlpackage"} or "coreml" in joined:
        return "coreml"
    if suffix in {".bin", ".pt", ".pth", ".ckpt"}:
        return "pytorch"
    if suffix in {".mlx", ".npz"} or "mlx" in joined:
        return "mlx"
    if suffix == ".tflite":
        return "tflite"
    if name in {"config.json", "tokenizer.json", "tokenizer_config.json", "special_tokens_map.json"}:
        return "config"
    if suffix in {".json", ".txt", ".md", ".py"} or name in {".gitattributes"}:
        return "other"
    return "other"


def default_gguf_pick(files: list[dict], max_gb: float | None) -> dict | None:
    """Use the usual 70%-of-cap logic; if nothing fits, still suggest a default."""
    gguf = [f for f in files if (f.get("format") or "").lower() == "gguf" or str(f.get("path") or "").lower().endswith(".gguf")]
    if not gguf:
        return None
    cap = float(max_gb) if max_gb and max_gb > 0 else 10**9
    pick = pick_fit(gguf, cap)
    if pick:
        return pick
    # Nothing fits the cap — still pick near 70% of cap (or the smallest).
    primary = [f for f in gguf if not is_sidecar(f["path"])]
    pool = primary or gguf
    preferred = [f for f in pool if PREFERRED_QUANTS.search((f.get("path") or "").upper())]
    use = preferred or pool
    target = cap * 0.7 if cap < 10**8 else min(use, key=lambda f: f.get("size_gb") or 0)["size_gb"]
    return min(use, key=lambda f: abs((f.get("size_gb") or 0) - target))


def _size_label(gb: float) -> str:
    if gb >= 1:
        return f"{gb:.1f} GB"
    if gb >= 0.001:
        return f"{gb * 1024:.0f} MB"
    return f"{max(gb * 1024 * 1024, 1):.0f} KB"


def inspect_hf_repo(repo_id: str, token: str | None = None, max_gb: float | None = None) -> dict:
    """Preview a pasted model: files, size, format, license, gated status, and a default pick."""
    repo_id = parse_hf_input(repo_id) if "/" in (repo_id or "") or "http" in (repo_id or "") else repo_id
    api = _hf_api(token)
    info = None
    gated_blocked = False
    try:
        info = api.model_info(repo_id)
    except HfHubHTTPError as exc:
        code = getattr(getattr(exc, "response", None), "status_code", None)
        if code == 404:
            raise ValueError(
                f"Hugging Face could not find a model called {repo_id}. Check the link and try again."
            ) from exc
        if code in {401, 403}:
            gated_blocked = True
        else:
            raise ValueError(f"Could not read that model page ({exc}).") from exc
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Could not reach Hugging Face. Check your internet and try again. ({exc})") from exc

    gated = _is_gated(info) if info is not None else True
    license_name = _license_from_info(info) if info is not None else "Unknown"
    tags = list(getattr(info, "tags", None) or []) if info is not None else []
    pipeline = getattr(info, "pipeline_tag", None) or "" if info is not None else ""
    card_url = f"https://huggingface.co/{repo_id}"
    model_brief = {
        "id": repo_id,
        "tags": tags,
        "pipeline_tag": pipeline,
        "card_url": card_url,
    }

    files: list[dict] = []
    tree_error = None
    try:
        tree = list(api.list_repo_tree(repo_id, recursive=True))
        for item in tree:
            path = getattr(item, "path", None) or ""
            size = getattr(item, "size", None)
            # Skip folders (no size).
            if not path or not isinstance(size, (int, float)):
                continue
            files.append(
                {
                    "path": path,
                    "size": int(size),
                    "size_gb": size / (1024**3),
                    "format": _format_of_path(path),
                    "sidecar": is_sidecar(path),
                }
            )
        files.sort(key=lambda f: (0 if f["format"] in {"gguf", "safetensors", "mlx", "onnx", "pytorch"} else 1, f["path"].lower()))
    except HfHubHTTPError as exc:
        code = getattr(getattr(exc, "response", None), "status_code", None)
        if code in {401, 403} or gated:
            gated_blocked = True
            tree_error = "gated"
        elif code == 404:
            raise ValueError(
                f"Hugging Face could not find a model called {repo_id}. Check the link and try again."
            ) from exc
        else:
            raise ValueError(f"Could not list the files in that model ({exc}).") from exc

    if gated_blocked and not files:
        return {
            "id": repo_id,
            "card_url": card_url,
            "license": license_name,
            "gated": True,
            "gated_blocked": True,
            "pipeline_tag": pipeline,
            "tags": tags,
            "files": [],
            "file_count": 0,
            "total_size": 0,
            "total_size_gb": 0,
            "weight_size": 0,
            "weight_size_gb": 0,
            "formats": [],
            "format": "unknown",
            "app_note": "",
            "has_gguf": False,
            "gguf_files": [],
            "default_pick": None,
            "has_safetensors": False,
            "has_bin": False,
            "suggest_skip_bin": False,
            "over_cap": False,
            "max_model_gb": max_gb,
            "message": (
                f"{repo_id} is gated, which means you need a Hugging Face account, "
                "to accept the license on the model page in your browser, and to paste "
                "an access token below before this app can see or download the files."
            ),
        }

    total = sum(f["size"] for f in files)
    weight_files = [f for f in files if f["format"] in {"gguf", "safetensors", "pytorch", "onnx", "mlx", "coreml", "tflite"}]
    weight_size = sum(f["size"] for f in weight_files)
    formats = []
    for f in weight_files:
        if f["format"] not in formats:
            formats.append(f["format"])
    # If a Mac MLX repo has no typical suffixes, still label mlx from the id.
    if repo_id.lower().startswith("mlx-community/") and "mlx" not in formats:
        formats.append("mlx")
    fmt_guess = formats[0] if formats else _guess_format_from_files([f["path"] for f in files])
    pick_stub = {"format": fmt_guess, "path": "(whole model)", "size": weight_size or total, "size_gb": (weight_size or total) / (1024**3)}
    fmt_name, app_note = guess_format_and_note(model_brief, pick_stub)

    # Quality-setting options are ordinary GGUFs; companions are offered separately.
    all_gguf = [f for f in files if f["format"] == "gguf"]
    gguf_files = [f for f in all_gguf if not f.get("sidecar")]
    for f in gguf_files:
        f["companions"] = find_companions(files, f)
    default = default_gguf_pick(gguf_files, max_gb) if gguf_files else None
    if default:
        default = dict(default)
        default["companions"] = find_companions(files, default)
    companions = (default.get("companions") if default else None) or find_companions(files, pick_stub)
    has_sft = any(f["format"] == "safetensors" for f in files)
    has_bin = any(f["path"].lower().endswith(".bin") and f["format"] == "pytorch" for f in files)
    stored_gb = (default["size_gb"] if default else ((weight_size or total) / (1024**3)))
    whole_gb = (weight_size or total) / (1024**3) if (weight_size or total) else 0
    pick_fits = bool(default and max_gb and default["size_gb"] <= float(max_gb))
    fmt_for_est = (default.get("format") if default else fmt_name) or "gguf"
    est = estimate_runtime_memory_gb(stored_gb, fmt_for_est)
    fit = describe_fit(
        stored_gb,
        max_gb,
        est,
        whole_gb=whole_gb,
        pick_fits=pick_fits,
        fmt=fmt_for_est,
    )

    return {
        "id": repo_id,
        "card_url": card_url,
        "license": license_name,
        "gated": gated,
        "gated_blocked": False,
        "pipeline_tag": pipeline,
        "tags": tags,
        "files": files,
        "file_count": len(files),
        "total_size": total,
        "total_size_gb": total / (1024**3),
        "weight_size": weight_size,
        "weight_size_gb": weight_size / (1024**3) if weight_size else 0,
        "formats": formats or ([fmt_name] if fmt_name else []),
        "format": fmt_name,
        "app_note": app_note,
        "has_gguf": bool(gguf_files),
        "gguf_files": gguf_files,
        "default_pick": default,
        "companions": companions,
        "is_multimodal": bool(companions),
        "has_safetensors": has_sft,
        "has_bin": has_bin,
        "suggest_skip_bin": bool(has_sft and has_bin),
        "over_cap": fit["over_cap"],
        "whole_over_cap": fit["whole_over_cap"],
        "max_model_gb": max_gb,
        "stored_size_gb": fit["stored_size_gb"],
        "estimated_ram_gb": fit["estimated_ram_gb"],
        "estimate_label": fit["estimate_label"],
        "warning": fit["warning"],
        "message": (
            f"{repo_id} is gated. Before downloading, open the model page in your browser, "
            "accept the license, and paste a Hugging Face access token below."
            if gated else ""
        ),
    }


def manual_dest(cfg: dict, machine: str, repo_id: str, root: Path | None = None) -> Path:
    return incoming_dir(cfg, machine, "manual", root) / repo_id.replace("/", "__")


def add_to_allow_list(cfg: dict, repo_id: str, key: str = "allow_list") -> bool:
    allow = list(cfg.get(key) or [])
    if repo_id in allow:
        return False
    allow.append(repo_id)
    cfg[key] = allow
    return True


def _safe_relative(relative_path: str) -> Path:
    if not relative_path or Path(relative_path).is_absolute() or ".." in Path(relative_path).parts:
        raise ValueError("Invalid path.")
    return Path(relative_path)


def retire_item(cfg: dict, machine: str, tag: str, relative_path: str, root: Path | None = None) -> Path:
    """Move a downloaded item to retired/. Never deletes."""
    rel = _safe_relative(relative_path)
    src = (incoming_dir(cfg, machine, tag, root) / rel).resolve()
    incoming_root = incoming_dir(cfg, machine, tag, root).resolve()
    if incoming_root not in src.parents and src != incoming_root:
        raise ValueError("That file is not inside the incoming folder.")
    if not src.exists():
        raise FileNotFoundError(f"Nothing to retire at {src}")
    dest = retired_dir(cfg, machine, tag, root) / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = dest.with_name(dest.name + f".retired-{stamp}")
    shutil.move(str(src), str(dest))
    return dest


def retire_github_item(cfg: dict, category: str, relative_path: str, root: Path | None = None) -> Path:
    rel = _safe_relative(relative_path)
    src = (github_incoming_dir(cfg, category, root) / rel).resolve()
    incoming_root = github_incoming_dir(cfg, category, root).resolve()
    if incoming_root not in src.parents and src != incoming_root:
        raise ValueError("That file is not inside the incoming folder.")
    if not src.exists():
        raise FileNotFoundError(f"Nothing to retire at {src}")
    dest = github_retired_dir(cfg, category, root) / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = dest.with_name(dest.name + f".retired-{stamp}")
    shutil.move(str(src), str(dest))
    return dest


def list_installed(cfg: dict, machine: str | None = None, root: Path | None = None) -> list[dict]:
    paths = cfg.get("paths") or {}
    incoming_name = paths.get("incoming") or "incoming"
    incoming = base_dir(cfg, root) / incoming_name
    if not incoming.exists():
        return []
    machines = [machine] if machine else [p.name for p in incoming.iterdir() if p.is_dir()]
    rows = []
    for mkey in machines:
        if mkey == "github":
            continue
        mdir = incoming / mkey
        if not mdir.is_dir():
            continue
        for tag_dir in sorted(mdir.iterdir()):
            if not tag_dir.is_dir() or tag_dir.name.startswith("."):
                continue
            for item in sorted(tag_dir.iterdir()):
                if item.name in {".cache", ".huggingface"} or item.name.startswith("."):
                    continue
                size = _folder_or_file_size(item)
                rows.append(
                    {
                        "machine": mkey,
                        "tag": tag_dir.name,
                        "name": item.name,
                        "relative_path": item.name,
                        "path": str(item),
                        "size_gb": size / (1024**3),
                        "is_dir": item.is_dir(),
                    }
                )
    return rows


def list_github_installed(cfg: dict, root: Path | None = None) -> list[dict]:
    paths = cfg.get("paths") or {}
    incoming_name = paths.get("incoming") or "incoming"
    incoming = base_dir(cfg, root) / incoming_name / "github"
    if not incoming.exists():
        return []
    rows = []
    for cat_dir in sorted(incoming.iterdir()):
        if not cat_dir.is_dir() or cat_dir.name.startswith("."):
            continue
        for item in sorted(cat_dir.iterdir()):
            if item.name.startswith("."):
                continue
            size = _folder_or_file_size(item)
            rows.append(
                {
                    "category": cat_dir.name,
                    "name": item.name,
                    "relative_path": item.name,
                    "path": str(item),
                    "size_gb": size / (1024**3),
                    "is_dir": item.is_dir(),
                    "is_git": (item / ".git").exists() if item.is_dir() else False,
                }
            )
    return rows


def _folder_or_file_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    total = 0
    for p in path.rglob("*"):
        if p.is_file():
            if ".cache" in p.parts or ".huggingface" in p.parts or ".git" in p.parts:
                continue
            total += p.stat().st_size
    return total


def write_report(data: dict, report_path: Path, dry_run: bool = True) -> None:
    lines = []
    lines.append(f"# HF watcher report ({'dry-run' if dry_run else 'live'})")
    lines.append(
        f"Generated: {data.get('generated_at') or datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    )
    lines.append(f"Machine: {data.get('machine_name') or data.get('machine')}")
    lines.append("")
    for tag in data.get("tags") or []:
        title = tag.get("title") or tag["name"]
        lines.append(f"## Tag: {title} (keep {tag['keep']})")
        if tag.get("error"):
            lines.append(f"- Search failed: {tag['error']}")
            lines.append("")
            continue
        models = tag.get("models") or []
        if not models:
            lines.append("- No fits found.")
        for i, row in enumerate(models, 1):
            fmt = (row.get("format") or "gguf").upper()
            lines.append(
                f"- {i}. `{row['id']}` — downloads={row.get('downloads')}; "
                f"{data.get('machine')}:{row.get('file')} ({row.get('size_gb', 0):.1f} GB, {fmt})"
            )
            if row.get("app_note"):
                lines.append(f"  {row['app_note']}")
        lines.append("")
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_cli(dry_run: bool, config_path: Path, machines_filter: list[str] | None) -> int:
    cfg = load_config(config_path)
    keys = list(enabled_machines(cfg))
    if machines_filter:
        keys = [k for k in keys if k in machines_filter]
        if not keys:
            print("No matching machines.", file=sys.stderr)
            return 1
    combined_tags: dict[str, dict] = {}
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    for key in keys:
        print(f"Looking up suggestions for {key}…", file=sys.stderr)
        data = collect_suggestions(cfg, key)
        for tag in data["tags"]:
            slot = combined_tags.setdefault(
                tag["name"],
                {
                    "name": tag["name"],
                    "title": tag.get("title") or tag["name"],
                    "keep": tag["keep"],
                    "models": [],
                    "error": tag.get("error"),
                },
            )
            if tag.get("error") and not slot.get("error"):
                slot["error"] = tag["error"]
            for row in tag.get("models") or []:
                row = dict(row)
                row["_machine"] = key
                slot["models"].append(row)

    lines = [
        f"# HF watcher report ({'dry-run' if dry_run else 'live'})",
        f"Generated: {generated}",
        "",
    ]
    for tag_name, tag in combined_tags.items():
        title = tag.get("title") or tag_name
        lines.append(f"## Tag: {title} (keep {tag['keep']})")
        if tag.get("error"):
            lines.append(f"- Search failed: {tag['error']}")
            lines.append("")
            continue
        by_id: dict[str, dict] = {}
        for row in tag["models"]:
            rec = by_id.setdefault(row["id"], {**row, "fits": []})
            rec["fits"].append(f"{row['_machine']}:{row.get('file')} ({row.get('size_gb', 0):.1f} GB)")
        ranked = sorted(by_id.values(), key=lambda x: (-(x.get("downloads") or 0), -(x.get("likes") or 0)))
        if not ranked:
            lines.append("- No fits found.")
        for i, row in enumerate(ranked, 1):
            lines.append(
                f"- {i}. `{row['id']}` — downloads={row.get('downloads')}; " + "; ".join(row["fits"])
            )
        lines.append("")

    report_name = (cfg.get("paths") or {}).get("report") or "report.md"
    report_path = config_path.parent / report_name
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(report_path.read_text(encoding="utf-8"))
    print(f"Wrote {report_path}", file=sys.stderr)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="ModelRepoFinder (command-line)")
    p.add_argument("--config", type=Path, default=ROOT / "machines.yaml")
    p.add_argument("--dry-run", action="store_true", default=True, help="List only (default)")
    p.add_argument("--live", action="store_true", help="Unused here; use the app to download")
    p.add_argument("--machines", nargs="+", help="Limit to these machine keys")
    args = p.parse_args()
    return run_cli(not args.live, args.config, args.machines)


if __name__ == "__main__":
    raise SystemExit(main())
