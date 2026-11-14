"""v1.7 hardware detection: parsers, size limits and the detected profile. No real probing."""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import helpers  # noqa: F401 - sets sys.path
import hardware as H
import watcher as W


class NumberTests(unittest.TestCase):
    def test_nominal_sizes_snap_to_box_size(self):
        self.assertEqual(H.nominal_gb(31.7), 32.0)
        self.assertEqual(H.nominal_gb(11.99), 12.0)
        self.assertEqual(H.nominal_gb(7.8), 8.0)
        self.assertEqual(H.nominal_gb(15.6), 16.0)
        self.assertIsNone(H.nominal_gb(0))
        self.assertIsNone(H.nominal_gb("nope"))

    def test_gb_label(self):
        self.assertEqual(H.gb_label(12.0), "12 GB")
        self.assertEqual(H.gb_label(9.5), "9.5 GB")
        self.assertEqual(H.gb_label(None), "unknown")

    def test_suggested_caps(self):
        self.assertEqual(H.suggest_cap("gpu", 12), 9.5)
        self.assertEqual(H.suggest_cap("gpu", 8), 6.5)
        self.assertEqual(H.suggest_cap("gpu", 24), 19.0)
        self.assertEqual(H.suggest_cap("apple", 16), 10.0)
        self.assertEqual(H.suggest_cap("apple", 8), 5.0)
        self.assertEqual(H.suggest_cap("cpu", 16), 6.5)
        self.assertEqual(H.suggest_cap("cpu", 2), 1.0)  # never below the minimum
        self.assertIsNone(H.suggest_cap("gpu", None))


class AdapterTests(unittest.TestCase):
    def test_vendors(self):
        self.assertEqual(H.vendor_of("NVIDIA GeForce RTX 4070"), "nvidia")
        self.assertEqual(H.vendor_of("AMD Radeon RX 7800 XT"), "amd")
        self.assertEqual(H.vendor_of("Intel(R) UHD Graphics 770"), "intel")
        self.assertEqual(H.vendor_of("Intel(R) Arc(TM) A770 Graphics"), "intel")

    def test_virtual_adapters_ignored(self):
        self.assertTrue(H.is_virtual_adapter("Microsoft Basic Display Adapter"))
        self.assertTrue(H.is_virtual_adapter("Microsoft Remote Display Adapter"))
        self.assertFalse(H.is_virtual_adapter("NVIDIA GeForce RTX 3060"))

    def test_dedicated_rules(self):
        self.assertTrue(H.looks_dedicated("NVIDIA GeForce RTX 3060", 12))
        self.assertFalse(H.looks_dedicated("NVIDIA GeForce MX150", 2))
        self.assertFalse(H.looks_dedicated("Intel(R) UHD Graphics", 8))
        self.assertTrue(H.looks_dedicated("Intel(R) Arc(TM) A770 Graphics", 16))
        self.assertTrue(H.looks_dedicated("AMD Radeon RX 6700 XT", 12))


class ParserTests(unittest.TestCase):
    def test_nvidia_smi(self):
        gpus = H.parse_nvidia_smi("NVIDIA GeForce RTX 4070, 12282\nNVIDIA GeForce RTX 3090, 24576 MiB\n\n")
        self.assertEqual([g["vram_gb"] for g in gpus], [12.0, 24.0])
        self.assertTrue(all(g["dedicated"] for g in gpus))

    def test_cim_json_single_and_list(self):
        one = json.dumps({"Name": "NVIDIA GeForce RTX 3060", "AdapterRAM": 4293918720})
        gpus = H.parse_cim_json(one)
        self.assertEqual(len(gpus), 1)
        self.assertTrue(gpus[0]["vram_at_least"])  # the 4 GB ceiling is flagged
        many = json.dumps([
            {"Name": "Intel(R) UHD Graphics", "AdapterRAM": 1073741824},
            {"Name": "Microsoft Basic Display Adapter", "AdapterRAM": 0},
        ])
        names = [g["name"] for g in H.parse_cim_json(many)]
        self.assertEqual(names, ["Intel(R) UHD Graphics"])  # the virtual adapter is skipped
        self.assertEqual(H.parse_cim_json("not json"), [])

    def test_system_profiler(self):
        text = json.dumps({"SPDisplaysDataType": [
            {"sppci_model": "AMD Radeon Pro 5500M", "spdisplays_vram": "8 GB"},
            {"sppci_model": "Intel UHD Graphics 630", "spdisplays_vram": "1536 MB"},
        ]})
        gpus = H.parse_system_profiler(text)
        self.assertEqual(gpus[0]["vram_gb"], 8.0)
        self.assertTrue(gpus[0]["dedicated"])
        self.assertAlmostEqual(gpus[1]["vram_gb"], 1.5)
        self.assertEqual(H.parse_system_profiler("{"), [])

    def test_meminfo(self):
        self.assertAlmostEqual(H.parse_meminfo("MemTotal:       16303916 kB\nMemFree: 1 kB\n"), 15.548, places=2)
        self.assertIsNone(H.parse_meminfo(""))

    def test_rocm_smi(self):
        text = json.dumps({"card0": {"Card series": "Radeon RX 7900 XTX", "VRAM Total Memory (B)": "25753026560"}})
        gpus = H.parse_rocm_smi(text)
        self.assertEqual(gpus[0]["vram_gb"], 24.0)


