#!/usr/bin/env python3
"""Read-only scan for AI models already on this computer.

Looks in well-known locations (the Hugging Face cache, Ollama, LM Studio, and
this app's own save folders) plus any folders the person has added. The walk
is bounded by depth, file count, and a time limit, and never follows a path
outside the folder it was asked to scan. Nothing is deleted or moved.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Iterable

import watcher as W

# Conservative limits so a scan cannot wander through a whole disk.
DEFAULT_MAX_DEPTH = 6
DEFAULT_MAX_FILES = 4000
DEFAULT_MAX_SECONDS = 15.0
DEFAULT_MAX_DIR_ENTRIES = 1500
MAX_CUSTOM_FOLDERS = 10
MAX_PATH_LENGTH = 1024

SKIP_DIR_NAMES = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".tox",
    ".idea",
    ".vscode",
    "System Volume Information",
    "$RECYCLE.BIN",
    "Recycled",
    "Recovery",
    "proc",
    "sys",
    "dev",
    "lost+found",
}

# Folder names we will not accept as a custom scan root.
REJECT_ROOT_NAMES = {
    "",
    "/",
    "\\",
    "c:\\",
    "c:/",
    "/etc",
    "/sys",
    "/proc",
    "/dev",
    "/root",
    "/boot",
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/lib64",
    # macOS shortcuts: /etc, /tmp, and /var are symlinks into /private.
    # Compare both the path as typed and the realpath result.
    "/private/etc",
    "/private/var",
    "/private/tmp",
    "/system",
    "/library",
    "/applications",
    "/windows",
    "c:\\windows",
    "c:\\windows\\system32",
    "c:\\program files",
    "c:\\program files (x86)",
}

WEIGHT_SUFFIXES = {".gguf", ".safetensors", ".mlx", ".npz"}
MLX_HINTS = ("config.json", "model.safetensors", "weights.npz", "mlx_model.safetensors")


class ScanBudget:
    """Shared limits for one scan pass."""

    def __init__(
        self,
        *,
        max_depth: int = DEFAULT_MAX_DEPTH,
        max_files: int = DEFAULT_MAX_FILES,
        max_seconds: float = DEFAULT_MAX_SECONDS,
        max_dir_entries: int = DEFAULT_MAX_DIR_ENTRIES,
    ):
        self.max_depth = max(1, int(max_depth))
        self.max_files = max(1, int(max_files))
        self.max_seconds = float(max_seconds)
        self.max_dir_entries = max(10, int(max_dir_entries))
        self.deadline = time.monotonic() + self.max_seconds
        self.files_seen = 0
        self.truncated = False
        self.reasons: list[str] = []

    def expired(self) -> bool:
        if time.monotonic() >= self.deadline:
            self._cut("time")
            return True
        return False

    def take_file(self) -> bool:
        if self.files_seen >= self.max_files:
            self._cut("file count")
            return False
        self.files_seen += 1
        return True

    def _cut(self, reason: str) -> None:
        if reason not in self.reasons:
            self.reasons.append(reason)
        self.truncated = True


def _unique_existing(paths: Iterable[Path]) -> list[Path]:
    out: list[Path] = []
    seen: set[str] = set()
    for raw in paths:
        try:
            path = Path(raw).expanduser()
            if not path.exists() or not path.is_dir():
                continue
            resolved = path.resolve()
        except (OSError, RuntimeError, ValueError):
            continue
        key = str(resolved).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(resolved)
    return out


def default_hf_cache_roots() -> list[Path]:
    candidates: list[Path] = []
    hub_cache = os.environ.get("HF_HUB_CACHE")
    hf_home = os.environ.get("HF_HOME")
    transformers = os.environ.get("TRANSFORMERS_CACHE")
    if hub_cache:
        candidates.append(Path(hub_cache).expanduser())
    if hf_home:
        candidates.append(Path(hf_home).expanduser() / "hub")
    if transformers:
        candidates.append(Path(transformers).expanduser())
    candidates.append(Path.home() / ".cache" / "huggingface" / "hub")
    # Windows installers sometimes use this instead.
    candidates.append(Path.home() / ".cache" / "huggingface")
    return _unique_existing(candidates)


def default_ollama_roots() -> list[Path]:
    candidates: list[Path] = []
    env = os.environ.get("OLLAMA_MODELS")
    if env:
        candidates.append(Path(env).expanduser())
    home = Path.home()
    candidates.append(home / ".ollama" / "models")
    candidates.append(home / ".ollama")
    # Windows default is %USERPROFILE%\.ollama, already covered by Path.home().
    return _unique_existing(candidates)


def default_lmstudio_roots() -> list[Path]:
    home = Path.home()
    candidates = [
        home / ".lmstudio" / "models",
        home / ".cache" / "lm-studio" / "models",
        home / ".cache" / "lm-studio",
        home / ".lmstudio",
    ]
    return _unique_existing(candidates)


def contained(path: Path, root: Path) -> bool:
    """True if `path` resolves to a location inside `root` (or is `root`)."""
    try:
        resolved = path.resolve()
        root_res = root.resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    return resolved == root_res or root_res in resolved.parents


def _matches_system_root_name(value: str) -> bool:
    """True when this exact path string is a listed system root.

    The candidate is compared both as typed (after slash cleanup) and in the
    forms stored in REJECT_ROOT_NAMES. Callers also pass os.path.realpath so
    a macOS shortcut such as /etc -> /private/etc is caught.
    """
    text = str(value).replace("\\", "/").rstrip("/").lower()
    raw = str(value).replace("\\", "/").lower()
    slash_forms = {item if str(item).endswith("/") else str(item) + "/" for item in REJECT_ROOT_NAMES}
    if raw in REJECT_ROOT_NAMES or (text + "/") in slash_forms:
        return True
    if text in {str(item).rstrip("/") for item in REJECT_ROOT_NAMES}:
        return True
    return False


def _is_filesystem_root(path: Path) -> bool:
    # A path that is only a filesystem root (/, C:\) is too broad.
    parts = path.parts
    if len(parts) <= 1:
        return True
    # Windows drive root: ('C:\\',) or ('C:', '\\') depending on Python.
    if len(parts) == 1 or (len(parts) == 2 and parts[1] in {"\\", "/", ""}):
        return True
    return False


def _is_rejected_root(path: Path) -> bool:
    """True if the typed path or its real path is a system root.

    Both are checked. On macOS, /etc is a symlink to /private/etc, so looking
    only at the resolved path used to miss the /etc denylist entry.
    """
    try:
        resolved_text = os.path.realpath(path)
    except (OSError, RuntimeError, ValueError):
        return True
    if _matches_system_root_name(os.fspath(path)) or _matches_system_root_name(resolved_text):
        return True
    return _is_filesystem_root(Path(resolved_text))


def validate_scan_folder(path_str: str) -> Path:
    """Check a user-supplied folder before storing or walking it."""
    raw = (path_str or "").strip()
    if not raw:
        raise ValueError("Please paste a folder path first.")
    if "\x00" in raw or any(ord(c) < 32 for c in raw if c not in "\t"):
        raise ValueError("That folder path is not allowed.")
    if len(raw) > MAX_PATH_LENGTH:
        raise ValueError("That folder path is too long.")
    path = Path(raw).expanduser()
    try:
        # realpath follows the macOS shortcuts (/etc -> /private/etc).
        # _is_rejected_root compares both this typed path and that result.
        resolved = Path(os.path.realpath(path))
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError(f"Could not use that folder: {exc}") from exc
    if _is_rejected_root(path):
        raise ValueError("That folder is too broad or is a system location, so it will not be scanned.")
    if not resolved.exists() or not resolved.is_dir():
        raise ValueError("That folder does not exist or is not a directory.")
    if not os.access(resolved, os.R_OK):
        raise ValueError("That folder cannot be read.")
    return resolved


def add_scan_folder(cfg: dict, path_str: str) -> Path:
    path = validate_scan_folder(path_str)
    folders = list(cfg.get("scan_folders") or [])
    key = str(path)
    lower = {str(f).lower() for f in folders}
    if key.lower() in lower:
        return path
    if len(folders) >= MAX_CUSTOM_FOLDERS:
        raise ValueError(f"You can add up to {MAX_CUSTOM_FOLDERS} extra folders.")
    folders.append(key)
    cfg["scan_folders"] = folders
    return path


def remove_scan_folder(cfg: dict, path_str: str) -> bool:
    raw = str(Path((path_str or "").strip()).expanduser())
    folders = list(cfg.get("scan_folders") or [])
    kept = [f for f in folders if str(f) != raw and str(f).lower() != raw.lower()]
    # Also match against the resolved form when possible.
    try:
        resolved = str(Path(raw).expanduser().resolve())
        kept = [f for f in kept if str(f) != resolved and str(Path(f).expanduser().resolve()) != Path(resolved)]
    except (OSError, RuntimeError, ValueError):
        pass
    changed = len(kept) != len(folders)
    cfg["scan_folders"] = kept
    return changed


def parse_hf_cache_dirname(name: str) -> str | None:
    """Turn `models--org--name` into `org/name`."""
    if not name or not name.startswith("models--"):
        return None
    rest = name[len("models--") :]
    parts = rest.split("--")
    if len(parts) < 2:
        return None
    return "/".join(parts)


def _size_of(path: Path) -> int:
    try:
        return int(path.stat().st_size)
    except OSError:
        return 0


def _folder_size(path: Path, budget: ScanBudget, root: Path, max_depth: int = 4) -> int:
    total = 0
    try:
        for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
            current = Path(dirpath)
            if not contained(current, root):
                dirnames[:] = []
                continue
            rel = current.relative_to(path)
            depth = len(rel.parts)
            if depth >= max_depth or budget.expired():
                dirnames[:] = []
                if depth >= max_depth:
                    budget._cut("depth")
                continue
            dirnames[:] = [
                d for d in dirnames if d not in SKIP_DIR_NAMES and not d.startswith(".") or d in {"snapshots", "refs"}
            ]
            for name in filenames:
                if budget.expired() or not budget.take_file():
                    dirnames[:] = []
                    break
                fp = current / name
                if fp.is_symlink():
                    continue
                total += _size_of(fp)
    except OSError:
        return total
    return total


def _item(
    *,
    source: str,
    ident: str,
    name: str,
    path: Path,
    size: int,
    filenames: list[str],
    fmt: str = "",
    managed: bool = False,
    machine: str = "",
    tag: str = "",
    relative_path: str = "",
    extra: dict | None = None,
) -> dict:
    row = {
        "source": source,
        "id": ident,
        "name": name,
        "path": str(path),
        "size": int(size),
        "size_gb": (int(size) / (1024**3)) if size else 0.0,
        "filenames": list(filenames),
        "format": fmt,
        "managed": bool(managed),
        "machine": machine,
        "tag": tag,
        "relative_path": relative_path,
    }
    if extra:
        row.update(extra)
    return row


def scan_hf_cache(roots: Iterable[Path], budget: ScanBudget) -> list[dict]:
    items: list[dict] = []
    for root in _unique_existing(roots):
        if budget.expired():
            break
        try:
            entries = list(os.scandir(root))
        except OSError:
            continue
        for entry in entries:
            if budget.expired():
                break
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            ident = parse_hf_cache_dirname(entry.name)
            if not ident:
                continue
            repo_dir = Path(entry.path)
            if not contained(repo_dir, root):
                continue
            snapshot = _preferred_snapshot(repo_dir)
            target = snapshot or repo_dir
            filenames: list[str] = []
            fmt = ""
            size = 0
            if snapshot and snapshot.is_dir():
                try:
                    for dirpath, dirnames, fnames in os.walk(snapshot, followlinks=False):
                        current = Path(dirpath)
                        if not contained(current, snapshot):
                            dirnames[:] = []
                            continue
                        dirnames[:] = [d for d in dirnames if d not in SKIP_DIR_NAMES]
                        for fn in fnames:
                            if budget.expired() or not budget.take_file():
                                dirnames[:] = []
                                break
                            fp = current / fn
                            if fp.is_symlink():
                                # Only count a symlink if it stays in the snapshot.
                                try:
                                    if not contained(fp, snapshot):
                                        continue
                                except (OSError, RuntimeError):
                                    continue
                            size += _size_of(fp)
                            lower = fn.lower()
                            if lower.endswith(tuple(WEIGHT_SUFFIXES)):
                                filenames.append(fn)
                                if lower.endswith(".gguf"):
                                    fmt = fmt or "gguf"
                                elif lower.endswith(".safetensors"):
                                    fmt = fmt or "safetensors"
                                elif lower.endswith(".mlx") or lower.endswith(".npz"):
                                    fmt = fmt or "mlx"
                except OSError:
                    pass
            else:
                size = _folder_size(repo_dir, budget, root)
            items.append(
                _item(
                    source="hf_cache",
                    ident=ident,
                    name=ident,
                    path=target,
                    size=size,
                    filenames=filenames,
                    fmt=fmt,
                )
            )
    return items


def _preferred_snapshot(repo_dir: Path) -> Path | None:
    refs = repo_dir / "refs"
    snapshots = repo_dir / "snapshots"
    if not snapshots.is_dir():
        return None
    # Prefer the revision named in refs/main, then any other ref, then the first snapshot.
    candidates: list[str] = []
    for name in ("main", "master", "refs/main"):
        ref = refs / name if name != "refs/main" else repo_dir / "refs" / "main"
        try:
            if ref.is_file():
                candidates.append(ref.read_text(encoding="utf-8").strip())
        except OSError:
            continue
    if refs.is_dir():
        try:
            for entry in os.scandir(refs):
                if entry.is_file(follow_symlinks=False):
                    try:
                        candidates.append(Path(entry.path).read_text(encoding="utf-8").strip())
                    except OSError:
                        continue
        except OSError:
            pass
    for rev in candidates:
        if not rev or ".." in rev or "/" in rev or "\\" in rev:
            continue
        snap = snapshots / rev
        try:
            if snap.is_dir() and contained(snap, snapshots):
                return snap
        except (OSError, RuntimeError, ValueError):
            continue
    try:
        for entry in os.scandir(snapshots):
            if entry.is_dir(follow_symlinks=False):
                snap = Path(entry.path)
                if contained(snap, snapshots):
                    return snap
    except OSError:
        return None
    return None


def _ollama_id_from_parts(parts: tuple[str, ...]) -> str:
    """Build a display id from a manifest path under manifests/."""
    # Typical: registry.ollama.ai / library / llama3 / latest
    # or: registry.ollama.ai / user / model / tag
    # or: hf.co / owner / model / tag
    cleaned = [p for p in parts if p and p not in {".", ".."}]
    if not cleaned:
        return ""
    # Drop the registry host if present (has a dot, or is a known host).
    if "." in cleaned[0] or cleaned[0] in {"registry", "registry.ollama.ai", "hf.co"}:
        cleaned = cleaned[1:]
    if not cleaned:
        return ""
    if cleaned[0] == "library" and len(cleaned) >= 2:
        name = cleaned[1]
        tag = cleaned[2] if len(cleaned) >= 3 else "latest"
        return f"{name}:{tag}"
    if len(cleaned) >= 3:
        return f"{cleaned[0]}/{cleaned[1]}:{cleaned[2]}"
    if len(cleaned) == 2:
        return f"{cleaned[0]}:{cleaned[1]}"
    return cleaned[0]


def scan_ollama(roots: Iterable[Path], budget: ScanBudget) -> list[dict]:
    items: list[dict] = []
    for root in _unique_existing(roots):
        if budget.expired():
            break
        # Accept either the models folder or its parent (~/.ollama).
        models = root / "models" if (root / "models" / "manifests").is_dir() else root
        manifests = models / "manifests"
        if not manifests.is_dir():
            # Maybe root itself is the manifests folder.
            if root.name == "manifests":
                manifests = root
                models = root.parent
            else:
                continue
        try:
            for dirpath, dirnames, filenames in os.walk(manifests, followlinks=False):
                current = Path(dirpath)
                if not contained(current, manifests):
                    dirnames[:] = []
                    continue
                if budget.expired():
                    dirnames[:] = []
                    break
                rel = current.relative_to(manifests)
                if len(rel.parts) > 8:
                    dirnames[:] = []
                    continue
                dirnames[:] = [d for d in dirnames if d not in SKIP_DIR_NAMES]
                for fn in filenames:
                    if fn.startswith(".") or not budget.take_file():
                        continue
                    fp = current / fn
                    if fp.is_symlink() or not fp.is_file():
                        continue
                    ident = _ollama_id_from_parts(rel.parts + (fn,))
                    size, fmt = _ollama_manifest_size(fp)
                    items.append(
                        _item(
                            source="ollama",
                            ident=ident or fn,
                            name=ident or fn,
                            path=fp,
                            size=size,
                            filenames=[fn],
                            fmt=fmt or "ollama",
                        )
                    )
        except OSError:
            continue
    return items


def _ollama_manifest_size(path: Path) -> tuple[int, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return _size_of(path), "ollama"
    if not isinstance(data, dict):
        return _size_of(path), "ollama"
    total = 0
    for layer in data.get("layers") or []:
        if not isinstance(layer, dict):
            continue
        try:
            total += int(layer.get("size") or 0)
        except (TypeError, ValueError):
            continue
    cfg = data.get("config") or {}
    if isinstance(cfg, dict):
        try:
            total += int(cfg.get("size") or 0)
        except (TypeError, ValueError):
            pass
    return total, "ollama"


def scan_weight_tree(
    roots: Iterable[Path],
    budget: ScanBudget,
    *,
    source: str,
) -> list[dict]:
    """Walk a folder for GGUF files, safetensors bundles, and MLX directories."""
    items: list[dict] = []
    # Group safetensors / mlx by parent folder so a model is one row, not 50 shards.
    bundles: dict[str, dict] = {}
    for root in _unique_existing(roots):
        if budget.expired():
            break
        try:
            for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
                current = Path(dirpath)
                if not contained(current, root):
                    dirnames[:] = []
                    continue
                try:
                    rel = current.relative_to(root)
                except ValueError:
                    dirnames[:] = []
                    continue
                depth = len(rel.parts)
                if depth >= budget.max_depth:
                    dirnames[:] = []
                    budget._cut("depth")
                    continue
                if budget.expired():
                    dirnames[:] = []
                    break
                if len(dirnames) + len(filenames) > budget.max_dir_entries:
                    dirnames[:] = []
                    budget._cut("directory size")
                    continue
                dirnames[:] = [d for d in dirnames if d not in SKIP_DIR_NAMES]
                ggufs: list[tuple[str, int]] = []
                tensors: list[tuple[str, int]] = []
                has_mlx = False
                other_weights = 0
                for fn in filenames:
                    if not budget.take_file():
                        dirnames[:] = []
                        break
                    lower = fn.lower()
                    fp = current / fn
                    if fp.is_symlink():
                        try:
                            if not contained(fp, root):
                                continue
                        except (OSError, RuntimeError, ValueError):
                            continue
                    size = _size_of(fp)
                    if lower.endswith(".gguf"):
                        ggufs.append((fn, size))
                    elif lower.endswith(".safetensors"):
                        tensors.append((fn, size))
                    elif lower.endswith(".mlx") or lower.endswith(".npz") or lower.startswith("mlx_model"):
                        has_mlx = True
                        other_weights += size
                        tensors.append((fn, size))
                    elif fn == "config.json":
                        other_weights += 0
                ident = _id_from_relative(root, current)
                for fn, size in ggufs:
                    items.append(
                        _item(
                            source=source,
                            ident=ident or fn,
                            name=fn,
                            path=current / fn,
                            size=size,
                            filenames=[fn],
                            fmt="gguf",
                        )
                    )
                if tensors or has_mlx:
                    key = str(current)
                    bundle = bundles.setdefault(
                        key,
                        {
                            "source": source,
                            "ident": ident or current.name,
                            "name": ident or current.name,
                            "path": current,
                            "size": 0,
                            "filenames": [],
                            "fmt": "mlx" if has_mlx else "safetensors",
                        },
                    )
                    bundle["size"] += sum(s for _, s in tensors) + other_weights
                    bundle["filenames"].extend(fn for fn, _ in tensors)
                    if has_mlx:
                        bundle["fmt"] = "mlx"
        except OSError:
            continue
    for bundle in bundles.values():
        items.append(
            _item(
                source=bundle["source"],
                ident=bundle["ident"],
                name=bundle["name"],
                path=bundle["path"],
                size=bundle["size"],
                filenames=bundle["filenames"],
                fmt=bundle["fmt"],
            )
        )
    return items


def _id_from_relative(root: Path, folder: Path) -> str:
    try:
        rel = folder.relative_to(root)
    except ValueError:
        return folder.name
    parts = [p for p in rel.parts if p not in {".", ".."}]
    if len(parts) >= 2:
        return f"{parts[0]}/{parts[1]}"
    if len(parts) == 1:
        return parts[0]
    return folder.name


def scan_app_folders(cfg: dict, root: Path | None) -> list[dict]:
    items: list[dict] = []
    for row in W.list_installed(cfg, machine=None, root=root):
        path = Path(row["path"])
        name = row.get("name") or path.name
        ident = _id_from_app_name(name)
        filenames = [name] if path.is_file() else _list_weight_names(path)
        fmt = ""
        for fn in filenames:
            lower = fn.lower()
            if lower.endswith(".gguf"):
                fmt = "gguf"
                break
            if lower.endswith(".safetensors"):
                fmt = fmt or "safetensors"
            if "mlx" in lower or lower.endswith(".npz"):
                fmt = fmt or "mlx"
        items.append(
            _item(
                source="app",
                ident=ident,
                name=name,
                path=path,
                size=int((row.get("size_gb") or 0) * (1024**3)),
                filenames=filenames,
                fmt=fmt,
                managed=True,
                machine=str(row.get("machine") or ""),
                tag=str(row.get("tag") or ""),
                relative_path=str(row.get("relative_path") or name),
            )
        )
    return items


def _id_from_app_name(name: str) -> str:
    # Manual downloads use org__name. GGUF files stay as filenames.
    stem = name
    if stem.lower().endswith(".gguf"):
        stem = stem[:-5]
    if "__" in name and "/" not in name:
        return name.replace("__", "/", 1)
    return stem


def _list_weight_names(folder: Path, limit: int = 40) -> list[str]:
    names: list[str] = []
    try:
        for p in folder.rglob("*"):
            if len(names) >= limit:
                break
            if not p.is_file():
                continue
            if ".git" in p.parts or ".cache" in p.parts:
                continue
            if p.suffix.lower() in WEIGHT_SUFFIXES:
                names.append(p.name)
    except OSError:
        return names
    return names


def scan_local_models(
    cfg: dict | None = None,
    root: Path | None = None,
    *,
    extra_folders: Iterable[str] | None = None,
    hf_cache_roots: Iterable[Path] | None = None,
    ollama_roots: Iterable[Path] | None = None,
    lmstudio_roots: Iterable[Path] | None = None,
    include_defaults: bool = True,
    include_app: bool = True,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_files: int = DEFAULT_MAX_FILES,
    max_seconds: float = DEFAULT_MAX_SECONDS,
) -> dict:
    """Scan known (and optional extra) locations. Read-only. Always returns a dict."""
    cfg = cfg or {}
    budget = ScanBudget(max_depth=max_depth, max_files=max_files, max_seconds=max_seconds)
    started = time.monotonic()
    errors: list[str] = []
    items: list[dict] = []

    def _run(label: str, fn):
        if budget.expired():
            return
        try:
            found = fn()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{label}: {exc}")
            return
        items.extend(found or [])

    hf_roots = list(hf_cache_roots) if hf_cache_roots is not None else (default_hf_cache_roots() if include_defaults else [])
    ol_roots = list(ollama_roots) if ollama_roots is not None else (default_ollama_roots() if include_defaults else [])
    lm_roots = list(lmstudio_roots) if lmstudio_roots is not None else (default_lmstudio_roots() if include_defaults else [])

    _run("Hugging Face cache", lambda: scan_hf_cache(hf_roots, budget))
    _run("Ollama", lambda: scan_ollama(ol_roots, budget))
    _run("LM Studio", lambda: scan_weight_tree(lm_roots, budget, source="lmstudio"))

    custom: list[Path] = []
    raw_folders = list(extra_folders) if extra_folders is not None else list(cfg.get("scan_folders") or [])
    for raw in raw_folders:
        try:
            custom.append(validate_scan_folder(str(raw)))
        except ValueError as exc:
            errors.append(str(exc))
    if custom:
        _run("custom folder", lambda: scan_weight_tree(custom, budget, source="custom"))

    if include_app and cfg is not None:
        _run("app folder", lambda: scan_app_folders(cfg, root))

    # Drop exact path duplicates, preferring a more specific source.
    source_rank = {"app": 0, "custom": 1, "lmstudio": 2, "ollama": 3, "hf_cache": 4}
    by_path: dict[str, dict] = {}
    for item in items:
        key = str(item.get("path") or "").lower()
        if not key:
            continue
        prev = by_path.get(key)
        if prev is None or source_rank.get(item["source"], 9) < source_rank.get(prev["source"], 9):
            by_path[key] = item
    cleaned = list(by_path.values())
    cleaned.sort(key=lambda r: (r.get("source") or "", (r.get("id") or "").lower(), r.get("name") or ""))

    counts: dict[str, int] = {}
    for item in cleaned:
        counts[item["source"]] = counts.get(item["source"], 0) + 1

    elapsed_ms = int((time.monotonic() - started) * 1000)
    return {
        "items": cleaned,
        "sources": counts,
        "truncated": budget.truncated,
        "truncate_reasons": list(budget.reasons),
        "errors": errors,
        "elapsed_ms": elapsed_ms,
        "scan_folders": [str(p) for p in custom],
    }


def identity_sets(result: dict | None) -> dict[str, set[str]]:
    """Build lowercase lookup sets for matching search results."""
    ids: set[str] = set()
    filenames: set[str] = set()
    by_id: dict[str, dict] = {}
    by_file: dict[str, dict] = {}
    for item in (result or {}).get("items") or []:
        ident = (item.get("id") or "").strip()
        if ident:
            key = ident.lower()
            ids.add(key)
            by_id.setdefault(key, item)
            # Also accept org__name and bare repo name.
            if "/" in ident:
                ids.add(ident.replace("/", "__").lower())
                by_id.setdefault(ident.replace("/", "__").lower(), item)
            if "__" in ident and "/" not in ident:
                ids.add(ident.replace("__", "/", 1).lower())
        for fn in item.get("filenames") or []:
            base = Path(str(fn)).name.lower()
            if base:
                filenames.add(base)
                by_file.setdefault(base, item)
        name = item.get("name") or ""
        if name:
            base = Path(str(name)).name.lower()
            if base:
                filenames.add(base)
                by_file.setdefault(base, item)
    return {"ids": ids, "filenames": filenames, "by_id": by_id, "by_file": by_file}


def annotate_already_local(rows: list[dict], identities: dict, *, file_keys: tuple[str, ...] = ("file", "file_path")) -> list[dict]:
    """Mark rows that match a local model by repo id or GGUF filename."""
    ids = identities.get("ids") or set()
    files = identities.get("filenames") or set()
    by_id = identities.get("by_id") or {}
    by_file = identities.get("by_file") or {}
    for row in rows:
        mid = (row.get("id") or "").strip()
        mid_l = mid.lower()
        hit = by_id.get(mid_l)
        if not hit:
            for key in file_keys:
                raw = row.get(key) or ""
                base = Path(str(raw)).name.lower()
                if base and base != "(whole model)" and base in files:
                    hit = by_file.get(base)
                    if hit:
                        break
        if hit:
            row["already_local"] = True
            row["already_local_source"] = hit.get("source") or ""
            row["already_local_path"] = hit.get("path") or ""
            row["already_local_name"] = hit.get("name") or hit.get("id") or ""
        else:
            row.setdefault("already_local", False)
    return rows


def source_label(source: str) -> str:
    return {
        "hf_cache": "Hugging Face cache",
        "ollama": "Ollama",
        "lmstudio": "LM Studio",
        "app": "Downloaded with this app",
        "custom": "Your folder",
    }.get(source, source)
