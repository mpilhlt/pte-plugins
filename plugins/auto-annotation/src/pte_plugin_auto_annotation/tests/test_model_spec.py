"""
@testCovers plugins/auto-annotation/src/pte_plugin_auto_annotation/model_spec.py
"""

import unittest

from pte_plugin_auto_annotation.model_spec import ModelSpecError, ProviderInfo, resolve_model_spec

PROVIDERS = [
    ProviderInfo("kisski", "KISSKI", {"llama-3.3-70b-instruct": "Llama 3.3 70B", "qwen3-32b": "Qwen 3 32B"}),
    ProviderInfo("endpoint-local-ollama", "Local Ollama", {"qwen2.5:14b": "qwen2.5:14b"}),
    ProviderInfo("endpoint-router", "Router", {"meta-llama/llama-3-8b": "Llama 3 8B"}),
    ProviderInfo("endpoint-unlisted", "Unlisted", {}),
]


class TestResolveModelSpec(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(resolve_model_spec("kisski/qwen3-32b", PROVIDERS), ("kisski", "qwen3-32b"))
        self.assertEqual(resolve_model_spec("KISSKI/Llama 3.3 70B", PROVIDERS), ("kisski", "llama-3.3-70b-instruct"))
        self.assertEqual(resolve_model_spec("Local Ollama", PROVIDERS), ("endpoint-local-ollama", "qwen2.5:14b"))
        self.assertEqual(resolve_model_spec("local-ollama/qwen2.5:14b", PROVIDERS), ("endpoint-local-ollama", "qwen2.5:14b"))
        self.assertEqual(resolve_model_spec("router/meta-llama/llama-3-8b", PROVIDERS), ("endpoint-router", "meta-llama/llama-3-8b"))
        self.assertEqual(resolve_model_spec("unlisted/any-model", PROVIDERS), ("endpoint-unlisted", "any-model"))

    def test_default(self):
        self.assertEqual(resolve_model_spec("default", PROVIDERS, ("kisski", "qwen3-32b")), ("kisski", "qwen3-32b"))
        self.assertEqual(resolve_model_spec("kisski", PROVIDERS, ("kisski", "qwen3-32b")), ("kisski", "qwen3-32b"))
        with self.assertRaises(ModelSpecError):
            resolve_model_spec("default", PROVIDERS)

    def test_errors(self):
        for spec in ("", "nope", "kisski", "kisski/gpt-9"):
            with self.subTest(spec=spec), self.assertRaises(ModelSpecError):
                resolve_model_spec(spec, PROVIDERS)

    def test_ambiguous_label(self):
        providers = [ProviderInfo("a", "Same", {"m": "m"}), ProviderInfo("b", "Same", {"m": "m"})]
        with self.assertRaises(ModelSpecError):
            resolve_model_spec("same/m", providers)
        self.assertEqual(resolve_model_spec("b/m", providers), ("b", "m"))


if __name__ == "__main__":
    unittest.main()
