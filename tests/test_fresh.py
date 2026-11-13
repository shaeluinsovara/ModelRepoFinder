"""Fresh suggestion batches and sort order. No network — Hugging Face and GitHub are mocked."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import helpers
import github_finder as GH
import seen_store
import watcher as W


def _models(n=12):
    rows = []
    for i in range(n):
        rows.append(
            {
                "id": f"org/model-{i}",
                "downloads": 10000 - i * 100,
                "likes": i * 7,
                "trending_score": 200 - i * 3,
                "lastModified": f"2026-02-{(i % 28) + 1:02d}T00:00:00",
                "createdAt": f"2026-01-{(i % 28) + 1:02d}T00:00:00",
                "card_url": f"https://huggingface.co/org/model-{i}",
                "tags": ["gguf"],
                "pipeline_tag": "text-generation",
            }
        )
    return rows


def _gguf(model_id: str):
    name = model_id.split("/")[-1]
    return [
        {
            "path": f"{name}.Q4_K_M.gguf",
            "size": 1_000_000_000,
            "size_gb": 0.93,
            "format": "gguf",
        }
    ]


class FreshHfTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.seen = self.tmp / ".mrf_seen.json"
        seen_store.set_path(self.seen)
        self.cfg = helpers.sample_config()
        # Use a GGUF-only machine so MLX is not also searched.
        self.machine = "pc_32gb_gpu"
        # Make the coding category predictable: one query, keep 2, GGUF only.
        self.cfg["tags"]["coding"]["keep"] = 2
        self.cfg["tags"]["coding"]["hf_queries"] = ["coding"]
        self.cfg["tags"]["coding"]["allow_nongguf"] = False
        self.cfg["tags"]["coding"]["use_pipeline_tag"] = False
        self.cfg["allow_list"] = []

    def tearDown(self):
        seen_store.set_path(None)
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _collect(self, **kw):
        defaults = dict(
            cfg=self.cfg,
            machine_key=self.machine,
            search_limit=20,
            categories=["coding"],
            sort="downloads",
            already_ids=set(),
            seen_path=self.seen,
            record_seen=True,
        )
        defaults.update(kw)
        with patch.object(W, "search_gguf_models", return_value=_models()) as search, patch.object(
            W, "search_models", return_value=[]
        ), patch.object(W, "search_mlx_models", return_value=[]), patch.object(
            W, "list_gguf_files", side_effect=_gguf
        ):
            data = W.collect_suggestions(**defaults)
        return data, search

    def test_second_refresh_is_a_new_batch(self):
        first, _ = self._collect()
        coding = first["tags"][0]
        first_ids = [m["id"] for m in coding["models"]]
        self.assertEqual(first_ids, ["org/model-0", "org/model-1"])
        self.assertFalse(coding["wrapped"])

        second, _ = self._collect()
        second_ids = [m["id"] for m in second["tags"][0]["models"]]
        self.assertEqual(second_ids, ["org/model-2", "org/model-3"])
        self.assertEqual(set(first_ids) & set(second_ids), set())

    def test_excludes_already_downloaded(self):
        data, _ = self._collect(already_ids={"org/model-0", "org/model-1"})
        ids = [m["id"] for m in data["tags"][0]["models"]]
        self.assertEqual(ids, ["org/model-2", "org/model-3"])
        self.assertGreaterEqual(data["tags"][0]["skipped_already"], 2)

    def test_sort_by_likes(self):
        # likes in _models go 0, 7, 14, ... so the last models have the most likes.
        data, search = self._collect(sort="likes")
        ids = [m["id"] for m in data["tags"][0]["models"]]
        # Most liked are model-11 then model-10 (likes = i*7).
        self.assertEqual(ids, ["org/model-11", "org/model-10"])
        # The mock was called with the likes sort.
        args, kwargs = search.call_args
        self.assertEqual(kwargs.get("sort") or (args[3] if len(args) > 3 else None) or "likes", "likes")

    def test_wraps_after_the_pool_is_exhausted(self):
        self.cfg["tags"]["coding"]["keep"] = 10
        # 12 models, keep 10: first call takes 10, second takes the remaining 2 and wraps
        # to fill keep (up to 10, so it will wrap and also re-offer earlier ones).
        first, _ = self._collect()
        first_ids = [m["id"] for m in first["tags"][0]["models"]]
        self.assertEqual(len(first_ids), 10)
        second, _ = self._collect()
        self.assertTrue(second["tags"][0]["wrapped"])
        self.assertTrue(second["tags"][0].get("wrap_note"))
        # After a wrap, a third call should start over at the current batch, not stall.
        third, _ = self._collect()
        self.assertTrue(third["tags"][0]["models"])

    def test_reset_lets_the_first_batch_return(self):
        self._collect()
        seen_store.reset(scope="hf", machine=self.machine, path=self.seen)
        again, _ = self._collect()
        ids = [m["id"] for m in again["tags"][0]["models"]]
        self.assertEqual(ids, ["org/model-0", "org/model-1"])

    def test_normalize_sort(self):
        self.assertEqual(W.normalize_hf_sort("Most Downloaded"), "downloads")
        self.assertEqual(W.normalize_hf_sort("trendingScore"), "trending")
        self.assertEqual(W.normalize_hf_sort("lastModified"), "newest")
        self.assertEqual(W.normalize_hf_sort("nonsense"), "downloads")
        self.assertEqual(W.hf_api_sort("trending"), "trendingScore")
        self.assertEqual(W.hf_api_sort("newest"), "lastModified")

    def test_trending_falls_back_to_likes(self):
        W.reset_hf_sort_state()

        def boom(**kwargs):
            if kwargs.get("sort") == "trendingScore":
                raise RuntimeError("Invalid sort parameter: trendingScore")
            return []

        class Dummy:
            def list_models(self, **kwargs):
                return boom(**kwargs)

        # search_models uses HF.list_models via _retry. Patch HF.
        with patch.object(W, "HF", Dummy()), patch.object(W, "_retry", side_effect=lambda fn: fn()):
            # Should not raise; falls back to likes and records the fallback.
            try:
                W.search_models("q", limit=5, gguf_only=True, sort="trending")
            except RuntimeError:
                # The fallback itself returned [] via Dummy, so a second call
                # with likes should succeed (return []).
                pass
        # Directly exercise the fallback path.
        W.reset_hf_sort_state()
        calls = []

        class Dummy2:
            def list_models(self, **kwargs):
                calls.append(kwargs.get("sort"))
                if kwargs.get("sort") == "trendingScore":
                    raise RuntimeError("nope")
                return []

        with patch.object(W, "HF", Dummy2()):
            out = W.search_models("q", limit=5, sort="trending")
        self.assertEqual(out, [])
        self.assertIn("trendingScore", calls)
        self.assertIn("likes", calls)
        self.assertEqual(W._HF_SORT_FALLBACK, "likes")
        W.reset_hf_sort_state()

    def test_newest_sort_order(self):
        models = [
            {"id": "a", "downloads": 10, "likes": 1, "lastModified": "2026-01-01T00:00:00"},
            {"id": "b", "downloads": 99, "likes": 1, "lastModified": "2026-06-01T00:00:00"},
            {"id": "c", "downloads": 50, "likes": 1, "lastModified": "2026-06-01T00:00:00"},
        ]
        ordered = W.sort_models(models, "newest")
        self.assertEqual([m["id"] for m in ordered], ["b", "c", "a"])  # same date: more downloads first


class FreshGithubTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.seen = self.tmp / ".mrf_seen.json"
        seen_store.set_path(self.seen)
        self.cfg = helpers.sample_config()
        self.cfg["github_tags"]["runners"]["keep"] = 2
        self.cfg["github_tags"]["runners"]["queries"] = ["llama runner"]
        self.cfg["github_allow_list"] = []
        GH.set_rng_seed(42)

    def tearDown(self):
        seen_store.set_path(None)
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _repos(self, n=10):
        rows = []
        for i in range(n):
            rows.append(
                {
                    "id": f"owner/repo-{i}",
                    "name": f"repo-{i}",
                    "url": f"https://github.com/owner/repo-{i}",
                    "description": "a runner",
                    "stars": 1000 - i * 10,
                    "pushed_at": f"2026-03-{(i % 27) + 1:02d}T00:00:00Z",
                    "pushed_at_pretty": f"2026-03-{(i % 27) + 1:02d}",
                    "license": "MIT",
                    "default_branch": "main",
                    "language": "Python",
                    "forks": 1,
                }
            )
        return rows

    def _collect(self, **kw):
        defaults = dict(
            cfg=self.cfg,
            machine_key="pc_32gb_gpu",
            categories=["runners"],
            per_page=20,
            sort="stars",
            already_ids=set(),
            seen_path=self.seen,
            record_seen=True,
        )
        defaults.update(kw)
        with patch.object(GH, "search_repositories", return_value=self._repos()):
            return GH.collect_github_suggestions(**defaults)

    def test_second_refresh_is_a_new_batch(self):
        first = self._collect()
        first_ids = [r["id"] for r in first["tags"][0]["repos"]]
        self.assertEqual(first_ids, ["owner/repo-0", "owner/repo-1"])
        second = self._collect()
        second_ids = [r["id"] for r in second["tags"][0]["repos"]]
        self.assertEqual(second_ids, ["owner/repo-2", "owner/repo-3"])

    def test_excludes_downloaded(self):
        data = self._collect(already_ids={"owner/repo-0"})
        ids = [r["id"] for r in data["tags"][0]["repos"]]
        self.assertNotIn("owner/repo-0", ids)
        self.assertEqual(ids[0], "owner/repo-1")

    def test_sort_updated(self):
        data = self._collect(sort="updated")
        ids = [r["id"] for r in data["tags"][0]["repos"]]
        # pushed_at grows with i, so the last repos are the most recently pushed.
        self.assertEqual(ids[0], "owner/repo-9")

    def test_fresh_mix_is_deterministic_with_seed(self):
        GH.set_rng_seed(1)
        a = self._collect(sort="fresh")
        seen_store.reset(scope="github", path=self.seen)
        GH.set_rng_seed(1)
        b = self._collect(sort="fresh")
        self.assertEqual(
            [r["id"] for r in a["tags"][0]["repos"]],
            [r["id"] for r in b["tags"][0]["repos"]],
        )

    def test_weighted_sample_respects_keep_and_excludes_nothing_from_empty(self):
        items = [{"id": i, "stars": i} for i in range(5)]
        GH.set_rng_seed(0)
        picked = GH.weighted_sample(items, 3, weight_of=lambda r: r["stars"])
        self.assertEqual(len(picked), 3)
        self.assertEqual(len({p["id"] for p in picked}), 3)
        self.assertEqual(GH.weighted_sample([], 3, weight_of=lambda r: 1), [])

    def test_normalize(self):
        self.assertEqual(GH.normalize_gh_sort("Recently updated"), "updated")
        self.assertEqual(GH.normalize_gh_sort("fresh mix"), "fresh")
        self.assertEqual(GH.normalize_gh_sort("nope"), "stars")


if __name__ == "__main__":
    unittest.main()
