import unittest

import helpers  # noqa: F401
import watcher as W


class FakeHttpError(Exception):
    def __init__(self, status, message=""):
        super().__init__(message or f"HTTP {status}")
        self.status_code = status


class RetryAndXetTests(unittest.TestCase):
    def setUp(self):
        W._TEST_HOOKS["fail_times"] = 0
        W._TEST_HOOKS["error"] = None
        W._TEST_HOOKS["sleep"] = 0
        W._TEST_HOOKS["xet_disabled"] = False

    def tearDown(self):
        W._TEST_HOOKS["fail_times"] = 0
        W._TEST_HOOKS["error"] = None
        W._TEST_HOOKS["sleep"] = 0
        W._TEST_HOOKS["xet_disabled"] = False

    def test_succeeds_after_transient_failures(self):
        W._TEST_HOOKS["fail_times"] = 2
        W._TEST_HOOKS["error"] = ConnectionError("Connection reset by peer")
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            return "ok"

        self.assertEqual(W.download_with_retries(fn, attempts=4), "ok")
        # Hook fails twice, then fn runs successfully on the third attempt.
        self.assertEqual(calls["n"], 1)
        self.assertEqual(W._TEST_HOOKS["fail_times"], 0)

    def test_gives_up_when_retries_exhausted(self):
        W._TEST_HOOKS["fail_times"] = 10
        W._TEST_HOOKS["error"] = ConnectionError("timeout")
        with self.assertRaises(ConnectionError):
            W.download_with_retries(lambda: "ok", attempts=3)

    def test_xet_fallback_after_two_xet_errors(self):
        W._TEST_HOOKS["fail_times"] = 2
        W._TEST_HOOKS["error"] = RuntimeError("CAS client error: file reconstruction error via hf_xet")
        W._TEST_HOOKS["xet_disabled"] = False
        log = {"log": [], "message": ""}
        result = W.download_with_retries(lambda: "ok", progress=log, attempts=4)
        self.assertEqual(result, "ok")
        self.assertTrue(W._TEST_HOOKS["xet_disabled"])
        blob = " ".join(log["log"])
        self.assertIn("simpler download", blob.lower())

    def test_is_xet_error_hints(self):
        self.assertTrue(W._is_xet_error(RuntimeError("CAS client error")))
        self.assertTrue(W._is_xet_error(RuntimeError("hf_xet failed")))
        self.assertFalse(W._is_xet_error(RuntimeError("ordinary timeout")))

    def test_permanent_http_errors_are_not_retried(self):
        for status, msg in (
            (401, "401 Unauthorized"),
            (403, "403 Forbidden"),
            (404, "404 Not Found"),
        ):
            with self.subTest(status=status):
                W._TEST_HOOKS["fail_times"] = 0
                calls = {"n": 0}

                def fn(status=status, msg=msg):
                    calls["n"] += 1
                    raise FakeHttpError(status, msg)

                with self.assertRaises(FakeHttpError):
                    W.download_with_retries(fn, attempts=4)
                self.assertEqual(calls["n"], 1)

    def test_disk_full_is_not_retried(self):
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            raise OSError("No space left on device: disk full")

        with self.assertRaises(OSError):
            W.download_with_retries(fn, attempts=4)
        self.assertEqual(calls["n"], 1)

    def test_retryable_http_5xx(self):
        W._TEST_HOOKS["fail_times"] = 0
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            if calls["n"] < 3:
                raise FakeHttpError(503, "503 Service Unavailable")
            return "ok"

        self.assertEqual(W.download_with_retries(fn, attempts=4), "ok")
        self.assertEqual(calls["n"], 3)


if __name__ == "__main__":
    unittest.main()
