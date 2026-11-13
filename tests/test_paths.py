import unittest
from pathlib import Path

import helpers
import watcher as W


class PathSafetyTests(unittest.TestCase):
    def setUp(self):
        self.cfg = helpers.sample_config()

    def test_safe_component_accepts_plain_names(self):
        self.assertTrue(W.is_safe_path_component("coding"))
        self.assertTrue(W.is_safe_path_component("manual"))
        self.assertTrue(W.is_safe_path_component("mac_16gb"))

    def test_rejects_slashes_and_dots(self):
        for bad in (
            "../etc",
            "foo/bar",
            "foo\\bar",
            "/etc",
            "..",
            ".",
            "",
            "C:\\windows",
            "C:windows",
            "foo/../bar",
            "coding/../../secret",
        ):
            with self.subTest(bad=bad):
                self.assertFalse(W.is_safe_path_component(bad), bad)

    def test_validate_category_accepts_known_and_special(self):
        self.assertEqual(W.validate_category_key(self.cfg, "coding", scope="hf"), "coding")
        self.assertEqual(
            W.validate_category_key(self.cfg, "manual", scope="hf", extra=W.SPECIAL_HF_CATEGORIES),
            "manual",
        )
        self.assertEqual(
            W.validate_category_key(self.cfg, "runners", scope="github"),
            "runners",
        )

    def test_validate_category_rejects_unknown(self):
        with self.assertRaises(ValueError) as ctx:
            W.validate_category_key(self.cfg, "not_a_real_category", scope="hf")
        self.assertIn("Unknown", str(ctx.exception))

    def test_validate_category_rejects_traversal_even_if_unknown_check_comes_later(self):
        with self.assertRaises(ValueError):
            W.validate_category_key(self.cfg, "../coding", scope="hf")
        with self.assertRaises(ValueError):
            W.validate_category_key(self.cfg, "..", scope="hf")
        with self.assertRaises(ValueError):
            W.validate_category_key(self.cfg, "/coding", scope="hf")

    def test_validate_machine(self):
        self.assertEqual(W.validate_machine_key(self.cfg, "mac_16gb"), "mac_16gb")
        with self.assertRaises(ValueError):
            W.validate_machine_key(self.cfg, "../mac_16gb")
        with self.assertRaises(ValueError):
            W.validate_machine_key(self.cfg, "no_such_machine")

    def test_incoming_dir_stays_under_root(self, tmp=None):
        import tempfile

        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            cfg = dict(self.cfg)
            cfg["paths"] = {"base": str(tmp), "incoming": "incoming", "retired": "retired"}
            dest = W.incoming_dir(cfg, "mac_16gb", "coding", tmp)
            self.assertTrue(str(dest.resolve()).startswith(str((tmp / "incoming").resolve())))
            W.assert_contained(dest, W.incoming_root(cfg, tmp))

    def test_incoming_dir_rejects_traversal_tag(self):
        import tempfile

        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            cfg = dict(self.cfg)
            cfg["paths"] = {"base": str(tmp), "incoming": "incoming", "retired": "retired"}
            with self.assertRaises(ValueError):
                W.incoming_dir(cfg, "mac_16gb", "../etc", tmp)
            with self.assertRaises(ValueError):
                W.incoming_dir(cfg, "../mac", "coding", tmp)
            with self.assertRaises(ValueError):
                W.github_incoming_dir(cfg, "../../etc", tmp)

    def test_assert_contained_catches_escape(self):
        import tempfile

        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            root = tmp / "incoming"
            root.mkdir()
            with self.assertRaises(ValueError):
                W.assert_contained(tmp / "incoming" / ".." / "secret", root)
            inside = root / "mac_16gb" / "coding"
            inside.mkdir(parents=True)
            self.assertEqual(W.assert_contained(inside, root), inside.resolve())


if __name__ == "__main__":
    unittest.main()
