from __future__ import annotations

import unittest
from pathlib import Path

import debate


class ShippedDefaultsTest(unittest.TestCase):
    def test_profile_is_explicit_autonomous_and_unlimited(self):
        repository = Path(__file__).resolve().parents[1]
        settings = debate.load_settings(repository / "config.ini", None)

        self.assertEqual(settings.model, "gpt-5.6-sol")
        self.assertEqual(settings.reasoning_effort, "max")
        self.assertEqual(settings.web_search, "live")
        self.assertEqual(settings.min_counter_rounds, 4)
        self.assertEqual(settings.max_counter_rounds, 10)

        self.assertEqual(settings.adjudication_mode, "model")
        self.assertEqual(settings.adjudicator_model, "gpt-5.5")
        self.assertNotEqual(settings.adjudicator_model, settings.model)
        self.assertEqual(settings.adjudicator_reasoning_effort, "xhigh")

        self.assertEqual(settings.max_model_calls, 0)
        self.assertEqual(settings.max_wall_minutes, 0)
        self.assertEqual(settings.max_total_tokens, 0)
        self.assertEqual(settings.max_estimated_cost_usd, 0)
        self.assertGreater(settings.input_usd_per_million, 0)
        self.assertGreater(settings.cached_input_usd_per_million, 0)
        self.assertGreater(settings.output_usd_per_million, 0)

        self.assertEqual(
            (repository / "VERSION").read_text(encoding="utf-8").strip(),
            debate.VERSION,
        )


if __name__ == "__main__":
    unittest.main()
