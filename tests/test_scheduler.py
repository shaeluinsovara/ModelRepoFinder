"""Unit tests for ModelRepoFinder v1.6 scheduling. No network."""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import helpers
import scheduler as S
import watcher as W


def _tz():
    return timezone(timedelta(hours=-5))  # America/New_York standard time, fixed


def _dt(y, m, d, hh=0, mm=0, ss=0):
    return datetime(y, m, d, hh, mm, ss, tzinfo=_tz())


def _sched(**kw):
    raw = dict(S.DEFAULT_SCHEDULE)
    raw.update(kw)
    return S.normalize_schedule(raw)


def _model(mid="org/model", **kw):
    row = {
        "id": mid,
        "tag": "coding",
        "format": "gguf",
        "size_gb": 1.0,
        "downloads": 1000,
        "likes": 10,
        "file": mid.split("/")[-1] + ".gguf",
    }
    row.update(kw)
    return row


def _tracked(mid="org/model", **kw):
    row = {
        "id": mid,
        "machine": "mac_16gb",
        "tag": "coding",
        "name": mid.replace("/", "__"),
        "relative_path": mid.replace("/", "__"),
        "path": "/tmp/" + mid.replace("/", "__"),
        "size_gb": 1.0,
        "format": "gguf",
        "family": S.model_family(mid),
        "downloaded_at": S._iso(_dt(2026, 10, 1, 3, 0)),
        "source": "auto",
    }
    row.update(kw)
    return row


class ScheduleMathTests(unittest.TestCase):
    def test_daily_slot_today_and_tomorrow(self):
        sched = _sched(frequency="daily", time="03:00")
        self.assertEqual(
            S.first_slot_on_or_after(sched, _dt(2026, 11, 13, 2, 59)),
            _dt(2026, 11, 13, 3, 0),
        )
        self.assertEqual(
            S.first_slot_on_or_after(sched, _dt(2026, 11, 13, 3, 0)),
            _dt(2026, 11, 13, 3, 0),
        )
        self.assertEqual(
            S.first_slot_on_or_after(sched, _dt(2026, 11, 13, 3, 1)),
            _dt(2026, 11, 14, 3, 0),
        )
        self.assertEqual(
            S.first_slot_strictly_after(sched, _dt(2026, 11, 13, 3, 0)),
            _dt(2026, 11, 14, 3, 0),
        )

    def test_weekly_weekday(self):
        # 13 Nov 2026 is a Friday.
        sched = _sched(frequency="weekly", weekday="monday", time="03:00")
        self.assertEqual(
            S.first_slot_on_or_after(sched, _dt(2026, 11, 13, 10, 0)),
            _dt(2026, 11, 16, 3, 0),
        )
        # Already Monday 03:00 — that slot counts.
        self.assertEqual(
            S.first_slot_on_or_after(sched, _dt(2026, 11, 16, 3, 0)),
            _dt(2026, 11, 16, 3, 0),
        )
        self.assertEqual(
            S.first_slot_strictly_after(sched, _dt(2026, 11, 16, 3, 0)),
            _dt(2026, 11, 23, 3, 0),
        )
        # Monday 02:59 still gets this Monday 03:00.
        self.assertEqual(
            S.first_slot_on_or_after(sched, _dt(2026, 11, 16, 2, 59)),
            _dt(2026, 11, 16, 3, 0),
        )

    def test_every_n_days_from_anchor(self):
        sched = _sched(frequency="every_n_days", every_n_days=7, time="03:00")
        anchor = _dt(2026, 11, 11, 16, 0)  # Wednesday 4pm; first slot Thursday 03:00
        first = S.first_slot_on_or_after(sched, anchor, anchor=anchor)
        self.assertEqual(first, _dt(2026, 11, 12, 3, 0))
        self.assertEqual(
            S.first_slot_on_or_after(sched, _dt(2026, 11, 13, 10, 0), anchor=anchor),
            _dt(2026, 11, 19, 3, 0),
        )
        self.assertEqual(
            S.first_slot_strictly_after(sched, _dt(2026, 11, 12, 3, 0), anchor=anchor),
            _dt(2026, 11, 19, 3, 0),
        )

    def test_invalid_time_rejected_in_strict_mode(self):
        with self.assertRaises(ValueError):
            S.normalize_schedule({"time": "25:99"}, strict=True)
        with self.assertRaises(ValueError):
            S.parse_time_of_day("noon")

    def test_disabled_is_never_due(self):
        sched = _sched(enabled=False, frequency="daily", time="03:00")
        self.assertFalse(S.is_due(sched, _dt(2026, 11, 13, 4, 0), {}))
        self.assertIsNone(S.next_run_at(sched, _dt(2026, 11, 13, 4, 0), {}))