class ProfileTests(unittest.TestCase):
    def test_gpu_profile(self):
        raw = {"ram_gb": 31.8, "gpus": [
            H._gpu("Intel(R) UHD Graphics 770", 1, "test"),
            H._gpu("NVIDIA GeForce RTX 4070", 11.99, "test"),
        ]}
        p = H.build_profile(raw, system="Windows")
        self.assertEqual(p["kind"], "gpu")
        self.assertEqual(p["suggested_max_model_gb"], 9.5)
        self.assertEqual(p["ram_gb"], 32.0)
        self.assertIn("RTX 4070", p["summary"])
        self.assertIn("12 GB graphics memory", p["summary"])
        self.assertIn("32 GB system memory", p["summary"])
        self.assertEqual(p["formats"], ["gguf"])

    def test_multi_gpu_uses_largest_and_says_so(self):
        raw = {"ram_gb": 64, "gpus": [
            H._gpu("NVIDIA GeForce RTX 3060", 12, "t"),
            H._gpu("NVIDIA GeForce RTX 3090", 24, "t"),
        ]}
        p = H.build_profile(raw, system="Linux")
        self.assertEqual(p["best_gpu"]["vram_gb"], 24.0)
        self.assertTrue(any("2 graphics cards" in n for n in p["notes"]))

    def test_apple_profile(self):
        p = H.build_profile({"ram_gb": 16, "apple_silicon": True, "chip": "Apple M2"}, system="Darwin")
        self.assertEqual(p["kind"], "apple")
        self.assertEqual(p["suggested_max_model_gb"], 10.0)
        self.assertEqual(p["formats"], ["gguf", "mlx"])
        self.assertIn("unified memory", p["summary"])

    def test_cpu_only_and_unknown(self):
        p = H.build_profile({"ram_gb": 16, "gpus": [H._gpu("Intel(R) UHD Graphics", 2, "t")]}, system="Linux")
        self.assertEqual(p["kind"], "cpu")
        self.assertEqual(p["suggested_max_model_gb"], 6.5)
        empty = H.build_profile({}, system="Linux")
        self.assertFalse(empty["ok"])
        self.assertIsNone(empty["suggested_max_model_gb"])

    def test_detect_never_raises(self):
        with patch.object(H, "detect_linux", side_effect=RuntimeError("boom")), \
                patch.object(H, "detect_nvidia", return_value=[]):
            p = H.detect("Linux")
        self.assertIn("summary", p)
        self.assertFalse(p["ok"])

    def test_no_personal_paths_in_profile(self):
        p = H.build_profile({"ram_gb": 16, "gpus": []}, system="Linux")
        blob = json.dumps(p)
        self.assertNotIn("\\Users\\", blob)
        self.assertNotIn("/home/", blob)


