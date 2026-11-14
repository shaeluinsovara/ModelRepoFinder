"""v1.7 download progress: speed, time left, "slow but still going", per-file. No network."""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

import helpers  # noqa: F401
import transfer as T


class SpeedMeterTests(unittest.TestCase):
    def test_steady_speed(self):
        m = T.SpeedMeter()
        m.reset(0.0)
        for i in range(11):
            m.observe(i * 1_000_000, now=float(i))
        self.assertAlmostEqual(m.speed(), 1_000_000, delta=1)
        self.assertEqual(m.quiet_seconds(now=10.0), 0.0)

    def test_quiet_time_grows_when_nothing_arrives(self):
        m = T.SpeedMeter()
        m.reset(0.0)
        m.observe(0, now=0.0)
        m.observe(500, now=1.0)
        for t in range(2, 40):
            m.observe(500, now=float(t))
        self.assertGreaterEqual(m.quiet_seconds(now=40.0), 39 - 1)

    def test_counter_going_down_starts_over(self):
        m = T.SpeedMeter()
        m.reset(0.0)
        m.observe(10_000_000, now=1.0)
        m.observe(20_000_000, now=2.0)
        m.observe(100, now=3.0)  # a new file started
        self.assertEqual(m.last_bytes, 100)
        self.assertEqual(m.quiet_seconds(now=3.0), 0.0)

    def test_smoothing_dampens_spikes(self):
        m = T.SpeedMeter(window=2)
        m.reset(0.0)
        done = 0
        for t in range(1, 6):
            done += 1_000_000
            m.observe(done, now=float(t))
        steady = m.speed()
        done += 50_000_000  # one burst
        m.observe(done, now=6.0)
        self.assertLess(m.speed(), 25_000_000)
        self.assertGreater(m.speed(), steady)


