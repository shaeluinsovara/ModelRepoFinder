import tempfile
import unittest
from pathlib import Path

import helpers


class ApiBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.A, self.W, self.token = helpers.isolate(self.tmp)
        self.server, self.port = helpers.start_server(self.A)
        self.host = f"127.0.0.1:{self.port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _post(self, path, body, **kw):
        headers = kw.pop("headers", {}) or {}
        if kw.pop("with_token", True):
            headers["X-MRF-Token"] = self.token
        return helpers.http_request(
            self.port, "POST", path, body=body, headers=headers, host=kw.get("host"), **{k: v for k, v in kw.items() if k != "host"}
        )

    def test_page_loads_and_injects_token(self):
        status, _, raw = helpers.http_request(self.port, "GET", "/")
        self.assertEqual(status, 200)
        html = raw.decode("utf-8")
        self.assertIn(self.token, html)
        self.assertNotIn(self.A.CSRF_PLACEHOLDER, html)
        self.assertIn("ModelRepoFinder", html)
        self.assertIn("v1.6", html)

    def test_get_info_ok(self):
        status, data, _ = helpers.http_request(self.port, "GET", "/api/info")
        self.assertEqual(status, 200)
        self.assertEqual(data.get("app"), "ModelRepoFinder")
        self.assertEqual(data.get("version"), "1.6")
        # The token itself must not be offered to a GET of the JSON API.
        blob = str(data)
        self.assertNotIn(self.token, blob)

    def test_post_without_token_rejected(self):
        status, data, _ = helpers.http_request(
            self.port,
            "POST",
            "/api/settings",
            body={"machine": "mac_16gb"},
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 403)
        self.assertIn("token", data.get("error", "").lower())

    def test_post_with_wrong_token_rejected(self):
        status, data, raw = self._post(
            "/api/settings",
            {"machine": "mac_16gb"},
            headers={"X-MRF-Token": "not-the-right-token"},
            with_token=False,
        )
        # with_token=False, but we passed a wrong one in headers
        status, data, _ = helpers.http_request(
            self.port,
            "POST",
            "/api/settings",
            body={"machine": "mac_16gb"},
            headers={"Content-Type": "application/json", "X-MRF-Token": "not-the-right-token"},
        )
        self.assertEqual(status, 403)

    def test_post_with_token_works(self):
        status, data, _ = self._post("/api/settings", {"machine": "mac_16gb"})
        self.assertEqual(status, 200, data)
        self.assertEqual(data.get("machine"), "mac_16gb")
        self.assertEqual(data.get("app"), "ModelRepoFinder")

    def test_bad_host_rejected(self):
        status, data, _ = helpers.http_request(
            self.port,
            "POST",
            "/api/settings",
            body={"machine": "mac_16gb"},
            headers={"Content-Type": "application/json", "X-MRF-Token": self.token},
            host="evil.example:80",
        )
        self.assertEqual(status, 403)
        self.assertIn("Host", data.get("error", ""))

    def test_bad_host_on_get_rejected(self):
        status, data, raw = helpers.http_request(
            self.port, "GET", "/", host="evil.example"
        )
        self.assertEqual(status, 403)
        self.assertNotIn(self.token.encode(), raw)

    def test_bad_origin_rejected(self):
        status, data, _ = helpers.http_request(
            self.port,
            "POST",
            "/api/settings",
            body={"machine": "mac_16gb"},
            headers={
                "Content-Type": "application/json",
                "X-MRF-Token": self.token,
                "Origin": "http://evil.example",
            },
        )
        self.assertEqual(status, 403)
        self.assertIn("Origin", data.get("error", ""))

    def test_matching_origin_allowed(self):
        status, data, _ = helpers.http_request(
            self.port,
            "POST",
            "/api/settings",
            body={"machine": "mac_16gb"},
            headers={
                "Content-Type": "application/json",
                "X-MRF-Token": self.token,
                "Origin": f"http://127.0.0.1:{self.port}",
            },
        )
        self.assertEqual(status, 200, data)

    def test_oversized_body_rejected(self):
        # Claim a huge Content-Length with a valid token; the handler must
        # refuse before reading it.
        status, data, _ = helpers.http_request(
            self.port,
            "POST",
            "/api/settings",
            body=b"{}",  # actual body is small
            headers={
                "Content-Type": "application/json",
                "X-MRF-Token": self.token,
                "Content-Length": str(self.A.MAX_BODY + 1),
            },
        )
        self.assertEqual(status, 413)
        self.assertIn("large", data.get("error", "").lower())

    def test_traversal_category_rejected(self):
        for tag in ("../etc", "..\\etc", "/etc", r"C:\Windows", "not_a_category"):
            with self.subTest(tag=tag):
                status, data, _ = self._post(
                    "/api/download",
                    {
                        "machine": "mac_16gb",
                        "items": [{"id": "org/model", "tag": tag, "format": "gguf", "file": "x.gguf"}],
                    },
                )
                self.assertEqual(status, 400, data)
                err = data.get("error", "").lower()
                self.assertTrue("category" in err or "allowed" in err or "unknown" in err, err)

    def test_traversal_github_category_rejected(self):
        status, data, _ = self._post(
            "/api/github/download",
            {"items": [{"id": "owner/repo", "tag": "../etc"}]},
        )
        self.assertEqual(status, 400, data)

    def test_unknown_route_with_token(self):
        status, data, _ = self._post("/api/does-not-exist", {})
        self.assertEqual(status, 404)


class TokenStorageTests(unittest.TestCase):
    def test_file_permissions_and_roundtrip(self):
        import os
        import sys
        import tempfile

        tmp = Path(tempfile.mkdtemp())
        try:
            import watcher as W

            W.save_secret(".hf_token", W._KEYRING_HF, "secret-value-123", tmp)
            # If keyring is not installed, a hardened file is used.
            path = tmp / ".hf_token"
            loaded = W.load_secret(".hf_token", W._KEYRING_HF, tmp)
            self.assertEqual(loaded, "secret-value-123")
            if path.exists() and sys.platform != "win32":
                mode = path.stat().st_mode & 0o777
                self.assertEqual(mode, 0o600)
            # Clearing removes it (and keychain entry if any).
            W.save_secret(".hf_token", W._KEYRING_HF, "", tmp)
            self.assertEqual(W.load_secret(".hf_token", W._KEYRING_HF, tmp), "")
            self.assertFalse(path.exists())
        finally:
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)

    def test_harden_does_not_raise(self):
        import tempfile
        import watcher as W

        tmp = Path(tempfile.mkdtemp())
        try:
            path = tmp / ".github_token"
            path.write_text("abc\n", encoding="utf-8")
            W.harden_secret_file(path)
            self.assertTrue(path.exists())
        finally:
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
