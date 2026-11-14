"""v1.7 My Library: one list of models and projects, and the spreadsheet export."""
from __future__ import annotations

import csv
import io
import json
import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import helpers  # noqa: F401
import library as LB


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.state = self.tmp / ".mrf_library.json"
        self.models_dir = self.tmp / "models"
        self.models_dir.mkdir()
        (self.models_dir / "a.gguf").write_bytes(b"x")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def rows(self):
        app_path = str(self.models_dir / "a.gguf")
        return LB.build_rows(
            local_models={"items": [
                {"source": "app", "path": app_path, "id": "dup"},
                {"source": "ollama", "path": str(self.tmp / "ollama" / "llama3"), "id": "llama3:8b",
                 "size_gb": 4.7, "format": "gguf"},
                {"source": "hf_cache", "path": app_path, "id": "same/path"},  # same file as an app row
            ]},
            local_repos={"items": [
                {"id": "ggml-org/llama.cpp", "name": "llama.cpp", "is_github": True, "is_git": True,
                 "url": "https://github.com/ggml-org/llama.cpp", "path": str(self.tmp / "code" / "llama.cpp"),
                 "source": "git", "size_gb": 0.2},
                {"id": "", "name": "=HYPERLINK(\"http://x\")", "path": str(self.tmp / "code" / "evil-main"),
                 "source": "zip_folder", "size_gb": 0.0},
            ]},
            app_models=[{"machine": "this_computer", "tag": "coding", "name": "org__Model-GGUF",
                         "relative_path": "org__Model-GGUF", "path": app_path, "size_gb": 4.4}],
            app_repos=[{"name": "ollama__ollama", "category": "local_ai", "path": str(self.tmp / "gh" / "ollama__ollama"),
                        "is_git": True, "size_gb": 0.5}],
            tracked=[{"machine": "this_computer", "tag": "coding", "relative_path": "org__Model-GGUF",
                      "id": "org/Model-GGUF", "downloaded_at": "2026-11-01T10:00:00Z", "format": "gguf"}],
            hf_names={"coding": "Coding"},
            gh_names={"local_ai": "Local AI tools"},
            machine_names={"this_computer": "This computer (detected)"},
            state_path=self.state,
        )

    def test_rows_are_combined_and_deduped(self):
        rows = self.rows()
        self.assertEqual(len(rows), 5)
        app = [r for r in rows if r["origin"] == "app"]
        found = [r for r in rows if r["origin"] == "found"]
        self.assertEqual(len(app), 2)
        self.assertEqual(len(found), 3)
        model = next(r for r in rows if r["kind"] == "model" and r["origin"] == "app")
        self.assertTrue(model["auto_managed"])
        self.assertEqual(model["ident"], "org/Model-GGUF")
        self.assertEqual(model["category"], "Coding")
        self.assertEqual(model["computer"], "This computer (detected)")
        self.assertEqual(model["date"], "2026-11-01T10:00:00Z")
        repo = next(r for r in rows if r["kind"] == "repo" and r["origin"] == "app")
        self.assertEqual(repo["ident"], "https://github.com/ollama/ollama")
        self.assertEqual(repo["name"], "ollama")
        for r in rows:
            self.assertIn("type_label", r)
            self.assertIn("source_label", r)
            self.assertTrue(r["date"])  # first-seen date filled in

    def test_first_seen_is_remembered(self):
        first = {r["location"]: r["date"] for r in self.rows()}
        saved = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertTrue(saved)
        again = {r["location"]: r["date"] for r in self.rows()}
        self.assertEqual(first, again)

    def test_filters(self):
        rows = self.rows()
        self.assertEqual(len(LB.filter_rows(rows, kind="model")), 2)
        self.assertEqual(len(LB.filter_rows(rows, kind="repo", origin="found")), 2)
        self.assertEqual(len(LB.filter_rows(rows, text="LLAMA")), 3)  # llama3, llama.cpp, ollama
        self.assertEqual(len(LB.filter_rows(rows, text="llama.cpp")), 1)
        self.assertEqual(len(LB.filter_rows(rows, kind="bogus")), 5)

    def test_safe_cell(self):
        for raw in ("=1+1", "+cmd", "-2", "@SUM(A1)", "\tx", "\rx"):
            with self.subTest(raw=raw):
                self.assertTrue(LB.safe_cell(raw).startswith("'"))
        self.assertEqual(LB.safe_cell("plain"), "plain")
        self.assertEqual(LB.safe_cell(None), "")
        self.assertEqual(LB.safe_cell(4.5), "4.5")

    def test_csv_bytes(self):
        blob = LB.to_csv_bytes(self.rows())
        self.assertTrue(blob.startswith(b"\xef\xbb\xbf"))  # UTF-8 BOM for Excel
        self.assertIn(b"\r\n", blob)
        text = blob.decode("utf-8-sig")
        table = list(csv.reader(io.StringIO(text)))
        self.assertEqual(table[0], [label for _, label in LB.CSV_COLUMNS])
        self.assertEqual(len(table), 6)
        cells = [c for row in table[1:] for c in row]
        self.assertFalse(any(c.startswith(("=", "+", "@")) for c in cells))
        self.assertIn("'=HYPERLINK(\"http://x\")", cells)

    def test_export_filename_and_summary(self):
        self.assertEqual(LB.export_filename(datetime(2026, 11, 13)), "ModelRepoFinder-library-2026-11-13.csv")
        s = LB.summary(self.rows())
        self.assertEqual((s["total"], s["models"], s["repos"], s["from_app"], s["found"]), (5, 2, 3, 2, 3))


if __name__ == "__main__":
    unittest.main()