class CatchUpTests(unittest.TestCase):
    def test_new_enable_waits_for_first_slot(self):
        sched = _sched(enabled=True, frequency="weekly", weekday="monday", time="03:00")
        # Enabled Friday afternoon; first slot is next Monday. Not due Saturday.
        state = {"enabled_at": S._iso(_dt(2026, 11, 13, 16, 0))}
        self.assertFalse(S.is_due(sched, _dt(2026, 11, 14, 10, 0), state))
        nxt = S.next_run_at(sched, _dt(2026, 11, 14, 10, 0), state)
        self.assertEqual(nxt, _dt(2026, 11, 16, 3, 0))

    def test_missed_slot_is_due_on_next_check(self):
        sched = _sched(enabled=True, frequency="weekly", weekday="monday", time="03:00")
        # Enabled last Friday; now it is Wednesday after Monday 03:00 was missed.
        state = {"enabled_at": S._iso(_dt(2026, 11, 6, 16, 0))}
        self.assertTrue(S.is_due(sched, _dt(2026, 11, 11, 10, 0), state))

    def test_catch_up_after_last_run_is_a_week_late(self):
        sched = _sched(enabled=True, frequency="weekly", weekday="monday", time="03:00")
        state = {
            "enabled_at": S._iso(_dt(2026, 10, 1, 3, 0)),
            "last_run": S._iso(_dt(2026, 11, 2, 3, 5)),  # a Monday
        }
        # Next slot was Mon 9 Nov; today is Wed 11 Nov.
        self.assertTrue(S.is_due(sched, _dt(2026, 11, 11, 10, 0), state))
        # After a catch-up run on Wednesday, the next slot is the following Monday.
        state["last_run"] = S._iso(_dt(2026, 11, 11, 10, 5))
        self.assertFalse(S.is_due(sched, _dt(2026, 11, 11, 10, 6), state))
        self.assertEqual(
            S.next_run_at(sched, _dt(2026, 11, 11, 10, 6), state),
            _dt(2026, 11, 16, 3, 0),
        )

    def test_reenable_does_not_fire_from_old_last_run(self):
        sched = _sched(enabled=True, frequency="daily", time="03:00")
        # Last ran a month ago, then was turned off, then turned back on today at 16:00.
        state = {
            "last_run": S._iso(_dt(2026, 10, 1, 3, 5)),
            "enabled_at": S._iso(_dt(2026, 11, 13, 16, 0)),
        }
        self.assertFalse(S.is_due(sched, _dt(2026, 11, 13, 16, 1), state))
        self.assertEqual(
            S.next_run_at(sched, _dt(2026, 11, 13, 16, 1), state),
            _dt(2026, 11, 14, 3, 0),
        )

    def test_launch_catch_up_same_as_is_due(self):
        # The launch tick uses the same is_due function, so a missed daily
        # slot is picked up the moment the app opens.
        sched = _sched(enabled=True, frequency="daily", time="03:00")
        state = {
            "enabled_at": S._iso(_dt(2026, 11, 10, 12, 0)),
            "last_run": S._iso(_dt(2026, 11, 12, 3, 1)),
        }
        # App opens on the 13th at 17:32, after the 03:00 slot was missed.
        self.assertTrue(S.is_due(sched, _dt(2026, 11, 13, 17, 32), state))

