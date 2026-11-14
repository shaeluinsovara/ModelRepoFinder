"""v1.7 scanner for code projects already on this computer. Read-only, no network."""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

import helpers  # noqa: F401
import local_scan as L


def make_git_repo(folder: Path, url: str, branch: str = "main") -> Path:
    git = folder / ".git"
    git.mkdir(parents=True)
    (git / "HEAD").write_text(f"ref: refs/heads/{branch}\n", encoding="utf-8")
    (git / "config").write_text(
        "[core]\n\trepositoryformatversion = 0\n"
        "[remote \"upstream\"]\n\turl = https://github.com/other/fork.git\n"
        f"[remote \"origin\"]\n\turl = {url}\n\tfetch = +refs/heads/*:refs/remotes/origin/*\n"
        "[branch \"main\"]\n\tremote = origin\n",
        encoding="utf-8",
    )
    (folder / "README.md").write_text("hello", encoding="utf-8")
    return folder


class RemoteUrlTests(unittest.TestCase):
    def test_url_shapes(self):
        cases = {
            "https://github.com/ggml-org/llama.cpp.git": ("github.com", "ggml-org/llama.cpp", True),
            "https://github.com/ggml-org/llama.cpp": ("github.com", "ggml-org/llama.cpp", True),
            "git@github.com:ollama/ollama.git": ("github.com", "ollama/ollama", True),
            "ssh://git@github.com/owner/repo.git": ("github.com", "owner/repo", True),
            "https://user:secret@github.com/owner/repo.git": ("github.com", "owner/repo", True),
            "https://gitlab.com/group/sub/proj.git": ("gitlab.com", "sub/proj", False),
        }
        for url, (host, rid, gh) in cases.items():
            with self.subTest(url=url):
                parsed = L.parse_git_remote_url(url)
                self.assertEqual(parsed["host"], host)
                self.assertEqual(parsed["id"], rid)
                self.assertEqual(parsed["is_github"], gh)
                self.assertNotIn("secret", parsed["url"])  # credentials never kept

    def test_bad_urls(self):
        for url in ("", "not a url", "https://github.com/onlyowner", "C:\\stuff"):
            with self.subTest(url=url):
                self.assertIsNone(L.parse_git_remote_url(url))

    def test_read_remotes(self):
        text = '[remote "origin"]\n  url = git@github.com:a/b.git\n[branch "x"]\n  url = nope\n'
        self.assertEqual(L.read_git_remotes(text), {"origin": "git@github.com:a/b.git"})


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "code"
        self.root.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def scan(self, depth=3, **kw):
        return L.scan_local_repos({}, roots=[(self.root, depth)], **kw)

    def test_finds_clones_zip_folders_and_zips(self):
        make_git_repo(self.root / "llama.cpp", "https://github.com/ggml-org/llama.cpp.git")
        make_git_repo(self.root / "work" / "whisper", "git@github.com:openai/whisper.git", branch="dev")
        zipped = self.root / "ComfyUI-master"
        zipped.mkdir()
        (zipped / "requirements.txt").write_text("x", encoding="utf-8")
        not_project = self.root / "notes-main"
        not_project.mkdir()  # no project files: ignored
        (self.root / "ollama-main.zip").write_bytes(b"PK\x05\x06" + b"\0" * 18)
        result = self.scan()
        by_name = {r["name"]: r for r in result["items"]}
        self.assertEqual(by_name["llama.cpp"]["id"], "ggml-org/llama.cpp")
        self.assertEqual(by_name["llama.cpp"]["source"], "git")
        self.assertEqual(by_name["llama.cpp"]["branch"], "main")
        self.assertEqual(by_name["whisper"]["id"], "openai/whisper")
        self.assertEqual(by_name["whisper"]["branch"], "dev")
        self.assertEqual(by_name["ComfyUI"]["source"], "zip_folder")
        self.assertEqual(by_name["ComfyUI"]["id"], "")
        self.assertEqual(by_name["ollama"]["source"], "zip_file")
        self.assertNotIn("notes", by_name)
        self.assertEqual(result["sources"].get("git"), 2)

    def test_never_looks_inside_a_project(self):
        outer = make_git_repo(self.root / "outer", "https://github.com/a/outer.git")
        make_git_repo(outer / "vendor" / "inner", "https://github.com/a/inner.git")
        names = [r["name"] for r in self.scan()["items"]]
        self.assertEqual(names, ["outer"])

    def test_depth_limit(self):
        make_git_repo(self.root / "a" / "b" / "c" / "deep", "https://github.com/a/deep.git")
        self.assertEqual(self.scan(depth=2)["items"], [])
        self.assertEqual(len(self.scan(depth=5)["items"]), 1)

    def test_worktree_git_file(self):
        real = make_git_repo(self.tmp / "store" / "main", "https://github.com/a/tree.git")
        wt = self.root / "tree-wt"
        wt.mkdir()
        (wt / ".git").write_text(f"gitdir: {real / '.git'}\n", encoding="utf-8")
        items = self.scan()["items"]
        self.assertEqual([r["id"] for r in items], ["a/tree"])

    def test_read_only(self):
        repo = make_git_repo(self.root / "r", "https://github.com/a/r.git")
        before = sorted((p, p.stat().st_mtime_ns) for p in repo.rglob("*"))
        self.scan()
        after = sorted((p, p.stat().st_mtime_ns) for p in repo.rglob("*"))
        self.assertEqual(before, after)

    def test_denylisted_roots_skipped(self):
        system_root = "C:\\Windows" if os.name == "nt" else "/etc"
        result = L.scan_local_repos({}, roots=[(Path(system_root), 2)])
        self.assertEqual(result["items"], [])
        result = L.scan_local_repos({}, roots=[(Path(os.path.abspath(os.sep)), 1)])
        self.assertEqual(result["items"], [])

    def test_bad_custom_folder_reported_not_scanned(self):
        result = L.scan_local_repos({"scan_folders": ["/"]}, roots=[])
        self.assertEqual(result["items"], [])
        self.assertTrue(result["errors"])

    def test_symlink_loop_safe(self):
        make_git_repo(self.root / "r", "https://github.com/a/r.git")
        try:
            os.symlink(self.root, self.root / "loop", target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks not available")
        items = self.scan(depth=6)["items"]
        self.assertEqual(len(items), 1)

    def test_time_budget(self):
        result = self.scan(max_seconds=0)
        self.assertIn("items", result)


class AnnotateTests(unittest.TestCase):
    def test_marks_github_results(self):
        found = {"items": [
            {"id": "ggml-org/llama.cpp", "name": "llama.cpp", "is_github": True, "path": "/x/llama.cpp", "source": "git"},
            {"id": "", "name": "ComfyUI", "is_github": False, "path": "/x/ComfyUI-master", "source": "zip_folder"},
            {"id": "group/proj", "name": "proj", "is_github": False, "path": "/x/proj", "source": "git"},
        ]}
        ident = L.repo_identity_sets(found)
        rows = [{"id": "ggml-org/llama.cpp"}, {"id": "comfyanonymous/ComfyUI"}, {"id": "someone/proj"}, {"id": "a/b"}]
        L.annotate_repos_local(rows, ident)
        self.assertTrue(rows[0]["already_local"])
        self.assertEqual(rows[0]["already_local_source"], "git")
        self.assertFalse(rows[1]["already_local"])
        self.assertTrue(rows[1]["maybe_local"])  # name-only match from a zip download
        self.assertFalse(rows[2].get("maybe_local", False))  # a different host's project with an id
        self.assertFalse(rows[3]["already_local"])

    def test_source_labels(self):
        for src in ("git", "zip_folder", "zip_file"):
            self.assertTrue(L.repo_source_label(src))


if __name__ == "__main__":
    unittest.main()
