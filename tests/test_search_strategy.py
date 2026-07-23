import unittest

from parsers.search_service import normalize_search_strategy


class SearchStrategyTests(unittest.TestCase):
    def test_normalize_search_strategy_uses_ai_payload(self) -> None:
        payload = {
            "sources": [
                {"name": "DOU", "category": "Technical Support", "keywords": ["technical support", "support specialist"]},
                {"name": "Djinni", "category": "project-manager", "keywords": ["project manager", "delivery manager"]},
            ],
            "fallback_keywords": ["customer support", "service desk"],
            "search_terms": ["service desk", "support manager"],
        }

        strategy = normalize_search_strategy(payload)

        self.assertEqual(strategy["sources"][0]["name"], "DOU")
        self.assertEqual(strategy["sources"][0]["category"], "Technical Support")
        self.assertEqual(strategy["fallback_keywords"], ["customer support", "service desk"])
        self.assertEqual(strategy["search_terms"], ["service desk", "support manager"])

    def test_normalize_search_strategy_falls_back_to_defaults(self) -> None:
        strategy = normalize_search_strategy(None)

        self.assertIn("sources", strategy)
        self.assertTrue(strategy["sources"])
        self.assertTrue(strategy["fallback_keywords"])


if __name__ == "__main__":
    unittest.main()
