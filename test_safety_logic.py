import sys
import types

sys.modules.setdefault("anthropic", types.SimpleNamespace(Anthropic=object))


import unittest
from unittest.mock import patch

from resolver import calc_position_shares, verify_position_closed
from risk import Position
from signals import SignalEngine, Signal


class ResolverTests(unittest.TestCase):
    def test_calc_position_shares_for_no_uses_no_price(self):
        pos = Position(
            market_id="m1",
            question="q",
            direction="NO",
            entry_price=0.80,
            size_usd=10.0,
            opened_at="2026-01-01T00:00:00+00:00",
            tags=[],
            token_id="tok",
        )
        self.assertAlmostEqual(calc_position_shares(pos), 50.0, places=3)

    @patch("resolver.fetch_polymarket_positions")
    def test_verify_position_closed_false_when_token_still_present(self, mock_fetch):
        mock_fetch.return_value = {"tok": {"size": 2.0}}
        self.assertFalse(verify_position_closed("tok", "0xabc", retries=1, wait_sec=0))

    @patch("resolver.fetch_polymarket_positions")
    def test_verify_position_closed_true_when_token_absent(self, mock_fetch):
        mock_fetch.return_value = {}
        self.assertTrue(verify_position_closed("tok", "0xabc", retries=1, wait_sec=0))


class SignalsTests(unittest.TestCase):
    def test_ai_only_signal_without_non_ai_confirmation_becomes_neutral(self):
        engine = SignalEngine.__new__(SignalEngine)
        engine.weights = {
            "sentiment": 0.34,
            "calibration": 0.33,
            "base_rate": 0.33,
            "momentum": 0.10,
            "consensus": 0.10,
            "predictit": 0.10,
            "volume_spike": 0.10,
        }

        class Market:
            yes_price = 0.55

        signals = [
            Signal("sentiment", 0.62, 0.9, "YES", "ai1", 0.34),
            Signal("calibration", 0.63, 0.9, "YES", "ai2", 0.33),
            Signal("base_rate", 0.64, 0.9, "YES", "ai3", 0.33),
        ]
        result = SignalEngine._ensemble(engine, Market(), signals)
        self.assertEqual(result.direction, "NEUTRAL")


if __name__ == "__main__":
    unittest.main()
