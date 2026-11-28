"""
Unit tests for the KISSKI LLM provider connector, including live
demand/busy status classification.

@testCovers plugins/kisski/src/pte_plugin_kisski/llm_provider.py
"""

import unittest
from unittest.mock import MagicMock, patch

from pte_plugin_kisski.llm_provider import (
    KisskiLLMProvider,
    _capabilities,
    _demand_to_availability,
)


class TestDemandToAvailability(unittest.TestCase):
    """Test the demand->availability threshold classification.

    Thresholds match the sibling zotero-rag project's
    backend/utils/kisski.py, reused here per the design doc.
    """

    def test_zero_demand_is_available(self):
        self.assertEqual(_demand_to_availability(0), "available")

    def test_one_demand_is_busy(self):
        self.assertEqual(_demand_to_availability(1), "busy")

    def test_five_demand_is_busy(self):
        self.assertEqual(_demand_to_availability(5), "busy")

    def test_six_demand_is_very_busy(self):
        self.assertEqual(_demand_to_availability(6), "very_busy")

    def test_large_demand_is_very_busy(self):
        self.assertEqual(_demand_to_availability(100), "very_busy")


class TestKisskiLLMProviderListModels(unittest.TestCase):
    """Test that list_models() populates ModelStatus from the demand field."""

    @patch("pte_plugin_kisski.llm_provider.get_retry_session")
    def test_list_models_populates_status_from_demand(self, mock_get_session):
        mock_session = MagicMock()
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "data": [
                {"id": "gemma-3-27b", "name": "Gemma 3 27B", "demand": 0},
                {"id": "llama-3-70b", "name": "Llama 3 70B", "demand": 3},
                {"id": "busy-model", "name": "Busy Model", "demand": 8},
            ]
        }
        mock_session.get.return_value = mock_response
        mock_get_session.return_value = mock_session

        provider = KisskiLLMProvider(
            id="kisski", label="KISSKI", base_url="https://chat-ai.academiccloud.de/v1", api_key="k"
        )
        models = provider.list_models()

        self.assertEqual(len(models), 3)
        self.assertEqual(models[0]["status"]["availability"], "available")
        self.assertEqual(models[0]["status"]["detail"], "demand: 0")
        self.assertEqual(models[1]["status"]["availability"], "busy")
        self.assertEqual(models[2]["status"]["availability"], "very_busy")
        self.assertTrue(all(m["free"] for m in models))

    @patch("pte_plugin_kisski.llm_provider.get_retry_session")
    def test_list_models_defaults_missing_demand_to_zero(self, mock_get_session):
        mock_session = MagicMock()
        mock_response = MagicMock()
        mock_response.json.return_value = {"data": [{"id": "m", "name": "M"}]}
        mock_session.get.return_value = mock_response
        mock_get_session.return_value = mock_session

        provider = KisskiLLMProvider(id="kisski", label="KISSKI", base_url="https://x/v1", api_key="k")
        models = provider.list_models()

        self.assertEqual(models[0]["status"]["availability"], "available")


class TestKisskiLLMProviderSharedKey(unittest.TestCase):
    """Test that the shared key is read from config on each use, not frozen at registration."""

    def _provider(self):
        return KisskiLLMProvider(id="kisski", label="KISSKI", base_url="https://x/v1", api_key=None, secret_service="kisski")

    @patch("pte_plugin_kisski.llm_provider.get_config")
    def test_shared_key_set_after_registration_is_used(self, mock_get_config):
        values = {"plugin.kisski.api.key": None}
        mock_get_config.return_value.get.side_effect = lambda key, *a, **kw: values.get(key)
        provider = self._provider()
        self.assertFalse(provider.has_shared_key())

        values["plugin.kisski.api.key"] = "admin-key"

        self.assertTrue(provider.has_shared_key())
        self.assertEqual(provider._api_key, "admin-key")

    @patch("pte_plugin_kisski.llm_provider.get_config")
    def test_shared_key_removed_after_registration_is_dropped(self, mock_get_config):
        values = {"plugin.kisski.api.key": "admin-key"}
        mock_get_config.return_value.get.side_effect = lambda key, *a, **kw: values.get(key)
        provider = self._provider()
        self.assertTrue(provider.has_shared_key())

        values["plugin.kisski.api.key"] = ""

        self.assertFalse(provider.has_shared_key())

    @patch("pte_plugin_kisski.llm_provider.get_config")
    def test_user_key_takes_precedence_over_shared_key(self, mock_get_config):
        mock_get_config.return_value.get.side_effect = lambda key, *a, **kw: "admin-key"

        user_provider = self._provider().with_api_key("user-key")

        self.assertEqual(user_provider._api_key, "user-key")
        self.assertEqual(user_provider.secret_service, "kisski")



class TestCapabilities(unittest.TestCase):
    """Test the vision capability read from the `input` modalities of /models."""

    def test_image_input_is_vision(self):
        self.assertEqual(_capabilities({"id": "m", "input": ["text", "image"]}), frozenset({"chat", "vision"}))

    def test_text_input_is_chat_only(self):
        self.assertEqual(_capabilities({"id": "m", "input": ["text"]}), frozenset({"chat"}))

    def test_missing_input_is_chat_only(self):
        self.assertEqual(_capabilities({"id": "m"}), frozenset({"chat"}))


if __name__ == "__main__":
    unittest.main()
