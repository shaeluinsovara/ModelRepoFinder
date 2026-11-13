import unittest

import helpers  # noqa: F401  — puts the repo on sys.path
import watcher as W


class ParseHfInputTests(unittest.TestCase):
    def test_org_name(self):
        self.assertEqual(W.parse_hf_input("openai/whisper-tiny"), "openai/whisper-tiny")

    def test_strips_quotes_and_space(self):
        self.assertEqual(W.parse_hf_input('  "openai/whisper-tiny"  '), "openai/whisper-tiny")

    def test_https_url(self):
        self.assertEqual(
            W.parse_hf_input("https://huggingface.co/openai/whisper-tiny"),
            "openai/whisper-tiny",
        )

    def test_http_and_www(self):
        self.assertEqual(
            W.parse_hf_input("http://www.huggingface.co/openai/whisper-tiny"),
            "openai/whisper-tiny",
        )

    def test_hf_co_and_no_scheme(self):
        self.assertEqual(W.parse_hf_input("hf.co/openai/whisper-tiny"), "openai/whisper-tiny")
        self.assertEqual(
            W.parse_hf_input("huggingface.co/TheBloke/Llama-2-7B-GGUF"),
            "TheBloke/Llama-2-7B-GGUF",
        )

    def test_tree_and_blob_links(self):
        self.assertEqual(
            W.parse_hf_input("https://huggingface.co/openai/whisper-tiny/tree/main"),
            "openai/whisper-tiny",
        )
        self.assertEqual(
            W.parse_hf_input("https://huggingface.co/openai/whisper-tiny/blob/main/README.md"),
            "openai/whisper-tiny",
        )

    def test_models_prefix(self):
        self.assertEqual(
            W.parse_hf_input("https://huggingface.co/models/openai/whisper-tiny"),
            "openai/whisper-tiny",
        )
        self.assertEqual(W.parse_hf_input("models/openai/whisper-tiny"), "openai/whisper-tiny")

    def test_rejects_datasets_and_spaces(self):
        with self.assertRaises(ValueError) as ctx:
            W.parse_hf_input("https://huggingface.co/datasets/org/name")
        self.assertIn("dataset", str(ctx.exception).lower())
        with self.assertRaises(ValueError) as ctx:
            W.parse_hf_input("https://huggingface.co/spaces/org/name")
        self.assertIn("space", str(ctx.exception).lower())

    def test_rejects_site_pages(self):
        with self.assertRaises(ValueError):
            W.parse_hf_input("https://huggingface.co/login")
        with self.assertRaises(ValueError):
            W.parse_hf_input("https://huggingface.co/docs/hub")

    def test_rejects_wrong_host(self):
        with self.assertRaises(ValueError):
            W.parse_hf_input("https://example.com/openai/whisper-tiny")

    def test_rejects_empty_and_short(self):
        with self.assertRaises(ValueError):
            W.parse_hf_input("")
        with self.assertRaises(ValueError):
            W.parse_hf_input("onlyname")

    def test_rejects_bad_characters(self):
        with self.assertRaises(ValueError):
            W.parse_hf_input("openai/whisper tiny")
        with self.assertRaises(ValueError):
            W.parse_hf_input("openai/../../../etc")


if __name__ == "__main__":
    unittest.main()
