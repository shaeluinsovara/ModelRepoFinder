import unittest

import helpers  # noqa: F401
import watcher as W


class FitWordingTests(unittest.TestCase):
    def test_estimate_is_larger_than_stored_and_rounded(self):
        est = W.estimate_runtime_memory_gb(4.0, "gguf")
        self.assertGreater(est, 4.0)
        self.assertEqual(est, round(est, 1))
        other = W.estimate_runtime_memory_gb(4.0, "safetensors")
        self.assertGreaterEqual(other, est)

    def test_under_cap_has_no_warning(self):
        fit = W.describe_fit(4.0, 10.0, fmt="gguf")
        self.assertFalse(fit["over_cap"])
        self.assertEqual(fit["warning"], "")
        self.assertEqual(fit["estimate_label"], "rough estimate")
        self.assertEqual(fit["stored_size_gb"], 4.0)
        self.assertGreater(fit["estimated_ram_gb"], 0)

    def test_over_cap_warns_and_distinguishes_disk_from_memory(self):
        fit = W.describe_fit(20.0, 10.0, fmt="gguf")
        self.assertTrue(fit["over_cap"])
        self.assertIn("On disk", fit["warning"])
        self.assertIn("Estimated memory to run", fit["warning"])
        self.assertIn("rough estimate", fit["warning"])
        self.assertIn("may not run well", fit["warning"])
        # The cap itself is described as a size cap, not as a hard RAM figure.
        self.assertIn("size cap", fit["warning"])

    def test_whole_over_but_pick_fits(self):
        fit = W.describe_fit(
            6.0,
            10.0,
            whole_gb=30.0,
            pick_fits=True,
            fmt="gguf",
        )
        self.assertFalse(fit["over_cap"])
        self.assertTrue(fit["whole_over_cap"])
        self.assertIn("whole folder", fit["warning"])
        self.assertIn("should fit", fit["warning"])
        self.assertIn("rough estimate", fit["warning"])

    def test_no_cap_no_warning(self):
        fit = W.describe_fit(20.0, None, fmt="gguf")
        self.assertFalse(fit["over_cap"])
        self.assertEqual(fit["warning"], "")


if __name__ == "__main__":
    unittest.main()
