import tempfile
import unittest
from pathlib import Path

import helpers
import watcher as W


class RetireRestoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = helpers.sample_config()
        self.cfg["paths"] = {
            "base": str(self.tmp),
            "incoming": "incoming",
            "retired": "retired",
        }
        self.incoming = W.incoming_dir(self.cfg, "mac_16gb", "coding", self.tmp)
        self.incoming.mkdir(parents=True)
        self.src = self.incoming / "tiny-model.gguf"
        self.src.write_bytes(b"fake-weights")

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_retire_moves_and_never_deletes(self):
        dest = W.retire_item(self.cfg, "mac_16gb", "coding", "tiny-model.gguf", self.tmp)
        self.assertTrue(dest.exists())
        self.assertFalse(self.src.exists())
        self.assertEqual(dest.read_bytes(), b"fake-weights")
        self.assertIn("retired", dest.parts)
        # A "restore" is just moving it back — the bytes are still there.
        restored = self.incoming / dest.name
        restored.parent.mkdir(parents=True, exist_ok=True)
        dest.replace(restored)
        self.assertTrue(restored.exists())
        self.assertEqual(restored.read_bytes(), b"fake-weights")

    def test_retire_rejects_traversal(self):
        # A file outside incoming must not be moveable through a crafted relative path.
        outside = self.tmp / "secret.txt"
        outside.write_text("do-not-touch")
        with self.assertRaises(ValueError):
            W.retire_item(self.cfg, "mac_16gb", "coding", "../secret.txt", self.tmp)
        with self.assertRaises(ValueError):
            W.retire_item(self.cfg, "mac_16gb", "coding", "../../secret.txt", self.tmp)
        self.assertTrue(outside.exists())
        self.assertEqual(outside.read_text(), "do-not-touch")

    def test_retire_rejects_absolute(self):
        with self.assertRaises(ValueError):
            W.retire_item(self.cfg, "mac_16gb", "coding", str(self.src.resolve()), self.tmp)

    def test_missing_file(self):
        with self.assertRaises(FileNotFoundError):
            W.retire_item(self.cfg, "mac_16gb", "coding", "no-such.gguf", self.tmp)

    def test_github_retire(self):
        dest_dir = W.github_incoming_dir(self.cfg, "runners", self.tmp)
        dest_dir.mkdir(parents=True)
        folder = dest_dir / "owner__repo"
        folder.mkdir()
        (folder / "README.md").write_text("hello")
        retired = W.retire_github_item(self.cfg, "runners", "owner__repo", self.tmp)
        self.assertTrue(retired.exists())
        self.assertFalse(folder.exists())
        self.assertTrue((retired / "README.md").exists())
        with self.assertRaises(ValueError):
            W.retire_github_item(self.cfg, "runners", "../runners/owner__repo", self.tmp)

    def test_collision_gets_a_stamp(self):
        first = W.retire_item(self.cfg, "mac_16gb", "coding", "tiny-model.gguf", self.tmp)
        # Put another file of the same name in incoming and retire again.
        self.src.write_bytes(b"second")
        second = W.retire_item(self.cfg, "mac_16gb", "coding", "tiny-model.gguf", self.tmp)
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())
        self.assertNotEqual(first, second)
        self.assertIn("retired-", second.name)
        self.assertEqual(first.read_bytes(), b"fake-weights")
        self.assertEqual(second.read_bytes(), b"second")


if __name__ == "__main__":
    unittest.main()
