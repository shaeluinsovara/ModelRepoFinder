"""v1.6.2: rate limits, partial searches, and the real download path. No network."""
from __future__ import annotations

import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import helpers
import scheduler as S
import seen_store
import watcher as W


def _tz():
    return timezone(timedelta(hours=-5))


def _dt(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=_tz())


class Http429(Exception):
    """Looks like huggingface_hub's HfHubHTTPError for a 429."""

    def __init__(self, retry_after="7"):
        super().__init__(
            "429 Client Error: Too Many Requests for url: https://huggingface.co/api/models"
            "?filter=gguf&search=TTS ... We had to rate limit your IP."
        )
        headers = {"Retry-After": retry_after} if retry_after is not None else {}
        self.response = SimpleNamespace(status_code=429, headers=headers)


def _sched(**kw):
    raw = dict(S.DEFAULT_SCHEDULE)
    raw.update(kw)
    return S.normalize_schedule(raw)


class FakeHub:
    """A tiny stand-in for HfApi: list_models, list_repo_tree, model_info."""

    def __init__(self, repos=None, fail_queries=(), fail_times=None, tree_429=()):
        self.repos = repos or {
            "org/Coder-7B-GGUF": [("coder-7b.Q4_K_M.gguf", 4 * 1024 ** 3)],
            "org/Coding-3B-GGUF": [("coding-3b.Q4_K_M.gguf", 2 * 1024 ** 3)],
            "org/Big-Coder-70B-GGUF": [("big.Q4_K_M.gguf", 40 * 1024 ** 3)],
        }
        self.fail_queries = set(fail_queries)
        self.fail_times = dict(fail_times or {})
        self.tree_429 = set(tree_429)
        self.list_calls = []
        self.tree_calls = []

    def list_models(self, **kw):
        self.list_calls.append(dict(kw))
        q = kw.get("search") or ""
        if q in self.fail_queries:
            raise Http429()
        if self.fail_times.get(q):
            self.fail_times[q] -= 1
            raise Http429(retry_after="3")
        if kw.get("author") == "mlx-community":
            return []
        out = []
        for i, mid in enumerate(self.repos):
            if q and q.lower() not in mid.lower():
                continue
            out.append(SimpleNamespace(
                id=mid, downloads=1000 - i, likes=10, tags=["gguf"], pipeline_tag="text-generation",
                trending_score=0, last_modified="2026-11-01T00:00:00", created_at="2026-10-01T00:00:00",
            ))
        return out

    def list_repo_tree(self, model_id, recursive=True):
        self.tree_calls.append(model_id)
        if model_id in self.tree_429:
            raise Http429(retry_after="1")
        return [SimpleNamespace(path=p, size=size) for p, size in self.repos.get(model_id, [])]

    def model_info(self, model_id):
        return SimpleNamespace(tags=[], card_data=None)


def stub_hf_hub_download(*, repo_id, filename, local_dir, user_agent=None, token=None,
                          revision=None, cache_dir=None, force_download=False):
    """Mimics huggingface_hub 0.36's hf_hub_download: NO tqdm_class argument."""
    target = Path(local_dir) / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"GGUF" + repo_id.encode())
    return str(target)


