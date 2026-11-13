import unittest

import helpers  # noqa: F401
import watcher as W


class FormatDetectionTests(unittest.TestCase):
    def test_suffixes(self):
        self.assertEqual(W._format_of_path("model.Q4_K_M.gguf"), "gguf")
        self.assertEqual(W._format_of_path("model.safetensors"), "safetensors")
        self.assertEqual(W._format_of_path("model.bin"), "pytorch")
        self.assertEqual(W._format_of_path("model.pt"), "pytorch")
        self.assertEqual(W._format_of_path("model.onnx"), "onnx")
        self.assertEqual(W._format_of_path("model.mlmodel"), "coreml")
        self.assertEqual(W._format_of_path("weights.mlx"), "mlx")
        self.assertEqual(W._format_of_path("model.tflite"), "tflite")
        self.assertEqual(W._format_of_path("config.json"), "config")
        self.assertEqual(W._format_of_path("README.md"), "other")

    def test_guess_from_file_list(self):
        self.assertEqual(W._guess_format_from_files(["a.gguf", "b.gguf"]), "gguf")
        self.assertEqual(W._guess_format_from_files(["model.safetensors", "config.json"]), "safetensors")
        self.assertEqual(W._guess_format_from_files(["model.onnx"]), "onnx")
        self.assertEqual(W._guess_format_from_files(["Foo.mlmodel"]), "coreml")
        self.assertEqual(W._guess_format_from_files(["weights.bin"]), "pytorch")

    def test_app_notes(self):
        fmt, note = W.guess_format_and_note({"id": "someone/llama-gguf", "tags": ["gguf"]}, {"format": "gguf"})
        self.assertEqual(fmt, "gguf")
        self.assertIn("Ollama", note)
        fmt, note = W.guess_format_and_note({"id": "mlx-community/foo", "tags": []}, {"format": "mlx"})
        self.assertEqual(fmt, "mlx")
        self.assertIn("Apple", note)
        fmt, note = W.guess_format_and_note({"id": "openai/whisper-tiny", "pipeline_tag": "automatic-speech-recognition"}, {})
        self.assertIn("whisper", fmt.lower() + note.lower())
        fmt, note = W.guess_format_and_note({"id": "rhasspy/piper-en", "tags": []}, {})
        self.assertIn("Piper", note)


class CompanionTests(unittest.TestCase):
    def test_role_detection(self):
        self.assertEqual(W.companion_role("llava.mmproj-f16.gguf"), "mmproj")
        self.assertEqual(W.companion_role("model.projector.gguf"), "projector")
        self.assertEqual(W.companion_role("vision-encoder-f16.gguf"), "vision-encoder")
        self.assertEqual(W.companion_role("model.clip.gguf"), "clip")
        self.assertIsNone(W.companion_role("model.Q4_K_M.gguf"))
        self.assertTrue(W.is_sidecar("foo.mmproj.gguf"))
        self.assertFalse(W.is_sidecar("model.Q4_K_M.gguf"))

    def test_find_companions_empty_for_text_model(self):
        files = [
            {"path": "model.Q4_K_M.gguf", "size": 4_000_000_000, "size_gb": 3.7, "format": "gguf"},
            {"path": "model.Q5_K_M.gguf", "size": 5_000_000_000, "size_gb": 4.6, "format": "gguf"},
        ]
        pick = files[0]
        self.assertEqual(W.find_companions(files, pick), [])

    def test_find_companions_matches_mmproj(self):
        files = [
            {"path": "llava-v1.6.Q4_K_M.gguf", "size": 4_000_000_000, "size_gb": 3.7, "format": "gguf"},
            {"path": "llava-v1.6.Q5_K_M.gguf", "size": 5_000_000_000, "size_gb": 4.6, "format": "gguf"},
            {"path": "llava-v1.6.mmproj-f16.gguf", "size": 600_000_000, "size_gb": 0.56, "format": "gguf"},
        ]
        comps = W.find_companions(files, files[0])
        self.assertEqual(len(comps), 1)
        self.assertEqual(comps[0]["role"], "mmproj")
        self.assertIn("mmproj", comps[0]["path"])

    def test_pick_fit_does_not_choose_the_companion(self):
        files = [
            {"path": "llava.Q4_K_M.gguf", "size": 4_000_000_000, "size_gb": 3.7, "format": "gguf"},
            {"path": "llava.mmproj-f16.gguf", "size": 0.6 * 1024**3, "size_gb": 0.6, "format": "gguf"},
        ]
        pick = W.pick_fit(files, max_gb=10)
        self.assertIsNotNone(pick)
        self.assertFalse(W.is_sidecar(pick["path"]))
        comps = W.find_companions(files, pick)
        self.assertEqual(comps[0]["path"], "llava.mmproj-f16.gguf")

    def test_prefers_matching_prefix_and_f16(self):
        files = [
            {"path": "model.Q4_K_M.gguf", "size": 4, "size_gb": 4, "format": "gguf"},
            {"path": "other.mmproj-f32.gguf", "size": 1, "size_gb": 1, "format": "gguf"},
            {"path": "model.mmproj-f16.gguf", "size": 2, "size_gb": 2, "format": "gguf"},
        ]
        comps = W.find_companions(files, files[0])
        self.assertEqual(comps[0]["path"], "model.mmproj-f16.gguf")


class LicenseKeepTests(unittest.TestCase):
    def test_default_ignore_skips_only_git(self):
        self.assertEqual(W.SNAPSHOT_IGNORE_DEFAULT, [".git*"])
        self.assertFalse(any("license" in p.lower() for p in W.SNAPSHOT_IGNORE_DEFAULT))
        self.assertFalse(any("readme" in p.lower() for p in W.SNAPSHOT_IGNORE_DEFAULT))


if __name__ == "__main__":
    unittest.main()
