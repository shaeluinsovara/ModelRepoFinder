#!/usr/bin/env python3
"""ModelRepoFinder — search, pick, and download Hugging Face models.

Keeps the original rules:
- GGUF search sorted by downloads
- Skip mmproj / projector / clip sidecars
- Pick a quant near 70% of the machine cap, preferring Q4_K_M / Q5_K_M / Q4_K_S / Q5_K_S / Q6_K
- Tags with keep counts
- Allow-list gate for downloads
- Retired models are moved, never deleted
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
VERSION = "1.3"
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
    paths = cfg.get("paths") or {}
    incoming = paths.get("incoming") or "incoming"
    return base_dir(cfg, root) / incoming / machine / tag


def retired_dir(cfg: dict, machine: str, tag: str, root: Path | None = None) -> Path:
    paths = cfg.get("paths") or {}
    retired = paths.get("retired") or "retired"
    return base_dir(cfg, root) / retired / machine / tag


def github_incoming_dir(cfg: dict, category: str, root: Path | None = None) -> Path:
    paths = cfg.get("paths") or {}
    incoming = paths.get("incoming") or "incoming"
    return base_dir(cfg, root) / incoming / "github" / category


def github_retired_dir(cfg: dict, category: str, root: Path | None = None) -> Path:
    paths = cfg.get("paths") or {}
    retired = paths.get("retired") or "retired"
    return base_dir(cfg, root) / retired / "github" / category


def is_sidecar(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return bool(SKIP_NAME_RE.search(name))


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


def search_models(
    query: str,
    limit: int = 40,
    pipeline_tag: str | None = None,
    gguf_only: bool = False,
    author: str | None = None,
) -> list[dict]:
    def _call():
        kwargs: dict[str, Any] = {
            "search": query,
            "limit": limit,
            "sort": "downloads",
        }
        if gguf_only:
            kwargs["filter"] = "gguf"
        if pipeline_tag:
            kwargs["pipeline_tag"] = pipeline_tag
        if author:
            kwargs["author"] = author
        return list(HF.list_models(**kwargs))

    models = _retry(_call)
    return [_model_brief(m) for m in models]


def search_gguf_models(
    query: str, limit: int = 40, pipeline_tag: str | None = None
) -> list[dict]:
    return search_models(query, limit=limit, pipeline_tag=pipeline_tag, gguf_only=True)


def search_mlx_models(query: str, limit: int = 40) -> list[dict]:
    return search_models(query, limit=limit, author="mlx-community")


def _model_brief(m: Any) -> dict:
    mid = getattr(m, "id", None) or ""
    tags = list(getattr(m, "tags", None) or [])
    return {
        "id": mid,
        "downloads": getattr(m, "downloads", None) or 0,
        "likes": getattr(m, "likes", None) or 0,
        "lastModified": str(getattr(m, "last_modified", None) or ""),
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
    search_limit: int = 40,
    categories: list[str] | None = None,
) -> dict:
    """Build a shortlist for one machine, only for the chosen categories."""
    machines = enabled_machines(cfg)
    if machine_key not in machines:
        raise ValueError(f"Unknown or disabled machine: {machine_key}")
    machine = machines[machine_key]
    formats = [f.lower() for f in (machine.get("formats") or ["gguf"])]
    cap = float(machine.get("max_model_gb") or 0)
    tags = all_hf_tags(cfg)
    selected = categories if categories is not None else list(machine.get("hf_categories") or tags.keys())
    selected = [k for k in selected if k in tags]
    allow = set(cfg.get("allow_list") or [])

    def note(msg: str) -> None:
        if progress_cb:
            progress_cb(msg)

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
            )
        )

    return {
        "machine": machine_key,
        "machine_name": machine.get("name") or machine_key,
        "max_model_gb": cap,
        "formats": formats,
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
) -> dict:
    display = tag_cfg.get("name") or tag_name
    keep = int(tag_cfg.get("keep") or 1)
    cap_this = cap
    if tag_cfg.get("cap_gb"):
        cap_this = min(cap_this, float(tag_cfg["cap_gb"]))
    pipeline_tag = tag_cfg.get("pipeline_tag")
    use_pt = bool(tag_cfg.get("use_pipeline_tag"))
    exclude_ids = list(tag_cfg.get("exclude_id_substrings") or [])
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
                ):
                    merged_any.setdefault(m["id"], m)
            groups.append(("any", list(merged_any.values())))
        if "mlx" in formats:
            merged_mlx: dict[str, dict] = {}
            for q in queries:
                note(f"Searching mlx-community for {display} (“{q}”)…")
                for m in search_mlx_models(q, limit=search_limit):
                    merged_mlx.setdefault(m["id"], m)
            groups.append(("mlx", list(merged_mlx.values())))
    except Exception as exc:  # noqa: BLE001
        tag_out["error"] = f"Search failed: {exc}"
        return tag_out

    for fmt, models in groups:
        ranked = _rank_models(
            models=models,
            fmt=fmt,
            cap=cap_this,
            keep=keep,
            exclude_ids=exclude_ids,
            allow=allow,
            tag_name=tag_name,
            keywords=keywords if fuzzy else [],
            fuzzy=fuzzy,
            progress_cb=progress_cb,
            display=display,
        )
        # Avoid listing the same repo twice if it appeared as GGUF and as "any".
        have = {m["id"] for m in tag_out["models"]}
        for row in ranked:
            if row["id"] not in have:
                tag_out["models"].append(row)
                have.add(row["id"])
    # After merging formats, keep the most downloaded `keep` (and keep a little
    # extra room if GGUF and another format both fit — up to keep per format
    # already applied, so trim to keep * number of groups, max keep*2).
    tag_out["models"].sort(key=lambda x: (-(x.get("downloads") or 0), -(x.get("likes") or 0)))
    max_keep = keep * max(1, len(groups))
    tag_out["models"] = tag_out["models"][:max_keep]
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
) -> list[dict]:
    candidates = []
    for m in models:
        mid = m.get("id") or ""
        if not mid or id_blocked(mid, exclude_ids):
            continue
        if keywords and not matches_keywords(m, keywords):
            # Leave a few for a card-text look later; drop obvious misses.
            if not fuzzy:
                continue
            m = dict(m)
            m["_needs_card"] = True
        candidates.append(m)

    # Prefer obvious keyword hits, then popularity.
    candidates.sort(
        key=lambda x: (
            0 if x.get("_needs_card") else 1,
            x.get("downloads") or 0,
            x.get("likes") or 0,
        ),
        reverse=True,
    )
    # Cap how many repos we inspect so a category with several queries stays snappy.
    inspect_cap = 40
    needs_card = [m for m in candidates if m.get("_needs_card")]
    definite = [m for m in candidates if not m.get("_needs_card")]
    # Give unmatched ones a second chance via the model card, but only a handful.
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
        except Exception as exc:  # noqa: BLE001
            return None if "gated" in str(exc).lower() else None
        if not pick:
            return None
        fmt_name, app_note = guess_format_and_note(m, pick)
        return {
            "id": mid,
            "downloads": m.get("downloads") or 0,
            "likes": m.get("likes") or 0,
            "lastModified": m.get("lastModified") or "",
            "card_url": m.get("card_url") or f"https://huggingface.co/{mid}",
            "file": (
                pick["path"].rsplit("/", 1)[-1]
                if pick["path"] != "(whole model)"
                else "(whole model)"
            ),
            "file_path": pick["path"],
            "size": pick.get("size") or 0,
            "size_gb": pick["size_gb"],
            "format": fmt_name,
            "app_note": app_note,
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

    picks.sort(key=lambda x: (-(x.get("downloads") or 0), -(x.get("likes") or 0)))
    return picks[:keep]


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
    patterns = list(ignore_patterns) if ignore_patterns is not None else [".git*", "LICENSE*", "license*"]

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



def load_hf_token(root: Path | None = None) -> str:
    path = (root or ROOT) / ".hf_token"
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def save_hf_token(token: str, root: Path | None = None) -> None:
    import os

    path = (root or ROOT) / ".hf_token"
    token = (token or "").strip()
    if not token:
        if path.exists():
            try:
                path.unlink()
            except OSError:
                pass
        return
    path.write_text(token + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


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
        if not re.match(r"^[A-Za-z0-9._-]+$", org) or not re.match(r"^[A-Za-z0-9._-]+$", name):
            raise ValueError("That does not look like a model name. Paste a link or org/name.")
        return f"{org}/{name}"

    raw = raw.strip("/")
    if raw.lower().startswith("models/"):
        raw = raw[7:]
    parts = [p for p in raw.split("/") if p]
    if len(parts) < 2:
        raise ValueError("Please paste a Hugging Face link or an org/name, like openai/whisper-tiny.")
    org, name = parts[0], parts[1]
    if not re.match(r"^[A-Za-z0-9._-]+$", org) or not re.match(r"^[A-Za-z0-9._-]+$", name):
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

    gguf_files = [f for f in files if f["format"] == "gguf" and not f.get("sidecar")]
    default = default_gguf_pick(gguf_files, max_gb) if gguf_files else None
    has_sft = any(f["format"] == "safetensors" for f in files)
    has_bin = any(f["path"].lower().endswith(".bin") and f["format"] == "pytorch" for f in files)
    compare_gb = (default["size_gb"] if default else ((weight_size or total) / (1024**3)))
    over_cap = bool(max_gb and compare_gb > float(max_gb))
    whole_over = bool(max_gb and ((weight_size or total) / (1024**3)) > float(max_gb))

    warning = ""
    if over_cap or whole_over:
        shown = compare_gb if default else ((weight_size or total) / (1024**3))
        warning = (
            f"This is about {_size_label(shown)}, which is larger than this computer's "
            f"{_size_label(float(max_gb))} cap. You can still download it if you have the "
            "disk space, but it may not run well here."
        )
        if default and whole_over and not (default["size_gb"] > float(max_gb or 0)):
            warning = (
                f"The whole repo is about {_size_label((weight_size or total) / (1024**3))}, "
                f"which is over this computer's {_size_label(float(max_gb))} cap. The suggested "
                f"GGUF file is {_size_label(default['size_gb'])} and should fit. You can still "
                "download either one."
            )
            over_cap = False  # the default pick fits; still mention whole-repo size in warning? 
            # Keep over_cap True only if the chosen default is over. The warning can still show.
            warning = warning  # default fits

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
        "has_safetensors": has_sft,
        "has_bin": has_bin,
        "suggest_skip_bin": bool(has_sft and has_bin),
        "over_cap": bool(max_gb and ((default["size_gb"] if default else (weight_size or total) / (1024**3)) > float(max_gb))),
        "whole_over_cap": whole_over,
        "max_model_gb": max_gb,
        "warning": warning,
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
