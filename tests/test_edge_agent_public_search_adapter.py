from __future__ import annotations

import os
import sys
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

from edge_agent_delegation import public_search_capability_available  # noqa: E402
from edge_agent_public_search_adapter import render_results, search  # noqa: E402


class PublicSearchAdapterTests(unittest.TestCase):
    def test_parses_observed_urls_and_does_not_invent_empty_results(self):
        body = b"""
        <a class='result__a' href='https://example.com/resume'>Resume Form</a>
        <div class='result__snippet'>Official resume template</div>
        <a class='result__a' href='https://example.com/resume#fragment'>Resume Form</a>
        """
        class Response(BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.close()

        response = Response(body)
        with patch("edge_agent_public_search_adapter.urlopen", return_value=response):
            results = search("온라인 이력서 양식")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].url, "https://example.com/resume")
        self.assertIn("example.com/resume", render_results("이력서", results))

    def test_capability_requires_explicit_contract_and_adapter_file(self):
        with patch.dict(os.environ, {
            "EDGE_AGENT_PUBLIC_SEARCH_ENABLED": "1",
            "EDGE_AGENT_PUBLIC_SEARCH_ADAPTER": "verified-public-search-v1",
        }):
            self.assertTrue(public_search_capability_available())
        with patch.dict(os.environ, {
            "EDGE_AGENT_PUBLIC_SEARCH_ENABLED": "0",
            "EDGE_AGENT_PUBLIC_SEARCH_ADAPTER": "verified-public-search-v1",
        }):
            self.assertFalse(public_search_capability_available())


if __name__ == "__main__":
    unittest.main()