class ConditionsTests(unittest.TestCase):
    def test_skip_already_local(self):
        sched = _sched(skip_already_local=True)
        ok, reason = S.model_matches(
            _model("org/alpha"), sched, already_ids={"org/alpha"}
        )
        self.assertFalse(ok)
        self.assertIn("already", reason)
        ok, _ = S.model_matches(
            _model("org/alpha"), sched, already_ids={"someone/else"}
        )
        self.assertTrue(ok)
        # Off: already-local is allowed.
        sched2 = _sched(skip_already_local=False)
        ok, _ = S.model_matches(
            _model("org/alpha"), sched2, already_ids={"org/alpha"}
        )
        self.assertTrue(ok)

    def test_already_local_matches_underscore_form(self):
        sched = _sched(skip_already_local=True)
        ok, reason = S.model_matches(
            _model("org/alpha"), sched, already_ids={"org__alpha"}
        )
        self.assertFalse(ok)
        self.assertIn("already", reason)

    def test_format_filter(self):
        sched = _sched(formats=["gguf"])
        ok, _ = S.model_matches(_model(format="gguf"), sched)
        self.assertTrue(ok)
        ok, reason = S.model_matches(_model(format="mlx"), sched)
        self.assertFalse(ok)
        self.assertIn("format", reason)

    def test_must_fit_and_min_stats(self):
        sched = _sched(must_fit=True, min_downloads=500, min_likes=5)
        ok, reason = S.model_matches(_model(size_gb=20, downloads=1000, likes=10), sched, cap=8)
        self.assertFalse(ok)
        self.assertIn("cap", reason)
        ok, reason = S.model_matches(_model(size_gb=2, downloads=10, likes=10), sched, cap=8)
        self.assertFalse(ok)
        self.assertIn("downloads", reason)
        ok, reason = S.model_matches(_model(size_gb=2, downloads=1000, likes=1), sched, cap=8)
        self.assertFalse(ok)
        self.assertIn("likes", reason)
        ok, _ = S.model_matches(_model(size_gb=2, downloads=1000, likes=10), sched, cap=8)
        self.assertTrue(ok)
        # must_fit off keeps a large model.
        sched2 = _sched(must_fit=False)
        ok, _ = S.model_matches(_model(size_gb=20), sched2, cap=8)
        self.assertTrue(ok)

    def test_category_and_top_picks_filter(self):
        sched = _sched(categories=["coding"], include_top_picks=False)
        ok, _ = S.model_matches(_model(tag="coding"), sched)
        self.assertTrue(ok)
        ok, reason = S.model_matches(_model(tag="vision"), sched)
        self.assertFalse(ok)
        sched2 = _sched(categories=["coding"], include_top_picks=True)
        ok, _ = S.model_matches(_model(tag="top_picks", source="top_picks"), sched2)
        self.assertTrue(ok)

    def test_filter_dedupes_and_sorts(self):
        sched = _sched(sort="likes")
        models = [
            _model("org/a", likes=2, downloads=9),
            _model("org/b", likes=8, downloads=1),
            _model("org/a", likes=2, downloads=9),
        ]
        kept, skipped = S.filter_candidates(models, sched)
        self.assertEqual([m["id"] for m in kept], ["org/b", "org/a"])
        self.assertTrue(any(s.get("skip_reason") == "duplicate" for s in skipped))

    def test_gather_passes_sort_and_categories_to_collect(self):
        captured = {}

        def collect(**kw):
            captured.update(kw)
            return {
                "tags": [{"name": "coding", "title": "Coding", "models": [
                    _model("org/fit", tag="coding", downloads=50, likes=3, size_gb=1, format="gguf")
                ], "error": None}]
            }

        cfg = helpers.sample_config()
        sched = _sched(
            categories=["coding"], sort="trending", include_top_picks=False,
            skip_already_local=True,
        )
        rows, errors, offline = S.gather_candidates(
            cfg, "mac_16gb", sched, already_ids=set(), collect_fn=collect,
        )
        self.assertFalse(offline)
        self.assertEqual(errors, [])
        self.assertEqual(captured.get("sort"), "trending")
        self.assertEqual(captured.get("categories"), ["coding"])
        self.assertEqual([r["id"] for r in rows], ["org/fit"])


class LimitsTests(unittest.TestCase):
    def test_max_new_per_run(self):
        sched = _sched(max_new_per_run=2, max_total_auto=20, max_disk_gb=50)
        models = [_model(f"org/m{i}", size_gb=1) for i in range(5)]
        kept, skipped = S.apply_limits(models, sched, current_count=0, current_disk_gb=0)
        self.assertEqual(len(kept), 2)
        self.assertEqual(len(skipped), 3)
        self.assertTrue(all("per-run" in s["skip_reason"] for s in skipped))

    def test_max_total_auto(self):
        sched = _sched(max_new_per_run=10, max_total_auto=5, max_disk_gb=50)
        models = [_model(f"org/m{i}", size_gb=1) for i in range(4)]
        kept, skipped = S.apply_limits(models, sched, current_count=4, current_disk_gb=4)
        self.assertEqual(len(kept), 1)
        self.assertTrue(any("total" in s["skip_reason"] for s in skipped))

    def test_max_disk(self):
        sched = _sched(max_new_per_run=10, max_total_auto=20, max_disk_gb=5)
        models = [
            _model("org/a", size_gb=2),
            _model("org/b", size_gb=2),
            _model("org/c", size_gb=2),
        ]
        kept, skipped = S.apply_limits(models, sched, current_count=0, current_disk_gb=2)
        # 5 - 2 = 3 GB left; a (2) + b (2) would be 4, so only a fits.
        self.assertEqual([m["id"] for m in kept], ["org/a"])
        self.assertTrue(any("disk" in s["skip_reason"] for s in skipped))


class FamilyTests(unittest.TestCase):
    def test_family_strips_version_and_quant_keeps_size(self):
        self.assertEqual(
            S.model_family("Org/Cool-Model-v1-7B-Q4_K_M-GGUF"),
            S.model_family("org/cool-model-v2-7B-Q5_K_M-GGUF"),
        )
        self.assertNotEqual(
            S.model_family("org/cool-model-7b"),
            S.model_family("org/cool-model-13b"),
        )
        self.assertEqual(S.version_tuple("org/cool-model-v2"), (2,))
        self.assertTrue(S.is_newer({"id": "org/m-v2"}, {"id": "org/m-v1"}))

