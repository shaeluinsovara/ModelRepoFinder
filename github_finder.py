#!/usr/bin/env python3
"""GitHub Repo Finder helpers for ModelRepoFinder.

Uses the public GitHub search API. No token is required; an optional token
(stored only on this computer) raises the rate limit.
"""
from __future__ import annotations

import json
import re
import os
import shutil
import stat
import subprocess
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import watcher as W

USER_AGENT = W.USER_AGENT
TOKEN_FILE = W.ROOT / ".github_token"
ProgressCb = Callable[[str], None]
_SEARCH_TIMES: list[float] = []


def _split_or_query(query: str, max_terms: int = 3) -> list[str]:
    """GitHub search allows only five AND/OR/NOT operators. Keep each chunk short."""
    import re

    parts = [p.strip() for p in re.split(r"\s+OR\s+", query, flags=re.I) if p.strip()]
    if len(parts) <= 1:
        return [query]
    chunks = []
    for i in range(0, len(parts), max_terms):
        piece = " OR ".join(parts[i : i + max_terms])
        chunks.append(piece)
    return chunks


def _respect_search_rate(token: str | None, progress_cb: ProgressCb | None = None) -> None:
    # Unauthenticated search is about 10 requests a minute; tokens get more.
    limit = 25 if token else 8
    now = time.time()
    _SEARCH_TIMES[:] = [t for t in _SEARCH_TIMES if now - t < 60]
    if len(_SEARCH_TIMES) >= limit:
        wait = 61 - (now - _SEARCH_TIMES[0])
        if wait > 0:
            if progress_cb:
                progress_cb("Waiting a moment so GitHub does not block us…")
            time.sleep(max(0, wait))
    _SEARCH_TIMES.append(time.time())


def cutoff_date(months: int = 12) -> str:
    day = datetime.now(timezone.utc) - timedelta(days=int(months * 30.5))
    return day.strftime("%Y-%m-%d")


def load_token(root: Path | None = None) -> str:
    return W.load_secret(".github_token", W._KEYRING_GH, root)


def save_token(token: str, root: Path | None = None) -> None:
    W.save_secret(".github_token", W._KEYRING_GH, token, root)


def git_available() -> bool:
    return shutil.which("git") is not None


def _headers(token: str | None) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _get_json(url: str, token: str | None, timeout: int = 45) -> dict:
    req = Request(url, headers=_headers(token))
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            remaining = resp.headers.get("X-RateLimit-Remaining")
            data = json.loads(raw.decode("utf-8"))
            if remaining is not None:
                data["_rate_remaining"] = remaining
            return data
    except HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
        if exc.code in {401, 403, 429}:
            hint = (
                "GitHub is rate-limiting these searches. Wait a minute, or paste a "
                "personal access token in the app to get a higher limit."
            )
            if exc.code == 401:
                hint = "That GitHub token was not accepted. Check it, or clear the field to search without one."
            raise RuntimeError(hint) from exc
        raise RuntimeError(f"GitHub request failed ({exc.code}): {body[:200] or exc}") from exc
    except URLError as exc:
        raise RuntimeError(f"Could not reach GitHub. Check your internet and try again. ({exc.reason})") from exc


