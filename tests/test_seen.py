"""Tests for the already-shown suggestion list. No network."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import helpers  # noqa: F401 — puts the repo on sys.path
import seen_store


class PickFreshTests(unittest.TestCase):
    def test_skips_seen_and_excluded(self):
        cands = [{"id": f"m{i}"} for i in range(8)]
        picked, meta = seen_store.pick_fresh(
            cands,
            id_of=lambda r: r["id"],
            seen=["m0", "m1"],
            exclude=["m2"],
            keep=2,
        )
        self.assertEqual([p["id"] for p in picked], ["m3", "m4"])
        self.assertFalse(meta["wrapped"])
        self.assertEqual(meta["skipped_seen"], 2)
        self.assertEqual(meta["skipped_already"], 1)

    def test_wraps_when_everything_was_shown(self):
        cands = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
        picked, meta = seen_store.pick_fresh(
            cands,
            id_of=lambda r: r["id"],
            seen=["a", "b", "c"],
            exclude=[],
            keep=2,
        )
        self.assertEqual([p["id"] for p in picked], ["a", "b"])
        self.assertTrue(meta["wrapped"])

    def test_exclude_wins_over_wrap(self):
        cands = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
        picked, meta = seen_store.pick_fresh(
            cands,
            id_of=lambda r: r["id"],
            seen=["a", "c"],
            exclude=["a"],
            keep=2,
        )
        # a is excluded, so wrap can only offer b (unseen) then c (seen)
        self.assertEqual([p["id"] for p in picked], ["b", "c"])
        self.assertTrue(meta["wrapped"])
        self.assertNotIn("a", [p["id"] for p in picked])

    def test_empty_and_zero_keep(self):
        picked, meta = seen_store.pick_fresh(
            [], id_of=lambda r: r["id"], seen=[], exclude=[], keep=3
        )
        self.assertEqual(picked, [])
        self.assertFalse(meta["wrapped"])
        picked, meta = seen_store.pick_fresh(
            [{"id": "a"}], id_of=lambda r: r["id"], seen=[], exclude=[], keep=0
        )
        self.assertEqual(picked, [])


class SeenFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / ".mrf_seen.json"
        seen_store.set_path(self.path)

    def tearDown(self):
        seen_store.set_path(None)
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_add_get_and_reset(self):
        seen_store.add_seen("hf", "mac_16gb", "coding", ["org/one", "org/two"])
        self.assertEqual(seen_store.get_seen("hf", "mac_16gb", "coding"), ["org/one", "org/two"])
        # Duplicates are ignored, order is kept.
        seen_store.add_seen("hf", "mac_16gb", "coding", ["org/two", "org/three"])
        self.assertEqual(
            seen_store.get_seen("hf", "mac_16gb", "coding"),
            ["org/one", "org/two", "org/three"],
        )
        # Other buckets are independent.
        self.assertEqual(seen_store.get_seen("hf", "mac_16gb", "vision"), [])
        seen_store.reset(scope="hf", machine="mac_16gb", category="coding")
        self.assertEqual(seen_store.get_seen("hf", "mac_16gb", "coding"), [])

    def test_record_batch_wrap_replaces(self):
        seen_store.add_seen("github", "pc_32gb_gpu", "runners", ["a/b", "c/d"])
        seen_store.record_batch("github", "pc_32gb_gpu", "runners", ["e/f"], wrapped=True)
        self.assertEqual(seen_store.get_seen("github", "pc_32gb_gpu", "runners"), ["e/f"])

    def test_rejects_path_tricks(self):
        with self.assertRaises(ValueError):
            seen_store.add_seen("hf", "../etc", "coding", ["x"])
        with self.assertRaises(ValueError):
            seen_store.add_seen("hf", "mac_16gb", "../coding", ["x"])
        with self.assertRaises(ValueError):
            seen_store.reset(scope="hf", machine="foo/bar")
        with self.assertRaises(ValueError):
            seen_store.reset(scope="not-a-scope")

    def test_corrupt_file_is_empty_not_crash(self):
        self.path.write_text("{{{not json", encoding="utf-8")
        self.assertEqual(seen_store.get_seen("hf", "mac_16gb", "coding"), [])

    def test_caps_growth(self):
        ids = [f"org/m{i}" for i in range(seen_store.MAX_IDS_PER_BUCKET + 50)]
        result = seen_store.add_seen("hf", "mac_16gb", "coding", ids)
        self.assertLessEqual(len(result), seen_store.MAX_IDS_PER_BUCKET)
        self.assertEqual(result[-1], ids[-1])


if __name__ == "__main__":
    unittest.main()