class PoliteApiCallTests(unittest.TestCase):
    def setUp(self):
        helpers.quiet_hf_network_layer()
        self.waits = []
        W._HF_SLEEP = self.waits.append

    def tearDown(self):
        helpers.quiet_hf_network_layer()

    def test_429_honors_retry_after_then_succeeds(self):
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            if calls["n"] == 1:
                raise Http429(retry_after="7")
            return "ok"

        W.reset_hf_budget(120)
        self.assertEqual(W.hf_api_call(fn), "ok")
        self.assertEqual(self.waits, [7.0])
        self.assertAlmostEqual(W.hf_budget_state()["remaining"], 113.0)

    def test_retry_after_is_capped_and_budget_limits_total_wait(self):
        def always():
            raise Http429(retry_after="600")

        W.reset_hf_budget(45)
        with self.assertRaises(W.HFRateLimited):
            W.hf_api_call(always)
        self.assertTrue(all(w <= W.HF_MAX_SINGLE_WAIT for w in self.waits))
        self.assertLessEqual(sum(self.waits), 45)
        # Out of patience and rate limited: the next call does not even ask.
        asked = {"n": 0}

        def counted():
            asked["n"] += 1
            return "never"

        with self.assertRaises(W.HFRateLimited):
            W.hf_api_call(counted)
        self.assertEqual(asked["n"], 0)
        # A new run gets a fresh budget.
        W.reset_hf_budget(120)
        self.assertEqual(W.hf_api_call(counted), "never")

    def test_non_transient_errors_are_not_retried(self):
        calls = {"n": 0}

        def bad():
            calls["n"] += 1
            raise ValueError("Invalid sort parameter")

        with self.assertRaises(ValueError):
            W.hf_api_call(bad)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(self.waits, [])

    def test_5xx_retries_with_backoff(self):
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                err = RuntimeError("503 Server Error")
                err.response = SimpleNamespace(status_code=503, headers={})
                raise err
            return [1]

        self.assertEqual(W.hf_api_call(flaky), [1])
        self.assertEqual(self.waits, [2.0, 4.0])

    def test_search_retries_429_and_does_not_switch_off_trending(self):
        W.reset_hf_sort_state()
        hub = FakeHub(fail_times={"coder": 1})
        with patch.object(W, "HF", hub):
            out = W.search_models("coder", limit=5, gguf_only=True, sort="trending")
        self.assertTrue(out)
        self.assertEqual(self.waits, [3.0])
        self.assertNotEqual(W._HF_TRENDING_OK, False)
        W.reset_hf_sort_state()

    def test_search_results_are_cached_briefly(self):
        W.HF_CACHE_TTL = 600
        W.clear_hf_cache()
        hub = FakeHub()
        with patch.object(W, "HF", hub):
            W.search_models("coder", limit=5, gguf_only=True)
            W.search_models("coder", limit=5, gguf_only=True)
        self.assertEqual(len(hub.list_calls), 1)

    def test_saved_token_is_sent_with_searches(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            (tmp / ".hf_token").write_text("hf_testtoken123\n", encoding="utf-8")
            W.set_hf_token_root(tmp)
            with patch.object(W, "_keyring_get", return_value=""):
                W.forget_hf_token_cache()
                client = W._hf_client()
                self.assertIsNot(client, W.HF)
                self.assertEqual(client.token, "hf_testtoken123")
                from huggingface_hub.utils import build_hf_headers

                headers = build_hf_headers(token=client.token)
                self.assertEqual(headers.get("authorization"), "Bearer hf_testtoken123")
        finally:
            W.set_hf_token_root(None)
            W.forget_hf_token_cache()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_friendly_errors(self):
        self.assertIn("HTTP 429", W.friendly_error(Http429()))
        msg = W.friendly_error(TypeError("hf_hub_download() got an unexpected keyword argument 'tqdm_class'"))
        self.assertIn("launcher", msg)


class DownloadCompatTests(unittest.TestCase):
    def test_download_gguf_works_without_tqdm_class(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            progress = {"total_bytes": 10, "log": []}
            with patch.object(W, "hf_hub_download", stub_hf_hub_download):
                path = W.download_gguf("org/x-GGUF", "x.Q4_K_M.gguf", tmp, progress=progress)
            self.assertTrue(path.exists())
            self.assertGreater(path.stat().st_size, 0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_arguments_fit_the_installed_huggingface_hub(self):
        """The real installed hf_hub_download / snapshot_download accept what we send."""
        import inspect

        args = dict(repo_id="a/b", filename="x.gguf", local_dir="/tmp/x", user_agent="t",
                    token=None, tqdm_class=W.DownloadProgress)
        kept = W._supported_kwargs(W.hf_hub_download, args)
        inspect.signature(W.hf_hub_download).bind(**kept)  # raises TypeError if not
        snap_args = dict(repo_id="a/b", local_dir="/tmp/x", user_agent="t", token=None,
                         ignore_patterns=[".git*"], tqdm_class=W.DownloadProgress)
        kept = W._supported_kwargs(W.snapshot_download, snap_args)
        inspect.signature(W.snapshot_download).bind(**kept)

    def test_tqdm_class_still_passed_when_supported(self):
        seen = {}

        def snap(*, repo_id, local_dir, tqdm_class=None, user_agent=None, token=None, ignore_patterns=None):
            seen["tqdm_class"] = tqdm_class
            Path(local_dir, "model.safetensors").write_bytes(b"x")
            return local_dir

        tmp = Path(tempfile.mkdtemp())
        try:
            with patch.object(W, "snapshot_download", snap):
                W.download_repo("org/y", tmp, progress={})
            if W.tqdm is not None:
                self.assertIs(seen["tqdm_class"], W.DownloadProgress)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_download_retry_honors_retry_after(self):
        W._TEST_HOOKS["sleep"] = None
        slept = []
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            if calls["n"] == 1:
                raise Http429(retry_after="9")
            return "ok"

        try:
            with patch.object(W.time, "sleep", slept.append):
                self.assertEqual(W.download_with_retries(fn, progress={"log": []}), "ok")
        finally:
            W._TEST_HOOKS["sleep"] = 0
        self.assertEqual(slept, [9.0])


class PartialCategorySearchTests(unittest.TestCase):
    def setUp(self):
        helpers.quiet_hf_network_layer()
        self.tmp = Path(tempfile.mkdtemp())
        seen_store.set_path(self.tmp / ".mrf_seen.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_one_failed_query_keeps_the_other_results(self):
        cfg = helpers.sample_config()
        hub = FakeHub(fail_queries={"coding"})
        with patch.object(W, "HF", hub):
            data = W.collect_suggestions(cfg, "mac_16gb", categories=["coding"], record_seen=False)
        tag = data["tags"][0]
        self.assertIsNone(tag["error"])
        self.assertTrue(tag.get("partial"))
        self.assertTrue(tag.get("rate_limited"))
        self.assertIn("org/Coder-7B-GGUF", [m["id"] for m in tag["models"]])

    def test_rate_limited_file_checks_mark_partial(self):
        cfg = helpers.sample_config()
        hub = FakeHub(tree_429={"org/Coding-3B-GGUF"})
        with patch.object(W, "HF", hub):
            data = W.collect_suggestions(cfg, "mac_16gb", categories=["coding"], record_seen=False)
        tag = data["tags"][0]
        self.assertTrue(tag.get("partial"))
        self.assertIn("could not be checked", tag.get("warning") or "")


class ScheduledRunTests(unittest.TestCase):
    def setUp(self):
        helpers.quiet_hf_network_layer()
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = helpers.sample_config()
        self.cfg["paths"] = {"base": str(self.tmp), "incoming": "incoming", "retired": "retired"}
        self.cfg["allow_list"] = []
        S.set_path(self.tmp / ".mrf_schedule.json")
        seen_store.set_path(self.tmp / ".mrf_seen.json")
        self.inv = S._empty_inventory()
        S.save_inventory(self.inv, S.current_path())
        self.incoming = W.incoming_dir(self.cfg, "mac_16gb", "coding", self.tmp)
        self.incoming.mkdir(parents=True)

    def tearDown(self):
        S.set_path(None)
        helpers.quiet_hf_network_layer()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _old_tracked(self):
        rows = []
        for i, name in enumerate(("old-a", "old-b", "old-c")):
            folder = self.incoming / name
            folder.mkdir()
            (folder / "w.gguf").write_bytes(b"old")
            rows.append({
                "id": f"org/{name}", "machine": "mac_16gb", "tag": "coding", "name": name,
                "relative_path": name, "path": str(folder), "size_gb": 0.0, "format": "gguf",
                "family": S.model_family(f"org/{name}"),
                "downloaded_at": S._iso(_dt(2026, 1, 1 + i)), "source": "auto",
            })
        self.inv["tracked"] = rows
        S.save_inventory(self.inv, S.current_path())
        return rows

    def test_end_to_end_scheduled_run_really_downloads(self):
        """Real collector + real default_download, with only the network faked."""
        W.HF_CACHE_TTL = 600
        W.clear_hf_cache()
        hub = FakeHub()
        sched = _sched(enabled=True, categories=["coding"], max_new_per_run=3, cleanup_enabled=False)
        with patch.object(W, "HF", hub), patch.object(W, "hf_hub_download", stub_hf_hub_download), \
                patch.object(W, "load_hf_token", return_value=""):
            preview = S.execute_run(self.cfg, "mac_16gb", sched, inventory=self.inv,
                                    already_ids=set(), dry_run=True, root=self.tmp,
                                    now_dt=_dt(2026, 11, 13, 3, 0))
            calls_after_preview = len(hub.list_calls)
            result = S.execute_run(self.cfg, "mac_16gb", sched, inventory=self.inv,
                                   already_ids=set(), dry_run=False, root=self.tmp,
                                   now_dt=_dt(2026, 11, 13, 3, 5))
        picked = sorted(m["id"] for m in preview["would_download"])
        self.assertEqual(picked, ["org/Coder-7B-GGUF", "org/Coding-3B-GGUF"])  # 70B is over the cap
        self.assertFalse(preview["partial_search"])
        self.assertEqual(result["status"], "ok", result["message"])
        self.assertEqual(sorted(d["id"] for d in result["downloaded"]), picked)
        self.assertEqual(result["failed"], [])
        for row in result["downloaded"]:
            self.assertTrue(Path(row["path"]).exists())
        self.assertTrue((self.incoming / "coder-7b.Q4_K_M.gguf").exists())
        # The real run reused the dry run's search results (no double requests).
        self.assertEqual(len(hub.list_calls), calls_after_preview)
        loaded = S.load_inventory(S.current_path())
        self.assertEqual(len(S.tracked_for(loaded, "mac_16gb")), 2)
        self.assertTrue(S.machine_state(loaded, "mac_16gb").get("last_run"))

    def test_partial_search_still_downloads_and_skips_cleanup(self):
        self._old_tracked()
        hub = FakeHub(fail_queries={"coding"})
        sched = _sched(enabled=True, categories=["coding"], max_new_per_run=3,
                       cleanup_enabled=True, cleanup_policy="keep_newest", keep_newest_n=1)
        with patch.object(W, "HF", hub), patch.object(W, "hf_hub_download", stub_hf_hub_download), \
                patch.object(W, "load_hf_token", return_value=""), \
                patch.object(W, "current_hf_token", return_value=""):
            preview = S.execute_run(self.cfg, "mac_16gb", sched, inventory=self.inv, already_ids=set(),
                                    dry_run=True, root=self.tmp, now_dt=_dt(2026, 11, 13, 3, 0))
            result = S.execute_run(self.cfg, "mac_16gb", sched, inventory=self.inv, already_ids=set(),
                                   dry_run=False, root=self.tmp, now_dt=_dt(2026, 11, 13, 3, 0))
        self.assertTrue(preview["partial_search"])
        self.assertTrue(preview["rate_limited"])
        self.assertEqual(preview["would_cleanup"], [])
        self.assertIn("Part of the search did not finish", preview["message"])
        self.assertTrue(result["downloaded"])
        self.assertEqual(result["status"], "partial")
        self.assertTrue(result["partial_search"])
        self.assertTrue(result["cleanup_skipped"])
        self.assertIn("incomplete", result["cleanup_skipped_reason"])
        self.assertEqual(result["cleaned"], [])
        self.assertIn("Access Tokens", result["message"])
        for name in ("old-a", "old-b", "old-c"):
            self.assertTrue((self.incoming / name).exists())
        loaded = S.load_inventory(S.current_path())
        entry = loaded["log"][-1]
        self.assertTrue(entry["partial_search"])
        self.assertTrue(S.machine_state(loaded, "mac_16gb").get("last_run"))

    def test_complete_search_still_cleans_up(self):
        self._old_tracked()
        hub = FakeHub()
        sched = _sched(enabled=True, categories=["coding"], max_new_per_run=3,
                       cleanup_enabled=True, cleanup_policy="keep_newest", keep_newest_n=1)
        with patch.object(W, "HF", hub), patch.object(W, "hf_hub_download", stub_hf_hub_download), \
                patch.object(W, "load_hf_token", return_value=""):
            result = S.execute_run(self.cfg, "mac_16gb", sched, inventory=self.inv, already_ids=set(),
                                   dry_run=False, root=self.tmp, now_dt=_dt(2026, 11, 13, 3, 0))
        self.assertEqual(result["status"], "ok", result["message"])
        self.assertFalse(result["cleanup_skipped"])
        self.assertTrue(result["cleaned"])

    def test_zero_results_from_rate_limit_backs_off_an_hour(self):
        hub = FakeHub(fail_queries={"coder", "coding"})
        sched = _sched(enabled=True, frequency="daily", time="03:00", categories=["coding"])
        now = _dt(2026, 11, 13, 3, 0)
        S.note_enabled_transition(self.inv, "mac_16gb", False, True, _dt(2026, 11, 12, 9, 0))
        with patch.object(W, "HF", hub), patch.object(W, "current_hf_token", return_value=""):
            result = S.execute_run(self.cfg, "mac_16gb", sched, inventory=self.inv, already_ids=set(),
                                   dry_run=False, root=self.tmp, now_dt=now)
        self.assertEqual(result["status"], "rate_limited")
        self.assertEqual(result["downloaded"], [])
        self.assertTrue(result["cleanup_skipped"])
        self.assertIn("Access Tokens", result["message"])
        loaded = S.load_inventory(S.current_path())
        state = S.machine_state(loaded, "mac_16gb")
        self.assertFalse(state.get("last_run"))
        self.assertEqual(S.parse_iso(state["retry_not_before"]), now + timedelta(minutes=60))
        self.assertFalse(S.is_due(sched, now + timedelta(minutes=1), state))
        self.assertFalse(S.is_due(sched, now + timedelta(minutes=59), state))
        self.assertTrue(S.is_due(sched, now + timedelta(minutes=61), state))
        self.assertEqual(loaded["log"][-1]["status"], "rate_limited")
        # A later successful run clears the back-off.
        with patch.object(W, "HF", FakeHub()), patch.object(W, "hf_hub_download", stub_hf_hub_download), \
                patch.object(W, "load_hf_token", return_value=""):
            ok = S.execute_run(self.cfg, "mac_16gb", sched, inventory=loaded, already_ids=set(),
                               dry_run=False, root=self.tmp, now_dt=now + timedelta(minutes=61))
        self.assertEqual(ok["status"], "ok", ok["message"])
        state = S.machine_state(S.load_inventory(S.current_path()), "mac_16gb")
        self.assertIsNone(state.get("retry_not_before"))
        self.assertTrue(state.get("last_run"))

    def test_download_errors_are_plain_and_failed_models_stay_eligible(self):
        def old_hub_download(*, repo_id, filename, local_dir, user_agent=None, token=None):
            raise TypeError("hf_hub_download() got an unexpected keyword argument 'tqdm_class'")

        hub = FakeHub()
        sched = _sched(enabled=True, categories=["coding"], max_new_per_run=3, cleanup_enabled=True)
        with patch.object(W, "HF", hub), patch.object(W, "hf_hub_download", old_hub_download), \
                patch.object(W, "load_hf_token", return_value=""):
            result = S.execute_run(self.cfg, "mac_16gb", sched, inventory=self.inv, already_ids=set(),
                                   dry_run=False, root=self.tmp, now_dt=_dt(2026, 11, 13, 3, 0))
        self.assertEqual(result["status"], "error")
        self.assertTrue(result["cleanup_skipped"])
        self.assertIn("launcher", result["failed"][0]["error"])
        self.assertIn("tqdm_class", result["failed"][0]["error_detail"])
        entry = S.load_inventory(S.current_path())["log"][-1]
        self.assertTrue(entry["failed"])
        self.assertTrue(any("Download of org/" in e for e in entry["errors"]))
        # The failed models were put on the approve list, but the next run
        # must still offer them instead of hiding them forever.
        self.assertIn("org/Coder-7B-GGUF", self.cfg["allow_list"])
        with patch.object(W, "HF", hub), patch.object(W, "hf_hub_download", stub_hf_hub_download), \
                patch.object(W, "load_hf_token", return_value=""):
            again = S.execute_run(self.cfg, "mac_16gb", sched, inventory=self.inv, already_ids=set(),
                                  dry_run=False, root=self.tmp, now_dt=_dt(2026, 11, 14, 3, 0))
        self.assertIn("org/Coder-7B-GGUF", [d["id"] for d in again["downloaded"]])

    def test_top_picks_category_downloads_into_top_picks_folder(self):
        hub = FakeHub()
        sched = _sched(enabled=True, categories=[], include_top_picks=True, max_new_per_run=1,
                       cleanup_enabled=False)
        self.cfg["machines"]["mac_16gb"]["hf_categories"] = []
        with patch.object(W, "HF", hub), patch.object(W, "hf_hub_download", stub_hf_hub_download), \
                patch.object(W, "load_hf_token", return_value=""):
            result = S.execute_run(self.cfg, "mac_16gb", sched, inventory=self.inv, already_ids=set(),
                                   dry_run=False, root=self.tmp, now_dt=_dt(2026, 11, 13, 3, 0))
        self.assertEqual(result["status"], "ok", result["message"])
        self.assertEqual(len(result["downloaded"]), 1)
        self.assertEqual(result["downloaded"][0]["tag"], "top_picks")
        self.assertTrue(Path(result["downloaded"][0]["path"]).exists())


if __name__ == "__main__":
    unittest.main()