def search_repositories(
    query: str,
    token: str | None = None,
    per_page: int = 20,
    months: int = 12,
    progress_cb: ProgressCb | None = None,
) -> list[dict]:
    """Search public repos, already filtered to recent activity, ranked by stars."""
    cutoff = cutoff_date(months)
    # Split long OR lists so we stay under GitHub's five-operator limit.
    # Do not wrap the query in extra parentheses; that can make GitHub return zero hits.
    chunks = _split_or_query(query, max_terms=3)
    items = []
    seen_ids: set[int] = set()
    for chunk in chunks:
        _respect_search_rate(token, progress_cb)
        q = f"{chunk} archived:false pushed:>={cutoff}"
        params = urlencode({"q": q, "sort": "stars", "order": "desc", "per_page": per_page})
        url = f"https://api.github.com/search/repositories?{params}"
        data = _get_json(url, token)
        for item in data.get("items") or []:
            iid = item.get("id")
            if iid in seen_ids:
                continue
            if iid is not None:
                seen_ids.add(iid)
            items.append(item)
    out = []
    cutoff_dt = datetime.now(timezone.utc) - timedelta(days=int(months * 30.5))
    for item in items:
        pushed = item.get("pushed_at") or item.get("updated_at") or ""
        try:
            pushed_dt = datetime.fromisoformat(pushed.replace("Z", "+00:00"))
        except ValueError:
            continue
        if pushed_dt < cutoff_dt:
            continue
        license_info = item.get("license") or {}
        license_name = license_info.get("spdx_id") or license_info.get("name") or "Unknown"
        if license_name in {"NOASSERTION", "NONE"}:
            license_name = "Unknown"
        full = item.get("full_name") or ""
        out.append(
            {
                "id": full,
                "name": item.get("name") or full,
                "url": item.get("html_url") or f"https://github.com/{full}",
                "description": (item.get("description") or "").strip(),
                "stars": int(item.get("stargazers_count") or 0),
                "pushed_at": pushed,
                "pushed_at_pretty": _pretty_date(pushed),
                "license": license_name,
                "default_branch": item.get("default_branch") or "main",
                "language": item.get("language") or "",
                "forks": int(item.get("forks_count") or 0),
            }
        )
    out.sort(key=lambda r: (-r["stars"], r["id"]))
    return out


