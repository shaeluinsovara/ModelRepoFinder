"""API tests for v1.5 endpoints and security. No network."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import helpers


class V15ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.A, self.W, self.token = helpers.isolate(self.tmp)
        self.server, self.port = helpers.start_server(self.A)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _post(self, path, body, *, with_token=True, headers=None, **kw):
        headers = dict(headers or {})
        if with_token:
            headers["X-MRF-Token"] = self.token
        return helpers.http_request(
            self.port, "POST", path, body=body, headers=headers, **kw
        )

    def test_info_includes_v15_fields(self):
        status, data, _ = helpers.http_request(self.port, "GET", "/api/info")
        self.assertEqual(status, 200)
        self.assertEqual(data.get("version"), "1.6.2")
        self.assertEqual(data.get("app"), "ModelRepoFinder")
        self.assertIn("local_models", data)
        self.assertIn("hf_sorts", data)
        self.assertIn("github_sorts", data)
        self.assertIn("popularity_disclaimer", data)
        keys = {s["key"] for s in data["hf_sorts"]}
        self.assertEqual(keys, {"downloads", "likes", "trending", "newest"})
        gh_keys = {s["key"] for s in data["github_sorts"]}
        self.assertEqual(gh_keys, {"stars", "updated", "fresh"})
        self.assertIn("community", (data.get("popularity_disclaimer") or "").lower())
        # Token itself must not appear in the JSON.
        self.assertNotIn(self.token, str(data))

    def test_new_posts_require_token(self):
        for path, body in (
            ("/api/seen/reset", {"scope": "all"}),
            ("/api/local-scan", {"force": True}),
            ("/api/top-picks", {"machine": "mac_16gb", "sort": "downloads"}),
            ("/api/refresh", {"machine": "pc_32gb_gpu", "categories": ["coding"], "sort": "likes"}),
        ):
            with self.subTest(path=path):
                status, data, _ = helpers.http_request(
                    self.port,
                    "POST",
                    path,
                    body=body,
                    headers={"Content-Type": "application/json"},
                )
                self.assertEqual(status, 403, data)
                self.assertIn("token", (data.get("error") or "").lower())

    def test_seen_reset_rejects_path_tricks(self):
        status, data, _ = self._post(
            "/api/seen/reset",
            {"scope": "hf", "machine": "../etc", "category": "coding"},
        )
        self.assertEqual(status, 400)
        status, data, _ = self._post(
            "/api/seen/reset",
            {"scope": "hf", "machine": "mac_16gb", "category": "../coding"},
        )
        self.assertEqual(status, 400)
        status, data, _ = self._post("/api/seen/reset", {"scope": "nope"})
        self.assertEqual(status, 400)

    def test_seen_reset_with_token(self):
        status, data, _ = self._post(
            "/api/seen/reset",
            {"scope": "hf", "machine": "mac_16gb"},
        )
        self.assertEqual(status, 200, data)
        self.assertTrue(data.get("ok"))

    def test_local_scan_and_get(self):
        status, data, _ = self._post("/api/local-scan", {"force": True})
        self.assertEqual(status, 200, data)
        self.assertIn("items", data)
        self.assertIn("sources", data)
        # GET is allowed without a token, still Host-checked.
        status, data, _ = helpers.http_request(self.port, "GET", "/api/local-models")
        self.assertEqual(status, 200)
        self.assertIn("items", data)

    def test_scan_folder_rejects_system_and_accepts_real(self):
        status, data, _ = self._post(
            "/api/settings", {"add_scan_folder": "/etc"}
        )
        self.assertEqual(status, 400, data)
        status, data, _ = self._post(
            "/api/settings", {"add_scan_folder": "/"}
        )
        self.assertEqual(status, 400, data)
        folder = self.tmp / "my-models"
        folder.mkdir()
        (folder / "voice.gguf").write_bytes(b"gguf")
        status, data, _ = self._post(
            "/api/settings", {"add_scan_folder": str(folder)}
        )
        self.assertEqual(status, 200, data)
        self.assertTrue(
            any(str(folder) in str(f) or Path(f).name == "my-models" for f in (data.get("scan_folders") or [])),
            data.get("scan_folders"),
        )

    def test_scan_folder_rejects_traversal_looking_paths_that_do_not_exist(self):
        status, data, _ = self._post(
            "/api/settings", {"add_scan_folder": str(self.tmp / ".." / "nope" / "secret")}
        )
        self.assertEqual(status, 400)

    def test_refresh_passes_sort_and_stays_local(self):
        captured = {}

        def fake_collect(cfg, machine, progress_cb=None, search_limit=80, categories=None, **kw):
            captured.update(kw)
            captured["machine"] = machine
            captured["categories"] = categories
            return {
                "machine": machine,
                "tags": [{"name": "coding", "title": "Coding", "keep": 2, "models": [], "error": None}],
                "generated_at": "2026-11-13 00:00 UTC",
                "sort": kw.get("sort"),
            }

        with patch.object(self.W, "collect_suggestions", side_effect=fake_collect):
            status, data, _ = self._post(
                "/api/refresh",
                {"machine": "pc_32gb_gpu", "categories": ["coding"], "sort": "likes"},
            )
            self.assertEqual(status, 200, data)
            # The worker runs on a thread. Stay inside the patch until it calls us.
            import time
            for _ in range(40):
                if captured:
                    break
                time.sleep(0.05)
            self.assertEqual(captured.get("sort"), "likes")
            self.assertEqual(captured.get("machine"), "pc_32gb_gpu")

    def test_top_picks_starts_and_unknown_machine_rejected(self):
        status, data, _ = self._post(
            "/api/top-picks",
            {"machine": "not_a_machine", "sort": "downloads"},
        )
        self.assertEqual(status, 400)

        def fake_top(*a, **kw):
            return {
                "overall": [],
                "tags": [],
                "disclaimer": "community signal",
                "mode": "top_picks",
                "generated_at": "2026-11-13 00:00 UTC",
                "sort": "trending",
            }

        with patch.object(self.W, "collect_top_picks", side_effect=fake_top):
            status, data, _ = self._post(
                "/api/top-picks",
                {"machine": "mac_16gb", "sort": "trending", "categories": []},
            )
        self.assertEqual(status, 200, data)
        self.assertTrue(data.get("ok"))

    def test_wrong_origin_still_rejected(self):
        status, data, _ = helpers.http_request(
            self.port,
            "POST",
            "/api/seen/reset",
            body={"scope": "all"},
            headers={
                "Content-Type": "application/json",
                "X-MRF-Token": self.token,
                "Origin": "http://evil.example",
            },
        )
        self.assertEqual(status, 403)

    def test_page_mentions_v15(self):
        status, _, raw = helpers.http_request(self.port, "GET", "/")
        self.assertEqual(status, 200)
        html = raw.decode("utf-8")
        self.assertIn("v1.6.2", html)
        self.assertIn(self.token, html)
        self.assertIn("Top picks", html)
        self.assertIn("Already on this computer", html)
        self.assertIn("Show earlier suggestions again", html)
        self.assertIn("Models on this computer", html)


if __name__ == "__main__":
    unittest.main()
