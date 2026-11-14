"""API tests for v1.7: detected hardware, size limit, My Library, export, progress. No network."""
from __future__ import annotations

import json
import shutil
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import helpers
import hardware
import scheduler as S


class V17ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.A, self.W, self.token = helpers.isolate(self.tmp)
        self.server, self.port = helpers.start_server(self.A)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        S.set_path(None)
        S.set_now_fn(None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _post(self, path, body, *, token=True, **kw):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-MRF-Token"] = self.token
        return helpers.http_request(self.port, "POST", path, body=body, headers=headers, **kw)

    # -- hardware -----------------------------------------------------------
    def test_info_has_detected_profile_first(self):
        status, data, _ = helpers.http_request(self.port, "GET", "/api/info")
        self.assertEqual(status, 200)
        self.assertEqual(data["version"], helpers.app_version())
        self.assertEqual(data["detected_machine"], "this_computer")
        self.assertEqual(data["machines"][0]["key"], "this_computer")
        self.assertTrue(data["machines"][0]["detected"])
        self.assertEqual(data["machines"][0]["max_model_gb"], 9.5)
        self.assertEqual(data["machine"], "this_computer")  # the default choice
        self.assertIn("RTX 4070", data["hardware"]["summary"])

    def test_get_hardware(self):
        status, data, _ = helpers.http_request(self.port, "GET", "/api/hardware")
        self.assertEqual(status, 200)
        self.assertEqual(data["kind"], "gpu")
        self.assertEqual(data["suggested_max_model_gb"], 9.5)

    def test_detect_again_needs_token_and_updates(self):
        status, _, _ = self._post("/api/hardware/detect", {}, token=False, expect_reject=True)
        self.assertIn(status, helpers.REJECTED)
        with patch.object(hardware, "detect", return_value=helpers.fake_hardware("apple")):
            status, data, _ = self._post("/api/hardware/detect", {})
        self.assertEqual(status, 200, data)
        self.assertEqual(data["hardware"]["kind"], "apple")
        this = next(m for m in data["machines"] if m["key"] == "this_computer")
        self.assertEqual(this["max_model_gb"], 10.0)

    def test_override_cap_and_reset(self):
        status, data, _ = self._post("/api/settings", {"machine": "this_computer", "max_model_gb": 11})
        self.assertEqual(status, 200, data)
        this = next(m for m in data["machines"] if m["key"] == "this_computer")
        self.assertEqual(this["max_model_gb"], 11.0)
        self.assertEqual(this["cap_source"], "yours")
        saved = (self.tmp / "machines.yaml").read_text(encoding="utf-8")
        self.assertIn("max_model_gb_override: 11", saved)
        self.assertNotIn("RTX 4070", saved)  # hardware details are never saved
        self.assertNotIn("_runtime_keys", saved)
        status, data, _ = self._post("/api/settings", {"machine": "this_computer", "max_model_gb": None})
        self.assertEqual(status, 200, data)
        this = next(m for m in data["machines"] if m["key"] == "this_computer")
        self.assertEqual(this["max_model_gb"], 9.5)
        self.assertEqual(this["cap_source"], "detected")

    def test_bad_caps_rejected(self):
        for bad in (0, -1, 5000, "lots", float("nan")):
            with self.subTest(bad=bad):
                body = json.dumps({"machine": "this_computer", "max_model_gb": bad}).replace("NaN", '"NaN"')
                status, data, _ = helpers.http_request(
                    self.port, "POST", "/api/settings", body=body,
                    headers={"Content-Type": "application/json", "X-MRF-Token": self.token},
                )
                self.assertEqual(status, 400, data)
        status, data, _ = self._post("/api/settings", {"machine": "pc_16gb_gpu", "max_model_gb": None})
        self.assertEqual(status, 400, data)

    def test_preset_cap_change(self):
        status, data, _ = self._post("/api/settings", {"machine": "pc_8gb_gpu", "max_model_gb": 7})
        self.assertEqual(status, 200, data)
        m = next(m for m in data["machines"] if m["key"] == "pc_8gb_gpu")
        self.assertEqual(m["max_model_gb"], 7.0)

    # -- library ------------------------------------------------------------
    def _seed_library(self):
        cfg = self.A._cfg()
        dest = self.W.incoming_dir(cfg, "this_computer", "coding", self.A.ROOT)
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "=evil.gguf").write_bytes(b"x" * 10)
        (dest / "fine-Q4_K_M.gguf").write_bytes(b"x" * 10)

    def test_library_and_export(self):
        self._seed_library()
        status, data, _ = helpers.http_request(self.port, "GET", "/api/library")
        self.assertEqual(status, 200, data)
        names = [r["name"] for r in data["rows"]]
        self.assertIn("fine-Q4_K_M.gguf", names)
        self.assertEqual(data["summary"]["from_app"], 2)
        self.assertNotIn(self.token, json.dumps(data))

        conn_status, _, raw = self._post("/api/library/export", {"kind": "model", "search": ""})
        self.assertEqual(conn_status, 200)
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        text = raw.decode("utf-8-sig")
        self.assertIn("'=evil.gguf", text)
        self.assertNotIn(",=evil", text)

        _, _, filtered = self._post("/api/library/export", {"search": "fine"})
        self.assertNotIn("evil", filtered.decode("utf-8-sig"))

    def test_export_headers_and_token(self):
        from http.client import HTTPConnection

        conn = HTTPConnection("127.0.0.1", self.port, timeout=10)
        body = b"{}"
        conn.request("POST", "/api/library/export", body=body, headers={
            "Content-Type": "application/json", "Content-Length": "2", "X-MRF-Token": self.token,
        })
        resp = conn.getresponse()
        resp.read()
        self.assertEqual(resp.status, 200)
        self.assertIn("text/csv", resp.getheader("Content-Type"))
        self.assertRegex(resp.getheader("Content-Disposition"), r'attachment; filename="ModelRepoFinder-library-\d{4}-\d{2}-\d{2}\.csv"')
        conn.close()
        status, _, _ = self._post("/api/library/export", {}, token=False, expect_reject=True)
        self.assertIn(status, helpers.REJECTED)

    # -- 403 with a body ----------------------------------------------------
    def test_refused_post_drains_body_and_closes(self):
        payload = json.dumps({"scope": "all", "pad": "x" * 200_000}).encode()
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as sock:
            head = (
                "POST /api/seen/reset HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{self.port}\r\n"
                "Content-Type: application/json\r\n"
                f"X-MRF-Token: {self.token}\r\n"
                "Origin: http://evil.example\r\n"
                f"Content-Length: {len(payload)}\r\n\r\n"
            ).encode()
            sock.sendall(head + payload)  # whole body goes through without a reset
            chunks = []
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        reply = b"".join(chunks).decode("latin-1")
        self.assertTrue(reply.startswith("HTTP/1.1 403") or reply.startswith("HTTP/1.0 403"), reply[:80])
        self.assertIn("connection: close", reply.lower())

    # -- download progress --------------------------------------------------
    def test_download_job_reports_detailed_progress(self):
        def fake_fetch(repo_id, item, dest, *, progress=None, token=None, nested=True):
            dest.mkdir(parents=True, exist_ok=True)
            path = dest / item["file_path"]
            path.write_bytes(b"m" * 2048)
            if progress is not None:
                progress["downloaded_bytes"] = 2048
                progress["total_bytes"] = 2048
            return path

        items = [
            {"id": "org/A-GGUF", "tag": "coding", "format": "gguf", "file_path": "a.Q4_K_M.gguf", "size": 2048},
            {"id": "org/B-GGUF", "tag": "coding", "format": "gguf", "file_path": "b.Q4_K_M.gguf", "size": 2048},
        ]
        with patch.object(self.W, "fetch_model", side_effect=fake_fetch):
            self.A._run_downloads("this_computer", items)
        status, job, _ = helpers.http_request(self.port, "GET", "/api/job")
        self.assertEqual(status, 200)
        d = job["download"]
        self.assertEqual(d["status"], "done", d)
        self.assertEqual(d["item_count"], 2)
        self.assertEqual(d["overall_total_bytes"], 4096)
        self.assertEqual(d["overall_done_bytes"], 4096)
        for key in ("speed_bps", "eta_seconds", "quiet_seconds", "stalled", "active_files", "stall_after_seconds"):
            self.assertIn(key, d)

    def test_running_job_marks_stall(self):
        import transfer

        d = self.A._STATE["download"]
        transfer.reset_item(d, total_bytes=1000, name="x")
        d["status"] = "running"
        d["current"] = "org/x"
        d["downloaded_bytes"] = 10
        transfer.meter_for(d).reset(0.0)
        with patch.object(transfer, "_clock", return_value=transfer.STALL_AFTER_SECONDS + 5):
            pub = self.A._job_public("download")
        self.assertTrue(pub["stalled"])
        d["status"] = "idle"
        d["current"] = None

    # -- page ---------------------------------------------------------------
    def test_page_has_four_tabs_schedule_first(self):
        status, _, raw = helpers.http_request(self.port, "GET", "/")
        self.assertEqual(status, 200)
        html = raw.decode("utf-8")
        order = [html.index(f'id="tab-{t}"') for t in ("sched", "hf", "gh", "lib")]
        self.assertEqual(order, sorted(order))
        self.assertIn('id="tab-sched" aria-selected="true"', html)
        self.assertIn("My Library", html)
        self.assertIn("Slow connection, still going", html)
        self.assertIn("This computer", html)
        self.assertIn("Export to a spreadsheet", html)


if __name__ == "__main__":
    unittest.main()