class LiveFieldTests(unittest.TestCase):
    def test_eta_and_stall(self):
        sink = {}
        T.reset_item(sink, total_bytes=100_000_000, name="x")
        meter = T.meter_for(sink)
        meter.reset(0.0)
        for t in range(0, 6):
            sink["downloaded_bytes"] = t * 10_000_000
            live = T.live_fields(sink, now=float(t))
        self.assertGreater(live["speed_bps"], 0)
        self.assertIsNotNone(live["eta_seconds"])
        self.assertLessEqual(abs(live["eta_seconds"] - 5), 2)
        self.assertFalse(live["stalled"])
        later = T.live_fields(sink, now=5.0 + T.STALL_AFTER_SECONDS + 1)
        self.assertTrue(later["stalled"])
        self.assertGreaterEqual(later["quiet_seconds"], int(T.STALL_AFTER_SECONDS))

    def test_no_total_means_no_eta(self):
        sink = {}
        T.reset_item(sink, total_bytes=0)
        T.meter_for(sink).reset(0.0)
        sink["downloaded_bytes"] = 1000
        self.assertIsNone(T.live_fields(sink, now=3.0)["eta_seconds"])
        self.assertEqual(T.live_fields(None), {})


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _incomplete(self, filename: str, size: int) -> Path:
        cache = self.tmp / ".cache" / "huggingface" / "download"
        cache.mkdir(parents=True, exist_ok=True)
        p = cache / (T.short_hash(filename + ".metadata") + ".abc123.incomplete")
        p.write_bytes(b"x" * size)
        return p

    def test_hash_matches_huggingface_naming(self):
        # huggingface_hub names the partial file after sha1(<name>.metadata), base64-url.
        self.assertEqual(T.short_hash("model.gguf.metadata"), "oXa0XAG8ZeXAcRMIPdQRwbfnQrg=")
        try:
            from huggingface_hub._local_folder import _short_hash
        except Exception:  # noqa: BLE001 - private helper; only checked when present
            return
        self.assertEqual(T.short_hash("model.gguf.metadata"), _short_hash("model.gguf.metadata"))

    def test_measures_bytes_on_disk_and_names_files(self):
        (self.tmp / "already.txt").write_bytes(b"y" * 50)  # existed before: not counted
        sink = {}
        sizes = {"model-Q4.gguf": 1000, "mmproj.gguf": 400}
        mon = T.TransferMonitor(sink, self.tmp, file_sizes=sizes)
        self._incomplete("model-Q4.gguf", 300)
        mon.sample(now=1.0)
        self.assertEqual(sink["downloaded_bytes"], 300)
        self.assertEqual(sink["total_bytes"], 1400)
        self.assertAlmostEqual(sink["percent"], 21.4, places=1)
        self.assertEqual(sink["active_files"][0]["name"], "model-Q4.gguf")
        self.assertEqual(sink["active_files"][0]["total_bytes"], 1000)
        self.assertEqual(sink["active_files"][0]["percent"], 30.0)
        self.assertEqual(sink["files_done"], 0)
        self.assertEqual(sink["files_total"], 2)

    def test_finished_files_counted_and_percent_capped(self):
        sink = {}
        sizes = {"a.gguf": 100, "sub/b.json": 10}
        mon = T.TransferMonitor(sink, self.tmp, file_sizes=sizes)
        (self.tmp / "a.gguf").write_bytes(b"a" * 100)
        (self.tmp / "sub").mkdir()
        (self.tmp / "sub" / "b.json").write_bytes(b"b" * 10)
        mon.sample(now=1.0)
        self.assertEqual(sink["files_done"], 2)
        self.assertLessEqual(sink["percent"], 99.9)  # 100% only once the download says so

    def test_never_runs_backwards(self):
        sink = {"downloaded_bytes": 5000}
        mon = T.TransferMonitor(sink, self.tmp, total_bytes=10000)
        mon.sample(now=1.0)
        self.assertEqual(sink["downloaded_bytes"], 5000)

    def test_context_manager_thread_and_nesting(self):
        sink = {}
        with T.TransferMonitor(sink, self.tmp, file_sizes={"f.bin": 10}, interval=0.05) as outer:
            self.assertTrue(sink.get(T.MONITOR_FIELD))
            with T.TransferMonitor(sink, self.tmp, interval=0.05) as inner:
                self.assertFalse(inner._owner)  # the outer one keeps watching
            self.assertTrue(sink.get(T.MONITOR_FIELD))
            (self.tmp / "f.bin").write_bytes(b"z" * 10)
        self.assertTrue(outer._owner)
        self.assertNotIn(T.MONITOR_FIELD, sink)
        self.assertEqual(sink["downloaded_bytes"], 10)
        self.assertEqual(sink["files_done"], 1)
        self.assertEqual(sink["active_files"], [])

    def test_none_sink_is_harmless(self):
        with T.TransferMonitor(None, self.tmp):
            pass

    def test_format_bytes(self):
        self.assertIn("GB", T.format_bytes(3 * 1024 ** 3))
        self.assertIn("MB", T.format_bytes(5 * 1024 ** 2))


class FetchModelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_gguf_with_companion_is_one_transfer(self):
        from unittest.mock import patch
        import watcher as W

        calls = []

        def fake_gguf(repo_id, file_path, dest, *, progress=None, token=None):
            calls.append(file_path)
            out = dest / file_path.rsplit("/", 1)[-1]
            out.write_bytes(b"g" * (300 if "mmproj" in file_path else 1000))
            return out

        item = {
            "format": "gguf", "file_path": "m.Q4_K_M.gguf", "size": 1000,
            "companions": [
                {"path": "mmproj-f16.gguf", "size": 300, "role": "vision"},
                {"path": "../escape.gguf", "size": 1},  # never fetched
            ],
        }
        sink = {}
        with patch.object(W, "download_gguf", side_effect=fake_gguf):
            path = W.fetch_model("org/M-GGUF", item, self.tmp, progress=sink)
        self.assertEqual(calls, ["m.Q4_K_M.gguf", "mmproj-f16.gguf"])
        self.assertEqual(path.name, "m.Q4_K_M.gguf")
        self.assertEqual(sink["total_bytes"], 1300)
        self.assertEqual(sink["downloaded_bytes"], 1300)
        self.assertEqual((sink["files_done"], sink["files_total"]), (2, 2))
        self.assertNotIn(T.MONITOR_FIELD, sink)
        self.assertEqual(W.planned_item_bytes(item), 1300)  # unsafe companion not counted

    def test_planned_bytes_whole_repo_unknown(self):
        import watcher as W

        self.assertEqual(W.planned_item_bytes({"format": "mlx", "file_path": "(whole model)"}), 0)


if __name__ == "__main__":
    unittest.main()
