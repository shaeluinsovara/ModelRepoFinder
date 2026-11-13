import unittest

import helpers  # noqa: F401
import github_finder as GH


class RepoIdTests(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(GH._safe_repo_id("owner/repo"), "owner/repo")
        self.assertEqual(GH._safe_repo_id("  owner/repo  "), "owner/repo")
        self.assertEqual(GH._safe_repo_id("/owner/repo/"), "owner/repo")

    def test_rejects_traversal_and_junk(self):
        for bad in (
            "",
            "owner",
            "owner/repo/extra",
            "owner/../repo",
            "owner/repo/..",
            ".. /..",
            "owner\\repo",
            "owner/re po",
            "owner/repo?tab=readme",
            "http://github.com/owner/repo",
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    GH._safe_repo_id(bad)

    def test_folder_name(self):
        self.assertEqual(GH.folder_name("owner/repo"), "owner__repo")


if __name__ == "__main__":
    unittest.main()
