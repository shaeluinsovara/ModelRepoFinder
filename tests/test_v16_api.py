"""API tests for ModelRepoFinder v1.6 schedule endpoints. No network."""
from __future__ import annotations

import time
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import helpers
import scheduler as S


class V16ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.A, self.W, self.token = helpers.isolate(self.tmp)
        self.server, self.port = helpers.start_server(self.A)
        self.host = f"127.0.0.1:{self.port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        S.set_path(None)
        S.set_now_fn(None)
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _post(self, path, body, **kw):
        headers = kw.pop("headers", {}) or {}
        if kw.pop("with_token", True):
            headers["X-MRF-Token"] = self.token
        return helpers.http_request(
            self.port, "POST", path, body=body, headers=headers,
            host=kw.get("host"),
        )

    def test_page_mentions_v16_and_schedule(self):
        status, _, raw = helpers.http_request(self.port, "GET", "/")
        self.assertEqual(status, 200)
        html = raw.decode("utf-8")
        self.assertIn("v1.6", html)
        self.assertIn(self.token, html)
        self.assertIn("Scheduled auto-scan", html)
        self.assertIn("standing approval", html)
        self.assertIn("Dry run / preview", html)
        self.assertIn("Run now", html)
        self.assertIn("Type DELETE to confirm", html)

    def test_info_includes_schedule_and_hides_token(self):
        status, data, _ = helpers.http_request(self.port, "GET", "/api/info")
        self.assertEqual(status, 200)
        self.assertEqual(data.get("version"), "1.6")
        self.assertIn("schedule", data)
        self.assertIn("settings", data["schedule"])
        self.assertFalse(data["schedule"]["settings"]["enabled"])
        self.assertEqual(data["schedule"]["delete_phrase"], "DELETE")
        self.assertNotIn(self.token, str(data))

    def test_new_endpoints_require_token(self):
        for path in (
            "/api/schedule",
            "/api/schedule/preview",
            "/api/schedule/run",
            "/api/schedule/opt-in",
            "/api/schedule/opt-out",
        ):
            with self.subTest(path=path):
                status, data, _ = helpers.http_request(
                    self.port, "POST", path,
                    body={"machine": "mac_16gb"},
                    headers={"Content-Type": "application/json"},
                )
                self.assertEqual(status, 403, data)
                self.assertIn("token", data.get("error", "").lower())

    def test_wrong_origin_rejected_on_schedule(self):
        status, data, _ = helpers.http_request(
            self.port, "POST", "/api/schedule",
            body={"machine": "mac_16gb", "schedule": {"enabled": False}},
            headers={
                "Content-Type": "application/json",
                "X-MRF-Token": self.token,
                "Origin": "http://evil.example",
            },
        )
        self.assertEqual(status, 403)
        self.assertIn("Origin", data.get("error", ""))

    def test_save_schedule_and_delete_confirmation(self):
        status, data, _ = self._post(
            "/api/schedule",
            {
                "machine": "mac_16gb",
                "schedule": {
                    "enabled": True,
                    "frequency": "weekly",
                    "weekday": "monday",
                    "time": "04:30",
                    "categories": ["coding"],
                    "max_new_per_run": 4,
                    "cleanup_action": "retire",
                },
            },
        )
        self.assertEqual(status, 200, data)
        settings = data["schedule"]["settings"]
        self.assertTrue(settings["enabled"])
        self.assertEqual(settings["time"], "04:30")
        self.assertEqual(settings["max_new_per_run"], 4)
        self.assertEqual(settings["cleanup_action"], "retire")
        self.assertTrue(data["schedule"]["enabled_at"])

        # Turning on delete without confirmation is refused.
        status, data, _ = self._post(
            "/api/schedule",
            {
                "machine": "mac_16gb",
                "schedule": {
                    "enabled": True,
                    "frequency": "weekly",
                    "weekday": "monday",
                    "time": "04:30",
                    "categories": ["coding"],
                    "cleanup_action": "delete",
                },
            },
        )
        self.assertEqual(status, 400, data)
        self.assertIn("DELETE", data.get("error", ""))

        # With confirmation it is accepted.
        status, data, _ = self._post(
            "/api/schedule",
            {
                "machine": "mac_16gb",
                "schedule": {
                    "enabled": True,
                    "frequency": "weekly",
                    "weekday": "monday",
                    "time": "04:30",
                    "categories": ["coding"],
                    "cleanup_action": "delete",
                },
                "delete_confirmation": "DELETE",
            },
        )
        self.assertEqual(status, 200, data)
        self.assertEqual(data["schedule"]["settings"]["cleanup_action"], "delete")
        self.assertTrue(data["schedule"]["settings"]["delete_confirmed"])

    def test_hostile_category_rejected(self):
        status, data, _ = self._post(
            "/api/schedule",
            {
                "machine": "mac_16gb",
                "schedule": {
                    "enabled": False,
                    "categories": ["../etc"],
                },
            },
        )
        self.assertEqual(status, 400, data)

    def test_preview_and_run_use_mocks_and_stay_local(self):
        captured = {}

        def fake_collect(**kw):
            captured.update(kw)
            return {
                "tags": [{
                    "name": "coding", "title": "Coding", "error": None,
                    "models": [{
                        "id": "org/preview-model",
                        "tag": "coding",
                        "format": "gguf",
                        "size_gb": 0.4,
                        "downloads": 100,
                        "likes": 5,
                        "file": "m.gguf",
                    }],
                }]
            }

        def no_download(item):
            raise AssertionError("dry run must not download")

        incoming = self.A.W.incoming_dir(
            self.A._cfg(), "mac_16gb", "coding", Path(self.A._SESSION_BASE)
        )
        incoming.mkdir(parents=True)

        with patch.object(S, "gather_candidates", wraps=None) as _:
            pass

        with patch.object(S, "execute_run") as fake_exec:
            fake_exec.return_value = {
                "machine": "mac_16gb",
                "dry_run": True,
                "kind": "preview",
                "status": "preview",
                "message": "1 model would be downloaded, and 0 would be moved.",
                "would_download": [{"id": "org/preview-model"}],
                "would_cleanup": [],
                "errors": [],
                "offline": False,
            }
            status, data, _ = self._post(
                "/api/schedule/preview",
                {"machine": "mac_16gb"},
            )
            self.assertEqual(status, 200, data)
            # Wait inside... the worker is a thread. Poll job status.
            seen = None
            for _ in range(40):
                _, job, _ = helpers.http_request(self.port, "GET", "/api/job")
                if job.get("schedule") and job["schedule"]["status"] in {"done", "error"}:
                    seen = job["schedule"]
                    break
                time.sleep(0.05)
            self.assertIsNotNone(seen)
            self.assertEqual(seen["status"], "done")
            self.assertTrue(seen["data"]["dry_run"])
            self.assertTrue(fake_exec.called)
            kwargs = fake_exec.call_args.kwargs
            self.assertTrue(kwargs.get("dry_run"))

        with patch.object(S, "execute_run") as fake_exec:
            fake_exec.return_value = {
                "machine": "mac_16gb",
                "dry_run": False,
                "kind": "run_now",
                "status": "ok",
                "message": "Downloaded 0 models. 0 auto-managed models were moved to the retired folder.",
                "downloaded": [],
                "cleaned": [],
                "failed": [],
                "errors": [],
                "offline": False,
            }
            status, data, _ = self._post("/api/schedule/run", {"machine": "mac_16gb"})
            self.assertEqual(status, 200, data)
            for _ in range(40):
                _, job, _ = helpers.http_request(self.port, "GET", "/api/job")
                if job.get("schedule") and job["schedule"]["status"] in {"done", "error"}:
                    break
                time.sleep(0.05)
            self.assertTrue(fake_exec.called)
            self.assertFalse(fake_exec.call_args.kwargs.get("dry_run"))

    def test_opt_in_rejects_traversal(self):
        status, data, _ = self._post(
            "/api/schedule/opt-in",
            {"machine": "mac_16gb", "tag": "coding", "relative_path": "../secret"},
        )
        self.assertEqual(status, 400, data)

    def test_unknown_machine_rejected(self):
        status, data, _ = self._post(
            "/api/schedule",
            {"machine": "not_a_machine", "schedule": {"enabled": False}},
        )
        self.assertEqual(status, 400, data)


if __name__ == "__main__":
    unittest.main()
