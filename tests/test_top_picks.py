"""Top picks view. No network — Hugging Face is mocked."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import helpers
import watcher as W


def _popular(n=15, prefix="pop"):
    rows = []
    for i in range(n):
        rows.append(
            {
                "id": f"community/{prefix}-{i}",
                "downloads": 50000 - i * 200,
                "likes": 800 - i,
                "trending_score": 90 - i,
                "lastModified": f"2026-05-{(i % 27) + 1:02d}T00:00:00",
                "createdAt": "",
                "card_url": f"https://huggingface.co/community/{prefix}-{i}",
                "tags": ["gguf"],
                "pipeline_tag": "text-generation",
            }
        )
    return rows


def _gguf(model_id: str):
    # Make the first two popular models too big to fit a 10 GB cap, so the
    # collector has to walk further down the list.
    idx = int(model_id.rsplit("-", 1)[-1]) if model_id[-1].isdigit() else 0
    gb = 40.0 if idx < 2 and "pop" in model_id else 2.0
    return [{"path": f"{model_id.split('/')[-1]}.Q4_K_M.gguf", "size": int(gb * 1024**3), "size_gb": gb, "format": "gguf"}]


class TopPicksTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = helpers.sample_config()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_overall_fits_the_machine_and_skips_too_big(self):
        # mac_16gb cap is 10 GB, so the first two "pop" models (40 GB) are dropped.
        with patch.object(W, "search_models", return_value=_popular()) as search, patch.object(
            W, "search_mlx_models", return_value=[]
        ), patch.object(W, "search_gguf_models", return_value=[]), patch.object(
            W, "list_gguf_files", side_effect=_gguf
        ):
            data = W.collect_top_picks(
                self.cfg,
                "mac_16gb",
                sort="downloads",
                categories=[],
                search_limit=20,
                keep=4,
            )
        ids = [m["id"] for m in data["overall"]]
        self.assertNotIn("community/pop-0", ids)
        self.assertNotIn("community/pop-1", ids)
        self.assertEqual(ids[0], "community/pop-2")
        self.assertEqual(len(ids), 4)
        self.assertIn("community signal", data["disclaimer"].lower())
        self.assertEqual(data["mode"], "top_picks")
        self.assertEqual(data["sort"], "downloads")
        # No-keyword call: empty query, gguf filter, requested sort.
        kwargs = search.call_args.kwargs
        self.assertIn(kwargs.get("sort"), {"downloads", None})
        # First positional or keyword query should be empty for overall.
        if search.call_args.args:
            self.assertEqual(search.call_args.args[0], "")

    def test_does_not_hide_already_local_models(self):
        # Top picks should still list a popular model that is already on disk,
        # so it can be marked "Already on this computer" in the interface.
        with patch.object(W, "search_models", return_value=_popular(8)), patch.object(
            W, "search_mlx_models", return_value=[]
        ), patch.object(W, "list_gguf_files", side_effect=_gguf):
            data = W.collect_top_picks(
                self.cfg,
                "pc_32gb_gpu",
                sort="downloads",
                categories=[],
                keep=5,
                already_ids={"community/pop-2"},
            )
        ids = [m["id"] for m in data["overall"]]
        # already_ids is accepted but must not hide the model from top picks.
        self.assertIn("community/pop-2", ids)

    def test_per_category_section(self):
        def search(query="", limit=50, pipeline_tag=None, gguf_only=False, author=None, sort="downloads", library=None):
            if query:
                return _popular(6, prefix="coding")
            return _popular(6, prefix="pop")

        with patch.object(W, "search_models", side_effect=search), patch.object(
            W, "search_gguf_models", side_effect=lambda q, limit=50, pipeline_tag=None, sort="downloads": _popular(6, prefix="coding")
        ), patch.object(W, "search_mlx_models", return_value=[]), patch.object(
            W, "list_gguf_files", side_effect=_gguf
        ):
            data = W.collect_top_picks(
                self.cfg,
                "pc_32gb_gpu",
                sort="likes",
                categories=["coding"],
                keep=3,
            )
        self.assertTrue(data["overall"])
        self.assertEqual(data["tags"][0]["name"], "coding")
        self.assertTrue(data["tags"][0]["models"])

    def test_disclaimer_is_plain_language(self):
        self.assertIn("not a guarantee of quality", W.POPULARITY_DISCLAIMER.lower())
        self.assertIn("community", W.POPULARITY_DISCLAIMER.lower())


if __name__ == "__main__":
    unittest.main()
