import os
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path

import helpers  # noqa: F401
import github_finder as GH


def _write_zip(path: Path, members: dict, *, symlinks=None):
    """members: name -> bytes/str. symlinks: name -> target string."""
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in members.items():
            if isinstance(content, str):
                content = content.encode("utf-8")
            zf.writestr(name, content)
        for name, target in (symlinks or {}).items():
            info = zipfile.ZipInfo(name)
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            zf.writestr(info, target)


class ZipExtractionSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_safe_github_style_zip(self):
        zpath = self.tmp / "good.zip"
        _write_zip(
            zpath,
            {
                "repo-main/README.md": "# hello",
                "repo-main/src/foo.py": "print(1)\n",
                "repo-main/LICENSE": "MIT",
            },
        )
        staging = self.tmp / "staging"
        GH.extract_zip_safely(zpath, staging)
        self.assertTrue((staging / "repo-main" / "README.md").is_file())
        self.assertEqual((staging / "repo-main" / "LICENSE").read_text(), "MIT")
        self.assertTrue((staging / "repo-main" / "src" / "foo.py").is_file())
        # And the higher-level unwrap:
        target = self.tmp / "owner__repo"
        GH._extract_zip(zpath, target)
        self.assertTrue((target / "README.md").is_file())
        self.assertTrue((target / "LICENSE").is_file())
        self.assertFalse(target.name.endswith(".staging") and (self.tmp / (target.name + ".staging")).exists())

    def test_rejects_dotdot(self):
        zpath = self.tmp / "slip.zip"
        _write_zip(zpath, {"../../outside.txt": "nope", "ok.txt": "fine"})
        staging = self.tmp / "staging"
        staging.mkdir()
        with self.assertRaises(ValueError) as ctx:
            GH.extract_zip_safely(zpath, staging)
        self.assertIn("unsafe", str(ctx.exception).lower())
        self.assertFalse((self.tmp / "outside.txt").exists())
        self.assertFalse((staging / "outside.txt").exists())

    def test_rejects_absolute_posix(self):
        zpath = self.tmp / "abs.zip"
        _write_zip(zpath, {"/tmp/evil-mrf-abs.txt": "nope"})
        staging = self.tmp / "staging"
        staging.mkdir()
        with self.assertRaises(ValueError):
            GH.extract_zip_safely(zpath, staging)
        self.assertFalse(Path("/tmp/evil-mrf-abs.txt").exists())

    def test_rejects_windows_drive(self):
        zpath = self.tmp / "win.zip"
        _write_zip(zpath, {r"C:/Windows/evil-mrf.txt": "nope"})
        staging = self.tmp / "staging"
        staging.mkdir()
        with self.assertRaises(ValueError):
            GH.extract_zip_safely(zpath, staging)

    def test_rejects_escaping_symlink(self):
        zpath = self.tmp / "link.zip"
        _write_zip(zpath, {"repo-main/README.md": "ok"}, symlinks={"repo-main/link": "../../outside"})
        staging = self.tmp / "staging"
        staging.mkdir()
        with self.assertRaises(ValueError) as ctx:
            GH.extract_zip_safely(zpath, staging)
        self.assertIn("symbolic link", str(ctx.exception).lower())
        # The escape target must not have been created.
        self.assertFalse((self.tmp / "outside").exists())

    def test_allows_internal_symlink(self):
        zpath = self.tmp / "intlink.zip"
        _write_zip(
            zpath,
            {"repo-main/README.md": "ok", "repo-main/data.txt": "payload"},
            symlinks={"repo-main/link": "data.txt"},
        )
        staging = self.tmp / "staging"
        try:
            GH.extract_zip_safely(zpath, staging)
        except OSError:
            # Windows without symlink privilege: skip the assertion about the link.
            return
        link = staging / "repo-main" / "link"
        if link.exists() or link.is_symlink():
            if link.is_symlink():
                self.assertTrue(GH._symlink_target_is_safe(link, "data.txt", staging))
                self.assertEqual(os.readlink(link), "data.txt")

    def test_validated_zip_dest_rejects_bad_names(self):
        staging = self.tmp / "staging"
        staging.mkdir()
        for name in ("../x", "/etc/passwd", r"C:\Windows\x", "..\\x", ""):
            info = zipfile.ZipInfo(name or "x")
            info.filename = name
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    GH.validated_zip_dest(info, staging)


if __name__ == "__main__":
    unittest.main()
