import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from ai.gemini_analyzer import analyze_job_with_fallback


class GeminiAnalyzerTests(unittest.TestCase):
    def test_fallback_on_rate_limit(self):
        async def run_test():
            with patch("ai.gemini_analyzer.get_client") as mock_get_client:
                client = AsyncMock()
                client.models.generate_content.side_effect = [
                    RuntimeError("429 Too Many Requests"),
                    {"fit_score": 7, "summary_bullets": ["ok"], "match_reason": "ok", "risk_score": 20, "risk_warnings": ["ok"], "should_notify": True},
                ]
                mock_get_client.return_value = client

                result = await analyze_job_with_fallback("prompt", None)
                self.assertIsNotNone(result)
                self.assertEqual(result.fit_score, 7)

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()