class CleanupPolicyTests(unittest.TestCase):
    def test_keep_newest_n_per_category(self):
        sched = _sched(cleanup_policy="keep_newest", keep_newest_n=2, cleanup_enabled=True)
        rows = [
            _tracked("org/a", downloaded_at=S._iso(_dt(2026, 11, 1)), relative_path="a", name="a"),
            _tracked("org/b", downloaded_at=S._iso(_dt(2026, 11, 5)), relative_path="b", name="b"),
            _tracked("org/c", downloaded_at=S._iso(_dt(2026, 11, 10)), relative_path="c", name="c"),
        ]
        planned = S.plan_cleanup(rows, sched, category_counts={"coding": 3})
        ids = {p["id"] for p in planned}
        self.assertEqual(ids, {"org/a"})  # oldest of 3, keep 2

    def test_older_than_days(self):
        sched = _sched(cleanup_policy="older_than", older_than_days=10, cleanup_enabled=True)
        rows = [
            _tracked("org/old", downloaded_at=S._iso(_dt(2026, 10, 1)), relative_path="old", name="old"),
            _tracked("org/new", downloaded_at=S._iso(_dt(2026, 11, 10)), relative_path="new", name="new"),
        ]
        planned = S.plan_cleanup(
            rows, sched, category_counts={"coding": 2}, now_dt=_dt(2026, 11, 13, 12, 0)
        )
        self.assertEqual([p["id"] for p in planned], ["org/old"])

    def test_superseded_by_newer_family(self):
        sched = _sched(cleanup_policy="superseded", cleanup_enabled=True)
        old = _tracked("org/cool-model-v1-7B", relative_path="v1", name="v1",
                       downloaded_at=S._iso(_dt(2026, 9, 1)))
        new = _tracked("org/cool-model-v2-7B", relative_path="v2", name="v2",
                       downloaded_at=S._iso(_dt(2026, 11, 1)))
        self.assertEqual(S.model_family(old["id"]), S.model_family(new["id"]))
        planned = S.plan_cleanup(
            [old, new], sched, category_counts={"coding": 2},
            newly_downloaded=[],
        )
        self.assertEqual([p["id"] for p in planned], ["org/cool-model-v1-7B"])

    def test_last_usable_keeps_one_per_category(self):
        sched = _sched(cleanup_policy="older_than", older_than_days=1, cleanup_enabled=True)
        # All three are old, but only two categories: coding has 1, vision has 2.
        rows = [
            _tracked("org/c1", tag="coding", relative_path="c1", name="c1",
                     downloaded_at=S._iso(_dt(2026, 1, 1))),
            _tracked("org/v1", tag="vision", relative_path="v1", name="v1",
                     downloaded_at=S._iso(_dt(2026, 1, 1))),
            _tracked("org/v2", tag="vision", relative_path="v2", name="v2",
                     downloaded_at=S._iso(_dt(2026, 1, 2))),
        ]
        planned = S.plan_cleanup(
            rows, sched,
            category_counts={"coding": 1, "vision": 2},
            now_dt=_dt(2026, 11, 13),
        )
        ids = {p["id"] for p in planned}
        self.assertNotIn("org/c1", ids)  # last usable in coding
        self.assertEqual(len([p for p in planned if p["tag"] == "vision"]), 1)

    def test_no_cleanup_during_download_or_on_failure(self):
        sched = _sched(cleanup_policy="keep_newest", keep_newest_n=1, cleanup_enabled=True)
        rows = [
            _tracked("org/a", relative_path="a", name="a", downloaded_at=S._iso(_dt(2026, 10, 1))),
            _tracked("org/b", relative_path="b", name="b", downloaded_at=S._iso(_dt(2026, 11, 1))),
        ]
        self.assertEqual(
            S.plan_cleanup(rows, sched, category_counts={"coding": 2}, download_in_progress=True),
            [],
        )
        self.assertEqual(
            S.plan_cleanup(rows, sched, category_counts={"coding": 2}, downloads_failed=True),
            [],
        )

    def test_cleanup_disabled(self):
        sched = _sched(cleanup_enabled=False, cleanup_policy="keep_newest", keep_newest_n=1)
        rows = [_tracked("org/a", relative_path="a"), _tracked("org/b", relative_path="b")]
        self.assertEqual(S.plan_cleanup(rows, sched, category_counts={"coding": 2}), [])


class DeleteAndContainmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = helpers.sample_config()
        self.cfg["paths"] = {"base": str(self.tmp), "incoming": "incoming", "retired": "retired"}
        self.incoming = W.incoming_dir(self.cfg, "mac_16gb", "coding", self.tmp)
        self.incoming.mkdir(parents=True)
        self.src = self.incoming / "tiny-model.gguf"
        self.src.write_bytes(b"fake-weights")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_delete_removes_and_retire_moves(self):
        dest = W.retire_item(self.cfg, "mac_16gb", "coding", "tiny-model.gguf", self.tmp)
        self.assertTrue(dest.exists())
        self.assertFalse(self.src.exists())
        self.assertIn("retired", dest.parts)
        # Put it back and delete.
        dest.replace(self.src)
        gone = S.delete_item(self.cfg, "mac_16gb", "coding", "tiny-model.gguf", self.tmp)
        self.assertFalse(Path(gone).exists())
        self.assertFalse(self.src.exists())

    def test_delete_rejects_traversal(self):
        outside = self.tmp / "secret.txt"
        outside.write_text("do-not-touch")
        with self.assertRaises(ValueError):
            S.delete_item(self.cfg, "mac_16gb", "coding", "../secret.txt", self.tmp)
        with self.assertRaises(ValueError):
            S.delete_item(self.cfg, "mac_16gb", "coding", "../../secret.txt", self.tmp)
        self.assertTrue(outside.exists())
        self.assertEqual(outside.read_text(), "do-not-touch")

    def test_delete_rejects_absolute_and_category_folder(self):
        with self.assertRaises(ValueError):
            S.delete_item(self.cfg, "mac_16gb", "coding", str(self.src.resolve()), self.tmp)
        with self.assertRaises(ValueError):
            S.delete_item(self.cfg, "mac_16gb", "coding", ".", self.tmp)
        self.assertTrue(self.src.exists())

    def test_delete_missing(self):
        with self.assertRaises(FileNotFoundError):
            S.delete_item(self.cfg, "mac_16gb", "coding", "no-such.gguf", self.tmp)

    def test_opt_in_refuses_outside_incoming(self):
        inv = S._empty_inventory()
        with self.assertRaises(ValueError):
            S.opt_in(
                self.cfg, inv, machine="mac_16gb", tag="coding",
                relative_path="../secret.txt", root=self.tmp,
            )


class ConfirmationTests(unittest.TestCase):
    def test_delete_requires_confirmation(self):
        incoming = _sched(cleanup_action="delete")
        with self.assertRaises(ValueError) as ctx:
            S.apply_delete_confirmation(incoming, _sched(cleanup_action="retire"), confirmation="")
        self.assertIn("DELETE", str(ctx.exception))
        with self.assertRaises(ValueError):
            S.apply_delete_confirmation(incoming, _sched(cleanup_action="retire"), confirmation="please")
        ok = S.apply_delete_confirmation(incoming, _sched(cleanup_action="retire"), confirmation="DELETE")
        self.assertTrue(ok["delete_confirmed"])
        # Case-insensitive, surrounding space ok.
        ok = S.apply_delete_confirmation(incoming, {}, confirmation=" delete ")
        self.assertTrue(ok["delete_confirmed"])

    def test_already_confirmed_can_save_other_fields(self):
        existing = _sched(cleanup_action="delete", delete_confirmed=True)
        incoming = _sched(cleanup_action="delete", max_new_per_run=5)
        ok = S.apply_delete_confirmation(incoming, existing, confirmation=None)
        self.assertTrue(ok["delete_confirmed"])

    def test_switching_back_to_retire_clears_confirmation(self):
        existing = _sched(cleanup_action="delete", delete_confirmed=True)
        incoming = _sched(cleanup_action="retire")
        ok = S.apply_delete_confirmation(incoming, existing, confirmation="DELETE")
        self.assertFalse(ok["delete_confirmed"])
        self.assertEqual(ok["cleanup_action"], "retire")
        # Turning delete on again requires a fresh confirmation.
        incoming2 = _sched(cleanup_action="delete")
        with self.assertRaises(ValueError):
            S.apply_delete_confirmation(incoming2, ok, confirmation="")

class DryRunAndExecuteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = helpers.sample_config()
        self.cfg["paths"] = {"base": str(self.tmp), "incoming": "incoming", "retired": "retired"}
        self.root = self.tmp
        S.set_path(self.tmp / ".mrf_schedule.json")
        self.inv = S._empty_inventory()
        S.save_inventory(self.inv, S.current_path())
        self.incoming = W.incoming_dir(self.cfg, "mac_16gb", "coding", self.tmp)
        self.incoming.mkdir(parents=True)

    def tearDown(self):
        import shutil
        S.set_path(None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _collect(self, *models):
        def fn(**kw):
            return {
                "tags": [{
                    "name": "coding", "title": "Coding", "error": None,
                    "models": list(models),
                }]
            }
        return fn

    def test_dry_run_does_not_change_anything(self):
        model = _model("org/fresh", size_gb=0.5)
        # Two existing auto-managed files so cleanup can retire the older
        # one without emptying the category (last-usable safeguard).
        existing = self.incoming / "old-model"
        existing.mkdir()
        (existing / "w.gguf").write_bytes(b"old")
        keeper = self.incoming / "keep-model"
        keeper.mkdir()
        (keeper / "w.gguf").write_bytes(b"keep")
        self.inv["tracked"] = [
            _tracked(
                "org/old", relative_path="old-model", name="old-model",
                path=str(existing),
                downloaded_at=S._iso(_dt(2026, 1, 1)),
            ),
            _tracked(
                "org/keep", relative_path="keep-model", name="keep-model",
                path=str(keeper),
                downloaded_at=S._iso(_dt(2026, 11, 10)),
            ),
        ]
        S.save_inventory(self.inv, S.current_path())
        sched = _sched(
            enabled=True, categories=["coding"], cleanup_enabled=True,
            cleanup_policy="older_than", older_than_days=10,
            cleanup_action="retire", max_new_per_run=5,
        )
        result = S.execute_run(
            self.cfg, "mac_16gb", sched, inventory=self.inv,
            collect_fn=self._collect(model),
            download_fn=lambda item: (_ for _ in ()).throw(AssertionError("download should not run")),
            dry_run=True, root=self.tmp, now_dt=_dt(2026, 11, 13, 12, 0),
        )
        self.assertTrue(result["dry_run"])
        self.assertEqual([m["id"] for m in result["would_download"]], ["org/fresh"])
        self.assertTrue(any(c["id"] == "org/old" for c in result["would_cleanup"]))
        self.assertTrue(existing.exists())
        self.assertFalse((self.tmp / "incoming" / "mac_16gb" / "coding" / "org__fresh").exists())
        # Inventory last_run was not written as a completed run.
        loaded = S.load_inventory(S.current_path())
        self.assertFalse(S.machine_state(loaded, "mac_16gb").get("last_run"))

    def test_run_downloads_then_cleans_retire(self):
        dest_file = self.incoming / "org__fresh.gguf"

        def download(item):
            dest_file.write_bytes(b"new-weights")
            return dest_file

        # Two old auto models so cleanup can retire one and still leave one
        # (last-usable) plus the new download.
        for name in ("old-a", "old-b"):
            folder = self.incoming / name
            folder.mkdir()
            (folder / "w.gguf").write_bytes(b"old")
        self.inv["tracked"] = [
            _tracked("org/old-a", relative_path="old-a", name="old-a",
                     path=str(self.incoming / "old-a"),
                     downloaded_at=S._iso(_dt(2026, 1, 1))),
            _tracked("org/old-b", relative_path="old-b", name="old-b",
                     path=str(self.incoming / "old-b"),
                     downloaded_at=S._iso(_dt(2026, 1, 2))),
        ]
        S.save_inventory(self.inv, S.current_path())
        sched = _sched(
            enabled=True, categories=["coding"], cleanup_enabled=True,
            cleanup_policy="keep_newest", keep_newest_n=2,
            cleanup_action="retire", max_new_per_run=3, max_total_auto=20,
        )
        allowed = []
        result = S.execute_run(
            self.cfg, "mac_16gb", sched, inventory=self.inv,
            collect_fn=self._collect(_model("org/fresh", size_gb=0.01)),
            download_fn=download, allow_fn=lambda rid: allowed.append(rid),
            dry_run=False, kind="run_now", root=self.tmp, now_dt=_dt(2026, 11, 13, 12, 0),
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(allowed, ["org/fresh"])
        self.assertTrue(dest_file.exists())
        self.assertEqual(len(result["downloaded"]), 1)
        # keep newest 2 of (old-a, old-b, fresh) → old-a retired.
        self.assertTrue(result["cleaned"])
        self.assertEqual(result["cleaned"][0]["action"], "retire")
        self.assertFalse((self.incoming / "old-a").exists())
        retired_root = W.retired_dir(self.cfg, "mac_16gb", "coding", self.tmp)
        self.assertTrue((retired_root / "old-a").exists())
        self.assertTrue((self.incoming / "old-b").exists())
        self.assertFalse(result["cleanup_skipped"])

    def test_delete_action_removes_file(self):
        folder = self.incoming / "old-a"
        folder.mkdir()
        (folder / "w.gguf").write_bytes(b"old")
        # A second model so last-usable does not block the delete.
        other = self.incoming / "keep-me"
        other.mkdir()
        (other / "w.gguf").write_bytes(b"keep")
        self.inv["tracked"] = [
            _tracked("org/old-a", relative_path="old-a", name="old-a",
                     path=str(folder), downloaded_at=S._iso(_dt(2026, 1, 1))),
            _tracked("org/keep", relative_path="keep-me", name="keep-me",
                     path=str(other), downloaded_at=S._iso(_dt(2026, 11, 1))),
        ]
        S.save_inventory(self.inv, S.current_path())
        sched = _sched(
            enabled=True, categories=["coding"], cleanup_enabled=True,
            cleanup_policy="older_than", older_than_days=10,
            cleanup_action="delete", delete_confirmed=True,
            include_top_picks=False,
        )
        # No new models, successful empty download, then cleanup.
        result = S.execute_run(
            self.cfg, "mac_16gb", sched, inventory=self.inv,
            collect_fn=self._collect(),  # no candidates
            download_fn=lambda item: (_ for _ in ()).throw(AssertionError("no download")),
            dry_run=False, root=self.tmp, now_dt=_dt(2026, 11, 13, 12, 0),
        )
        self.assertEqual(result["status"], "ok")
        self.assertFalse(folder.exists())
        self.assertTrue(other.exists())
        self.assertEqual(result["cleaned"][0]["action"], "delete")
        # Retired folder should not have received it.
        retired = W.retired_dir(self.cfg, "mac_16gb", "coding", self.tmp)
        self.assertFalse(retired.exists() and any(retired.iterdir()))

    def test_failed_download_skips_cleanup(self):
        folder = self.incoming / "old-a"
        folder.mkdir()
        (folder / "w.gguf").write_bytes(b"old")
        extra = self.incoming / "old-b"
        extra.mkdir()
        (extra / "w.gguf").write_bytes(b"old")
        self.inv["tracked"] = [
            _tracked("org/old-a", relative_path="old-a", name="old-a",
                     path=str(folder), downloaded_at=S._iso(_dt(2026, 1, 1))),
            _tracked("org/old-b", relative_path="old-b", name="old-b",
                     path=str(extra), downloaded_at=S._iso(_dt(2026, 1, 2))),
        ]
        S.save_inventory(self.inv, S.current_path())
        sched = _sched(
            cleanup_enabled=True, cleanup_policy="keep_newest", keep_newest_n=1,
            categories=["coding"], max_new_per_run=3,
        )

        def boom(item):
            raise ConnectionError("failed to establish a new connection")

        result = S.execute_run(
            self.cfg, "mac_16gb", sched, inventory=self.inv,
            collect_fn=self._collect(_model("org/fresh", size_gb=0.01)),
            download_fn=boom, dry_run=False, root=self.tmp, now_dt=_dt(2026, 11, 13),
        )
        self.assertTrue(result["cleanup_skipped"])
        self.assertTrue(folder.exists())
        self.assertTrue(extra.exists())
        self.assertEqual(result["cleaned"], [])
        self.assertTrue(result["failed"])

    def test_offline_skips_and_does_not_record_last_run(self):
        sched = _sched(categories=["coding"], cleanup_enabled=True,
                       cleanup_policy="keep_newest", keep_newest_n=1)

        def offline(**kw):
            raise ConnectionError("failed to resolve huggingface.co")

        folder = self.incoming / "old-a"
        folder.mkdir()
        (folder / "w.gguf").write_bytes(b"old")
        extra = self.incoming / "old-b"
        extra.mkdir()
        (extra / "w.gguf").write_bytes(b"x")
        self.inv["tracked"] = [
            _tracked("org/old-a", relative_path="old-a", name="old-a",
                     downloaded_at=S._iso(_dt(2026, 1, 1))),
            _tracked("org/old-b", relative_path="old-b", name="old-b",
                     downloaded_at=S._iso(_dt(2026, 1, 2))),
        ]
        result = S.execute_run(
            self.cfg, "mac_16gb", sched, inventory=self.inv,
            collect_fn=offline, dry_run=False, root=self.tmp, now_dt=_dt(2026, 11, 13, 12, 0),
        )
        self.assertEqual(result["status"], "offline")
        self.assertTrue(result["cleanup_skipped"])
        self.assertTrue(folder.exists())
        loaded = S.load_inventory(S.current_path())
        self.assertFalse(S.machine_state(loaded, "mac_16gb").get("last_run"))
        # Still due, so the next tick will retry.
        self.assertTrue(S.is_due(
            _sched(enabled=True, frequency="daily", time="03:00"),
            _dt(2026, 11, 13, 12, 0),
            {"enabled_at": S._iso(_dt(2026, 11, 10, 3, 0))},
        ))

    def test_download_in_progress_skips_the_run(self):
        sched = _sched(categories=["coding"])
        result = S.execute_run(
            self.cfg, "mac_16gb", sched, inventory=self.inv,
            collect_fn=self._collect(_model("org/fresh")),
            download_in_progress=True, dry_run=False, root=self.tmp,
        )
        self.assertEqual(result["status"], "skipped")
        self.assertTrue(result["cleanup_skipped"])

    def test_unverified_download_counts_as_failure(self):
        def empty(item):
            # File is created but empty — verification should fail.
            dest = self.incoming / "empty.gguf"
            dest.write_bytes(b"")
            return dest

        sched = _sched(categories=["coding"], cleanup_enabled=True)
        result = S.execute_run(
            self.cfg, "mac_16gb", sched, inventory=self.inv,
            collect_fn=self._collect(_model("org/fresh", size_gb=0.01)),
            download_fn=empty, dry_run=False, root=self.tmp,
        )
        self.assertTrue(result["failed"])
        self.assertTrue(result["cleanup_skipped"])
        self.assertEqual(result["downloaded"], [])

    def test_opt_in_then_cleanup_can_retire(self):
        folder = self.incoming / "manual-one"
        folder.mkdir()
        (folder / "w.gguf").write_bytes(b"manual")
        other = self.incoming / "keep-auto"
        other.mkdir()
        (other / "w.gguf").write_bytes(b"auto")
        self.inv["tracked"] = [
            _tracked("org/auto", relative_path="keep-auto", name="keep-auto",
                     downloaded_at=S._iso(_dt(2026, 11, 1))),
        ]
        S.opt_in(
            self.cfg, self.inv, machine="mac_16gb", tag="coding",
            relative_path="manual-one", root=self.tmp, now_dt=_dt(2026, 1, 1),
            repo_id="org/manual",
        )
        # Stamp it as old so older_than will pick it.
        for t in self.inv["tracked"]:
            if t["relative_path"] == "manual-one":
                t["downloaded_at"] = S._iso(_dt(2026, 1, 1))
        S.save_inventory(self.inv, S.current_path())
        sched = _sched(
            cleanup_enabled=True, cleanup_policy="older_than", older_than_days=10,
            cleanup_action="retire", categories=["coding"],
        )
        result = S.execute_run(
            self.cfg, "mac_16gb", sched, inventory=self.inv,
            collect_fn=self._collect(), dry_run=False, root=self.tmp,
            now_dt=_dt(2026, 11, 13),
        )
        self.assertFalse((self.incoming / "manual-one").exists())
        self.assertTrue(any(c["id"] == "org/manual" for c in result["cleaned"]))
        self.assertTrue((self.incoming / "keep-auto").exists())

    def test_manual_not_opted_in_is_untouched(self):
        folder = self.incoming / "manual-one"
        folder.mkdir()
        (folder / "w.gguf").write_bytes(b"manual")
        auto = self.incoming / "auto-one"
        auto.mkdir()
        (auto / "w.gguf").write_bytes(b"auto")
        self.inv["tracked"] = [
            _tracked("org/auto", relative_path="auto-one", name="auto-one",
                     downloaded_at=S._iso(_dt(2026, 1, 1))),
        ]
        S.save_inventory(self.inv, S.current_path())
        sched = _sched(
            cleanup_enabled=True, cleanup_policy="older_than", older_than_days=1,
            categories=["coding"],
        )
        # Last-usable: coding has 2 items, only auto is tracked, so auto could
        # be cleaned while leaving the manual one. That's correct — last usable
        # counts every model in the folder, including the manual one.
        result = S.execute_run(
            self.cfg, "mac_16gb", sched, inventory=self.inv,
            collect_fn=self._collect(), dry_run=False, root=self.tmp,
            now_dt=_dt(2026, 11, 13),
        )
        self.assertTrue(folder.exists())
        self.assertTrue(any(c.get("relative_path") == "auto-one" for c in result["cleaned"]))


class OfflineHelperTests(unittest.TestCase):
    def test_offline_errors(self):
        self.assertTrue(S.is_offline_error(ConnectionError("failed to resolve")))
        self.assertTrue(S.is_offline_error(TimeoutError("timed out")))
        self.assertTrue(S.is_offline_error(OSError("Network is unreachable")))
        self.assertFalse(S.is_offline_error(OSError("No space left on device (errno 28)")))
        self.assertFalse(S.is_offline_error(ValueError("unknown category")))
        self.assertFalse(S.is_offline_error(None))


class InventoryTests(unittest.TestCase):
    def test_roundtrip_and_log_cap(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            path = tmp / ".mrf_schedule.json"
            S.set_path(path)
            data = S._empty_inventory()
            for i in range(60):
                S.append_log(data, {"at": str(i), "message": "x"})
            self.assertEqual(len(data["log"]), S.LOG_LIMIT)
            S.save_inventory(data, path)
            loaded = S.load_inventory(path)
            self.assertEqual(len(loaded["log"]), S.LOG_LIMIT)
            self.assertEqual(loaded["log"][0]["at"], "10")  # first 10 dropped
        finally:
            S.set_path(None)
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
