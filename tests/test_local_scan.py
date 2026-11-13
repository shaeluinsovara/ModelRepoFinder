"""Tests for the read-only local model scan. No network."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import helpers  # noqa: F401
import local_scan


class ParseTests(unittest.TestCase):
    def test_hf_cache_dirname(self):
        self.assertEqual(
            local_scan.parse_hf_cache_dirname("models--TheBloke--Llama-2-7B-GGUF"),
            "TheBloke/Llama-2-7B-GGUF",
        )
        self.assertEqual(
            local_scan.parse_hf_cache_dirname("models--org--name--sub"),
            "org/name/sub",
        )
        self.assertIsNone(local_scan.parse_hf_cache_dirname("datasets--org--name"))
        self.assertIsNone(local_scan.parse_hf_cache_dirname("models--onlyone"))
        self.assertIsNone(local_scan.parse_hf_cache_dirname(""))

    def test_ollama_id(self):
        self.assertEqual(
            local_scan._ollama_id_from_parts(("registry.ollama.ai", "library", "llama3", "latest")),
            "llama3:latest",
        )
        self.assertEqual(
            local_scan._ollama_id_from_parts(("registry.ollama.ai", "nousresearch", "hermes", "v1")),
            "nousresearch/hermes:v1",
        )
        self.assertEqual(
            local_scan._ollama_id_from_parts(("hf.co", "owner", "model", "Q4")),
            "owner/model:Q4",
        )


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _hf_cache(self) -> Path:
        root = self.tmp / "hf-hub"
        snap = root / "models--TheBloke--TinyLlama-GGUF" / "snapshots" / "abc123"
        snap.mkdir(parents=True)
        gguf = snap / "tinyllama.Q4_K_M.gguf"
        gguf.write_bytes(b"x" * 2048)
        (snap / "LICENSE").write_text("MIT\n", encoding="utf-8")
        refs = root / "models--TheBloke--TinyLlama-GGUF" / "refs"
        refs.mkdir(parents=True, exist_ok=True)
        (refs / "main").write_text("abc123", encoding="utf-8")
        return root

    def _ollama(self) -> Path:
        manifests = self.tmp / "ollama" / "models" / "manifests" / "registry.ollama.ai" / "library" / "llama3"
        manifests.mkdir(parents=True)
        (manifests / "latest").write_text(
            json.dumps(
                {
                    "layers": [
                        {"mediaType": "application/vnd.ollama.image.model", "digest": "sha256:aaa", "size": 4000},
                        {"mediaType": "application/vnd.ollama.image.template", "digest": "sha256:bbb", "size": 40},
                    ]
                }
            ),
            encoding="utf-8",
        )
        return self.tmp / "ollama" / "models"

    def _lmstudio(self) -> Path:
        folder = self.tmp / "lmstudio" / "publisher" / "Cool-Model-GGUF"
        folder.mkdir(parents=True)
        (folder / "cool.Q5_K_M.gguf").write_bytes(b"g" * 1024)
        return self.tmp / "lmstudio"

    def test_scans_known_layouts(self):
        hf = self._hf_cache()
        ol = self._ollama()
        lm = self._lmstudio()
        custom = self.tmp / "extra"
        custom.mkdir()
        (custom / "voice.gguf").write_bytes(b"v" * 512)
        mlx = custom / "mlx-model"
        mlx.mkdir()
        (mlx / "config.json").write_text("{}", encoding="utf-8")
        (mlx / "model.safetensors").write_bytes(b"s" * 256)
        (mlx / "weights.npz").write_bytes(b"n" * 128)

        result = local_scan.scan_local_models(
            cfg={},
            root=self.tmp,
            extra_folders=[str(custom)],
            hf_cache_roots=[hf],
            ollama_roots=[ol],
            lmstudio_roots=[lm],
            include_defaults=False,
            include_app=False,
        )
        sources = {i["source"] for i in result["items"]}
        self.assertIn("hf_cache", sources)
        self.assertIn("ollama", sources)
        self.assertIn("lmstudio", sources)
        self.assertIn("custom", sources)
        ids = {i["id"] for i in result["items"]}
        self.assertIn("TheBloke/TinyLlama-GGUF", ids)
        self.assertIn("llama3:latest", ids)
        names = {i["name"] for i in result["items"]}
        self.assertIn("cool.Q5_K_M.gguf", names)
        self.assertIn("voice.gguf", names)
        # Sizes are reported.
        tiny = next(i for i in result["items"] if i["id"] == "TheBloke/TinyLlama-GGUF")
        self.assertGreater(tiny["size"], 2000)
        self.assertIn("tinyllama.Q4_K_M.gguf", tiny["filenames"])
        llama = next(i for i in result["items"] if i["id"] == "llama3:latest")
        self.assertEqual(llama["size"], 4040)
        self.assertFalse(result["truncated"])

    def test_annotate_by_id_and_filename(self):
        ident = {
            "ids": {"thebloke/tinyllama-gguf"},
            "filenames": {"tinyllama.q4_k_m.gguf"},
            "by_id": {"thebloke/tinyllama-gguf": {"source": "hf_cache", "path": "/cache", "name": "Tiny"}},
            "by_file": {"tinyllama.q4_k_m.gguf": {"source": "hf_cache", "path": "/cache", "name": "Tiny"}},
        }
        rows = [
            {"id": "TheBloke/TinyLlama-GGUF", "file": "other.gguf"},
            {"id": "someone/else", "file": "tinyllama.Q4_K_M.gguf", "file_path": "tinyllama.Q4_K_M.gguf"},
            {"id": "no/match", "file": "nope.gguf"},
        ]
        local_scan.annotate_already_local(rows, ident)
        self.assertTrue(rows[0]["already_local"])
        self.assertTrue(rows[1]["already_local"])
        self.assertFalse(rows[2]["already_local"])
        self.assertEqual(rows[0]["already_local_source"], "hf_cache")

    def test_identity_sets(self):
        result = {
            "items": [
                {
                    "id": "Org/Name",
                    "name": "file.Q4_K_M.gguf",
                    "filenames": ["file.Q4_K_M.gguf"],
                    "path": "/x",
                    "source": "app",
                }
            ]
        }
        ident = local_scan.identity_sets(result)
        self.assertIn("org/name", ident["ids"])
        self.assertIn("org__name", ident["ids"])
        self.assertIn("file.q4_k_m.gguf", ident["filenames"])


class PathSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_rejects_broad_and_system_paths(self):
        for bad in ("", "/", "/etc", "/proc", "/sys", "/dev"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    local_scan.validate_scan_folder(bad)

    def test_macos_realpath_etc_is_rejected(self):
        # On macOS, /etc is a symlink to /private/etc. The denylist has to
        # catch the path as typed and the folder realpath returns.
        real = os.path.realpath

        def mapped(path, *args, **kwargs):
            text = str(path).replace("\\", "/").rstrip("/")
            if text == "/etc":
                return "/private/etc"
            return real(path, *args, **kwargs)

        with patch("local_scan.os.path.realpath", side_effect=mapped) as mocked:
            with self.assertRaises(ValueError):
                local_scan.validate_scan_folder("/etc")
        self.assertTrue(mocked.called)
        with self.assertRaises(ValueError):
            local_scan.validate_scan_folder("/private/etc")

    def test_rejects_missing_and_nul(self):
        with self.assertRaises(ValueError):
            local_scan.validate_scan_folder(str(self.tmp / "does-not-exist"))
        with self.assertRaises(ValueError):
            local_scan.validate_scan_folder("hello\x00world")

    def test_accepts_real_folder(self):
        folder = self.tmp / "models"
        folder.mkdir()
        got = local_scan.validate_scan_folder(str(folder))
        self.assertEqual(got, folder.resolve())

    def test_symlink_escape_is_not_followed(self):
        # A symlink pointing outside the scan root must not be walked into.
        root = self.tmp / "scan"
        root.mkdir()
        secret = self.tmp / "secret"
        secret.mkdir()
        (secret / "hidden.gguf").write_bytes(b"shh")
        link = root / "escape"
        try:
            os.symlink(secret, link)
        except OSError:
            self.skipTest("symlinks not available")
        budget = local_scan.ScanBudget(max_depth=4, max_files=100, max_seconds=5)
        items = local_scan.scan_weight_tree([root], budget, source="custom")
        names = [i["name"] for i in items]
        self.assertNotIn("hidden.gguf", names)

    def test_depth_limit(self):
        deep = self.tmp / "a" / "b" / "c" / "d" / "e" / "f"
        deep.mkdir(parents=True)
        (deep / "deep.gguf").write_bytes(b"z")
        budget = local_scan.ScanBudget(max_depth=2, max_files=100, max_seconds=5)
        items = local_scan.scan_weight_tree([self.tmp], budget, source="custom")
        self.assertTrue(budget.truncated)
        self.assertNotIn("deep.gguf", [i["name"] for i in items])

    def test_does_not_move_or_delete(self):
        folder = self.tmp / "keep"
        folder.mkdir()
        target = folder / "stay.gguf"
        target.write_bytes(b"data")
        result = local_scan.scan_local_models(
            cfg={},
            root=self.tmp,
            extra_folders=[str(folder)],
            hf_cache_roots=[],
            ollama_roots=[],
            lmstudio_roots=[],
            include_defaults=False,
            include_app=False,
        )
        self.assertTrue(target.exists())
        self.assertEqual(target.read_bytes(), b"data")
        self.assertEqual(len(result["items"]), 1)


class FolderListTests(unittest.TestCase):
    def test_add_and_remove(self):
        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw) / "m"
            folder.mkdir()
            cfg = {"scan_folders": []}
            local_scan.add_scan_folder(cfg, str(folder))
            self.assertEqual(len(cfg["scan_folders"]), 1)
            self.assertTrue(local_scan.remove_scan_folder(cfg, str(folder)))
            self.assertEqual(cfg["scan_folders"], [])


if __name__ == "__main__":
    unittest.main()