def _pretty_date(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        return iso[:10] if iso else ""


def collect_github_suggestions(
    cfg: dict,
    machine_key: str,
    token: str | None = None,
    progress_cb: ProgressCb | None = None,
    categories: list[str] | None = None,
    per_page: int = 20,
) -> dict:
    tags = W.all_github_tags(cfg)
    machines = W.enabled_machines(cfg)
    machine = machines.get(machine_key) or {}
    selected = categories if categories is not None else list(machine.get("github_categories") or tags.keys())
    selected = [k for k in selected if k in tags]
    allow = set(cfg.get("github_allow_list") or [])

    result_tags = []
    for key in selected:
        tag = tags[key]
        title = tag.get("name") or key
        keep = int(tag.get("keep") or 5)
        queries = list(tag.get("queries") or [])
        if not queries:
            queries = [tag.get("search") or title]
        slot: dict = {
            "name": key,
            "title": title,
            "keep": keep,
            "description": tag.get("description") or "",
            "repos": [],
            "error": None,
        }
        merged: dict[str, dict] = {}
        try:
            for q in queries:
                if progress_cb:
                    progress_cb(f"Searching GitHub for {title}…")
                for repo in search_repositories(q, token=token, per_page=per_page, progress_cb=progress_cb):
                    merged.setdefault(repo["id"], repo)
                # Be kind to the unauthenticated rate limit when several queries run.
                if len(queries) > 1:
                    time.sleep(0.6)
        except Exception as exc:  # noqa: BLE001
            slot["error"] = str(exc)
            result_tags.append(slot)
            continue
        ranked = sorted(merged.values(), key=lambda r: (-r["stars"], r["id"]))[:keep]
        for repo in ranked:
            repo = dict(repo)
            repo["tag"] = key
            repo["allowed"] = repo["id"] in allow
            slot["repos"].append(repo)
        result_tags.append(slot)

    return {
        "machine": machine_key,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "git_available": git_available(),
        "tags": result_tags,
    }


def _safe_repo_id(repo_id: str) -> str:
    repo_id = (repo_id or "").strip().strip("/")
    if repo_id.count("/") != 1 or any(c in repo_id for c in " \t\\:?#"):
        raise ValueError(f"That does not look like a GitHub repo name: {repo_id}")
    owner, name = repo_id.split("/")
    if not owner or not name or ".." in repo_id:
        raise ValueError(f"That does not look like a GitHub repo name: {repo_id}")
    return f"{owner}/{name}"


def folder_name(repo_id: str) -> str:
    return repo_id.replace("/", "__")


def clone_or_zip(
    repo_id: str,
    dest_dir: Path,
    default_branch: str = "main",
    progress: dict | None = None,
    prefer_git: bool | None = None,
) -> Path:
    """Clone with git when possible, otherwise download the default-branch zip."""
    repo_id = _safe_repo_id(repo_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / folder_name(repo_id)
    use_git = git_available() if prefer_git is None else prefer_git
    if use_git and git_available():
        if progress is not None:
            progress["message"] = f"Cloning {repo_id} with git…"
            progress["percent"] = 5
        if target.exists():
            raise FileExistsError(f"{target} already exists. Use Update, or Retire it first.")
        url = f"https://github.com/{repo_id}.git"
        result = subprocess.run(
            ["git", "clone", "--depth", "1", url, str(target)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "git clone failed").strip()
            raise RuntimeError(f"git clone failed: {err[:400]}")
        if progress is not None:
            progress["percent"] = 100
        return target
    return _download_zip(repo_id, target, default_branch, progress)


def _download_zip(repo_id: str, target: Path, default_branch: str, progress: dict | None) -> Path:
    branches = [default_branch or "main"]
    if "master" not in branches:
        branches.append("master")
    if "main" not in branches:
        branches.append("main")
    last_err: Exception | None = None
    tmp_zip = target.parent / f".{folder_name(repo_id)}.zip"
    for branch in branches:
        url = f"https://codeload.github.com/{repo_id}/zip/refs/heads/{branch}"
        try:
            if progress is not None:
                progress["message"] = f"Downloading zip of {repo_id} ({branch})…"
                progress["percent"] = 5
            _stream_to_file(url, tmp_zip, progress)
            _extract_zip(tmp_zip, target)
            if progress is not None:
                progress["percent"] = 100
            return target
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            continue
        finally:
            if tmp_zip.exists():
                try:
                    tmp_zip.unlink()
                except OSError:
                    pass
    raise RuntimeError(f"Could not download a zip of {repo_id}: {last_err}")


def _stream_to_file(url: str, dest: Path, progress: dict | None) -> None:
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/zip"})
    with urlopen(req, timeout=120) as resp, dest.open("wb") as out:
        total = resp.headers.get("Content-Length")
        total_n = int(total) if total and total.isdigit() else 0
        if progress is not None:
            progress["total_bytes"] = total_n
            progress["downloaded_bytes"] = 0
        copied = 0
        while True:
            chunk = resp.read(1024 * 64)
            if not chunk:
                break
            out.write(chunk)
            copied += len(chunk)
            if progress is not None:
                progress["downloaded_bytes"] = copied
                if total_n:
                    progress["percent"] = round(100.0 * copied / total_n, 1)


def _is_zip_symlink(info: zipfile.ZipInfo) -> bool:
    # Unix: the upper 16 bits of external_attr are st_mode.
    mode = (info.external_attr >> 16) & 0xFFFF
    return stat.S_ISLNK(mode)


def _zip_member_parts(name: str) -> list[str]:
    raw = (name or "").replace("\\", "/")
    if not raw or raw.startswith("/") or raw.startswith("\\"):
        raise ValueError(f"Refusing to extract an unsafe path from the archive: {name}")
    if re.match(r"^[a-zA-Z]:", raw) or raw.startswith("//"):
        raise ValueError(f"Refusing to extract an unsafe path from the archive: {name}")
    parts = [p for p in raw.split("/") if p and p != "."]
    if not parts or any(p == ".." for p in parts):
        raise ValueError(f"Refusing to extract an unsafe path from the archive: {name}")
    return parts


def validated_zip_dest(info: zipfile.ZipInfo, staging: Path) -> Path:
    """Return the destination for one archive member, or raise ValueError.

    Rejects absolute paths, `..` components, and paths that resolve outside staging.
    """
    staging = staging.resolve()
    parts = _zip_member_parts(info.filename)
    dest = staging.joinpath(*parts).resolve()
    if dest != staging and staging not in dest.parents:
        raise ValueError(f"Refusing to extract an unsafe path from the archive: {info.filename}")
    return dest


def _symlink_target_is_safe(dest: Path, target: str, staging: Path) -> bool:
    if not target:
        return False
    cleaned = target.replace("\\", "/")
    if Path(cleaned).is_absolute() or re.match(r"^[a-zA-Z]:", cleaned) or cleaned.startswith("/"):
        return False
    if ".." in Path(cleaned).parts:
        # Still allow a relative link that ultimately stays inside staging.
        pass
    resolved = (dest.parent / target).resolve()
    staging = staging.resolve()
    return resolved == staging or staging in resolved.parents


def extract_zip_safely(zip_path: Path, staging: Path) -> None:
    """Extract `zip_path` into `staging`, verifying every member stays underneath."""
    staging = staging.resolve()
    staging.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            dest = validated_zip_dest(info, staging)
            if _is_zip_symlink(info):
                target = zf.read(info).decode("utf-8", "replace")
                if not _symlink_target_is_safe(dest, target, staging):
                    raise ValueError(
                        f"Refusing to extract a symbolic link that leaves the staging folder: {info.filename}"
                    )
                dest.parent.mkdir(parents=True, exist_ok=True)
                try:
                    if dest.exists() or dest.is_symlink():
                        dest.unlink()
                    dest.symlink_to(target)
                except OSError as exc:
                    # Windows may refuse symlink creation; skip a safe link rather than fail.
                    import sys

                    print(f"Could not create symbolic link {info.filename}: {exc}", file=sys.stderr)
                continue
            if info.is_dir() or info.filename.endswith("/"):
                dest.mkdir(parents=True, exist_ok=True)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info, "r") as src, dest.open("wb") as out:
                shutil.copyfileobj(src, out)


def _extract_zip(zip_path: Path, target: Path) -> None:
    staging = target.parent / (target.name + ".staging")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        extract_zip_safely(zip_path, staging)
        # GitHub zips have a single top-level folder, repo-branch/.
        entries = [p for p in staging.iterdir() if p.name != "__MACOSX"]
        source = entries[0] if len(entries) == 1 and entries[0].is_dir() else staging
        if target.exists():
            shutil.rmtree(target)
        shutil.move(str(source), str(target))
    except Exception:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        raise
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)

def update_repo(
    repo_id: str,
    dest_dir: Path,
    default_branch: str = "main",
    progress: dict | None = None,
) -> Path:
    repo_id = _safe_repo_id(repo_id)
    target = dest_dir / folder_name(repo_id)
    if not target.exists():
        return clone_or_zip(repo_id, dest_dir, default_branch, progress)
    if (target / ".git").exists() and git_available():
        if progress is not None:
            progress["message"] = f"Updating {repo_id} with git pull…"
            progress["percent"] = 10
        result = subprocess.run(
            ["git", "-C", str(target), "pull", "--ff-only"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "git pull failed").strip()
            raise RuntimeError(f"git pull failed: {err[:400]}")
        if progress is not None:
            progress["percent"] = 100
        return target
    # Zip install: re-download and replace.
    if progress is not None:
        progress["message"] = f"Re-downloading {repo_id}…"
    backup = target.with_name(target.name + ".bak")
    if backup.exists():
        shutil.rmtree(backup, ignore_errors=True)
    shutil.move(str(target), str(backup))
    try:
        path = _download_zip(repo_id, target, default_branch, progress)
        shutil.rmtree(backup, ignore_errors=True)
        return path
    except Exception:
        if not target.exists() and backup.exists():
            shutil.move(str(backup), str(target))
        raise