class DetectedMachineTests(unittest.TestCase):
    def setUp(self):
        self.cfg = helpers.sample_config()

    def test_defaults_put_detected_profile_first(self):
        keys = list(self.cfg["machines"].keys())
        self.assertEqual(keys[0], W.DETECTED_MACHINE)
        for key in ("pc_8gb_gpu", "mac_8gb", "pc_24gb_gpu"):
            self.assertIn(key, keys)
        self.assertNotEqual(self.cfg["machines"]["pc_24gb_gpu"].get("enabled"), False)
        for machine in self.cfg["machines"].values():
            self.assertIn("science", machine["hf_categories"])
            self.assertIn("science", machine["github_categories"])
        self.assertIn("science", self.cfg["tags"])
        self.assertIn("science", self.cfg["github_tags"])

    def test_apply_detected(self):
        W.apply_detected_hardware(self.cfg, helpers.fake_hardware("gpu"))
        m = self.cfg["machines"][W.DETECTED_MACHINE]
        self.assertEqual(m["max_model_gb"], 9.5)
        self.assertEqual(m["cap_source"], "detected")
        self.assertEqual(m["vram_gb"], 12.0)
        self.assertEqual(m["formats"], ["gguf"])

    def test_apply_apple_gets_mlx(self):
        W.apply_detected_hardware(self.cfg, helpers.fake_hardware("apple"))
        m = self.cfg["machines"][W.DETECTED_MACHINE]
        self.assertEqual(m["formats"], ["gguf", "mlx"])
        self.assertEqual(m["unified_gb"], 16.0)

    def test_override_wins_and_reapply_is_clean(self):
        self.cfg["machines"][W.DETECTED_MACHINE]["max_model_gb_override"] = 11
        W.apply_detected_hardware(self.cfg, helpers.fake_hardware("gpu"))
        m = self.cfg["machines"][W.DETECTED_MACHINE]
        self.assertEqual(m["max_model_gb"], 11.0)
        self.assertEqual(m["cap_source"], "yours")
        self.assertEqual(m["suggested_max_model_gb"], 9.5)
        # Switching hardware (e.g. detect again on a Mac) replaces runtime values.
        m["max_model_gb_override"] = None
        W.apply_detected_hardware(self.cfg, helpers.fake_hardware("apple"))
        self.assertEqual(m["max_model_gb"], 10.0)
        self.assertNotIn("vram_gb", m)

    def test_fallback_when_nothing_detected(self):
        W.apply_detected_hardware(self.cfg, {})
        m = self.cfg["machines"][W.DETECTED_MACHINE]
        self.assertEqual(m["max_model_gb"], W.FALLBACK_CAP_GB)
        self.assertEqual(m["cap_source"], "fallback")

    def test_hardware_never_saved(self):
        W.apply_detected_hardware(self.cfg, helpers.fake_hardware("gpu"))
        tmp = Path(tempfile.mkdtemp())
        path = tmp / "machines.yaml"
        W.save_config(path, self.cfg)
        text = path.read_text(encoding="utf-8")
        self.assertNotIn("RTX 4070", text)
        self.assertNotIn(W.RUNTIME_KEYS_FIELD, text)
        saved = W.load_config(path)["machines"][W.DETECTED_MACHINE]
        self.assertNotIn("hardware", saved)
        self.assertNotIn("max_model_gb", saved)
        self.assertNotIn("cap_source", saved)
        # The live config still has them.
        self.assertIn("hardware", self.cfg["machines"][W.DETECTED_MACHINE])

    def test_suggest_machine_prefers_detected(self):
        W.apply_detected_hardware(self.cfg, helpers.fake_hardware("gpu"))
        self.assertEqual(W.suggest_machine(self.cfg), W.DETECTED_MACHINE)


class MigrationTests(unittest.TestCase):
    def test_v162_settings_upgrade(self):
        cfg = helpers.sample_config()
        old = copy.deepcopy(cfg)
        for key in W.V17_MACHINES:
            old["machines"].pop(key, None)
        old.pop("config_version", None)
        for machine in old["machines"].values():
            machine["hf_categories"] = [c for c in machine["hf_categories"] if c != "science"]
            machine["github_categories"] = [c for c in machine["github_categories"] if c != "science"]
        old["machines"]["pc_16gb_gpu"]["hf_categories"] = ["coding"]  # the user's own choice
        old["tags"].pop("science")
        old["github_tags"].pop("science")
        changed = W.migrate_config(old, helpers.REPO / "machines.yaml")
        self.assertTrue(changed)
        keys = list(old["machines"].keys())
        self.assertEqual(keys[0], W.DETECTED_MACHINE)
        self.assertIn("pc_8gb_gpu", keys)
        self.assertIn("mac_8gb", keys)
        self.assertEqual(old["config_version"], W.CONFIG_VERSION)
        self.assertIn("science", old["tags"])
        self.assertEqual(old["machines"]["pc_16gb_gpu"]["hf_categories"], ["coding", "science"])
        # Running it again changes nothing further.
        snapshot = copy.deepcopy(old)
        W.migrate_config(old, helpers.REPO / "machines.yaml")
        self.assertEqual(old["machines"], snapshot["machines"])


if __name__ == "__main__":
    unittest.main()
