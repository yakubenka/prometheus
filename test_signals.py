"""
Тесты для signals.py
Покрывает: классификацию вопросов, ensemble логику, quality scoring,
           strategy typing, external support counting, weight updates.
Без реальных HTTP запросов и без Anthropic API.
"""
import sys
import types
from unittest.mock import MagicMock

# Мокаем внешние модули до импорта чего-либо из проекта
sys.modules.setdefault("anthropic", types.SimpleNamespace(Anthropic=object))
if "requests" not in sys.modules:
    _mock_requests = MagicMock()
    _mock_requests.Session.return_value = MagicMock()
    sys.modules["requests"] = _mock_requests
    sys.modules["requests.adapters"] = MagicMock()
    sys.modules["requests.packages"] = MagicMock()

from dataclasses import dataclass, field
from typing import Optional

import pytest

from signals import (
    SignalEngine,
    Signal,
    EnsembleResult,
    _classify_question,
    get_domain_min_edge,
    get_confidence_kelly_mult,
)


# ── Вспомогательные объекты ────────────────────────────────────────────────────

@dataclass
class FakeMarket:
    """Минимальный заменитель Market для тестов."""
    id:          str   = "test-market-001"
    question:    str   = "Will this resolve YES?"
    yes_price:   float = 0.50
    no_price:    float = 0.50
    volume_24h:  float = 50_000.0
    spread:      float = 0.02
    end_date:    str   = "2026-12-31T00:00:00Z"
    description: str   = ""
    tags:        tuple = ()


def _make_engine() -> SignalEngine:
    """Создаёт SignalEngine без реального AI и HTTP."""
    engine = SignalEngine.__new__(SignalEngine)
    engine.ai      = None
    engine.intel   = None
    engine.weights = dict(SignalEngine.DEFAULT_WEIGHTS)
    engine._ai_budget_remaining = 0
    return engine


def _make_result(
    edge:       float = 0.10,
    direction:  str   = "YES",
    confidence: str   = "medium",
    prob:       float = 0.60,
) -> EnsembleResult:
    return EnsembleResult(
        final_score    = prob,
        direction      = direction,
        edge           = edge,
        ai_probability = prob,
        confidence     = confidence,
    )


# ── _classify_question ─────────────────────────────────────────────────────────

class TestClassifyQuestion:
    def test_electoral_keywords(self):
        assert _classify_question("Will Trump win the election?")       == "electoral"
        assert _classify_question("Will the senate vote on the bill?")  == "electoral"
        assert _classify_question("Will the president resign?")         == "electoral"

    def test_economic_keywords(self):
        assert _classify_question("Will the Fed raise rates in 2026?")  == "economic"
        assert _classify_question("Will CPI exceed 3% this year?")      == "economic"
        assert _classify_question("Will GDP contract in Q3?")           == "economic"

    def test_geopolitical_keywords(self):
        assert _classify_question("Will Russia attack NATO?")           == "geopolitical"
        assert _classify_question("Will Iran escalate with Israel?")    == "geopolitical"
        assert _classify_question("Will China invade Taiwan?")          == "geopolitical"

    def test_crypto_keywords(self):
        assert _classify_question("Will Bitcoin reach $200k?")          == "crypto"
        assert _classify_question("Will ETH flip BTC by year end?")     == "crypto"
        assert _classify_question("Will Solana hit $500?")              == "crypto"

    def test_binary_catch_all(self):
        # "will" triggers the binary catch-all before "general"
        assert _classify_question("Will it rain in Paris?")             == "binary"

    def test_general_default(self):
        assert _classify_question("Mars colonization mission 2030")     == "general"

    def test_empty_string(self):
        assert _classify_question("") == "general"


# ── get_domain_min_edge ────────────────────────────────────────────────────────

class TestGetDomainMinEdge:
    def test_crypto_highest(self):
        assert get_domain_min_edge(["crypto"]) == 0.10

    def test_politics_lowest(self):
        assert get_domain_min_edge(["politics"]) == 0.04

    def test_unknown_tag_returns_default(self):
        assert get_domain_min_edge(["foobar"], default=0.07) == 0.07

    def test_empty_tags_returns_default(self):
        assert get_domain_min_edge([], default=0.05) == 0.05

    def test_first_matching_tag_wins(self):
        # crypto (0.10) before sports (0.08)
        assert get_domain_min_edge(["crypto", "sports"]) == 0.10


# ── get_confidence_kelly_mult ──────────────────────────────────────────────────

class TestGetConfidenceKellyMult:
    def test_high_is_full(self):
        assert get_confidence_kelly_mult("high") == 1.00

    def test_medium_reduced(self):
        assert get_confidence_kelly_mult("medium") == 0.70

    def test_low_further_reduced(self):
        assert get_confidence_kelly_mult("low") == 0.40

    def test_unknown_fallback(self):
        assert get_confidence_kelly_mult("nonsense") == 0.50


# ── SignalEngine._ensemble ─────────────────────────────────────────────────────

class TestEnsemble:
    def test_yes_when_prob_above_market(self):
        engine = _make_engine()
        market = FakeMarket(yes_price=0.35)
        signals = [
            Signal("momentum",  0.65, 0.70, "YES", "up 10%/24h",       0.20),
            Signal("consensus", 0.62, 0.80, "YES", "cheap vs Kalshi",   0.34),
        ]
        result = engine._ensemble(market, signals)
        assert result.direction == "YES"
        assert result.edge > 0

    def test_no_when_prob_below_market(self):
        engine = _make_engine()
        market = FakeMarket(yes_price=0.75)
        signals = [
            Signal("momentum",  0.40, 0.70, "NO", "down 15%/24h",      0.20),
            Signal("consensus", 0.42, 0.80, "NO", "rich vs Kalshi",     0.34),
        ]
        result = engine._ensemble(market, signals)
        assert result.direction == "NO"

    def test_neutral_when_prob_equals_market(self):
        engine = _make_engine()
        market = FakeMarket(yes_price=0.50)
        signals = [
            Signal("momentum",  0.50, 0.30, "NEUTRAL", "flat", 0.20),
            Signal("consensus", 0.50, 0.30, "NEUTRAL", "aligned", 0.34),
        ]
        result = engine._ensemble(market, signals)
        assert result.direction == "NEUTRAL"

    def test_ai_only_setup_forces_neutral(self):
        """Если только AI-сигналы без non-AI поддержки — NEUTRAL."""
        engine = _make_engine()
        market = FakeMarket(yes_price=0.40)
        signals = [
            # Только AI сигналы (sentiment, calibration, base_rate)
            Signal("sentiment",   0.70, 0.80, "YES", "positive",  0.06),
            Signal("calibration", 0.68, 0.75, "YES", "calibrated", 0.04),
            Signal("base_rate",   0.65, 0.70, "YES", "base",       0.04),
        ]
        result = engine._ensemble(market, signals)
        assert result.direction == "NEUTRAL"

    def test_edge_is_abs_prob_minus_market_price(self):
        engine = _make_engine()
        market = FakeMarket(yes_price=0.40)
        signals = [Signal("momentum", 0.65, 0.80, "YES", "up", 0.30)]
        result = engine._ensemble(market, signals)
        assert abs(result.edge - abs(result.ai_probability - 0.40)) < 0.001

    def test_confidence_high_when_strong_signals(self):
        engine = _make_engine()
        market = FakeMarket(yes_price=0.25)
        signals = [
            Signal("momentum",  0.75, 0.90, "YES", "up 20%",     0.20),
            Signal("consensus", 0.73, 0.90, "YES", "big gap",    0.34),
            Signal("predictit", 0.70, 0.85, "YES", "arb signal", 0.24),
        ]
        result = engine._ensemble(market, signals)
        # edge > 0.12, external support >= 1 → high или medium
        assert result.confidence in ("high", "medium")

    def test_confidence_low_when_weak_edge(self):
        engine = _make_engine()
        market = FakeMarket(yes_price=0.50)
        signals = [Signal("momentum", 0.51, 0.10, "YES", "barely", 0.20)]
        result = engine._ensemble(market, signals)
        assert result.confidence == "low"

    def test_empty_signals_returns_neutral(self):
        engine = _make_engine()
        result = engine._ensemble(FakeMarket(), [])
        assert result.direction == "NEUTRAL"


# ── SignalEngine._external_support_count ──────────────────────────────────────

class TestExternalSupportCount:
    def test_counts_high_confidence_consensus(self):
        engine = _make_engine()
        signals = [Signal("consensus", 0.65, 0.50, "YES", "cheap", 0.34)]
        assert engine._external_support_count("YES", signals) == 1

    def test_ignores_low_confidence(self):
        engine = _make_engine()
        # confidence 0.20 < 0.35 threshold
        signals = [Signal("consensus", 0.65, 0.20, "YES", "weak", 0.34)]
        assert engine._external_support_count("YES", signals) == 0

    def test_ignores_opposite_direction(self):
        engine = _make_engine()
        signals = [Signal("consensus", 0.35, 0.80, "NO", "rich", 0.34)]
        assert engine._external_support_count("YES", signals) == 0

    def test_neutral_direction_always_zero(self):
        engine = _make_engine()
        signals = [Signal("consensus", 0.65, 0.90, "YES", "big gap", 0.34)]
        assert engine._external_support_count("NEUTRAL", signals) == 0

    def test_counts_both_consensus_and_predictit(self):
        engine = _make_engine()
        signals = [
            Signal("consensus", 0.65, 0.50, "YES", "cheap",  0.34),
            Signal("predictit", 0.67, 0.55, "YES", "arb",    0.24),
        ]
        assert engine._external_support_count("YES", signals) == 2

    def test_ignores_non_external_signals(self):
        engine = _make_engine()
        # momentum не считается external support
        signals = [Signal("momentum", 0.65, 0.80, "YES", "up", 0.20)]
        assert engine._external_support_count("YES", signals) == 0


# ── SignalEngine._strategy_type ────────────────────────────────────────────────

class TestStrategyType:
    def test_cross_market_gap_with_external_support(self):
        engine = _make_engine()
        signals = [Signal("consensus", 0.65, 0.50, "YES", "cheap", 0.34)]
        assert engine._strategy_type("YES", signals, ai_used=False) == "cross_market_gap"

    def test_ai_assisted_when_ai_used_no_external(self):
        engine = _make_engine()
        signals = [Signal("momentum", 0.65, 0.80, "YES", "up", 0.20)]
        assert engine._strategy_type("YES", signals, ai_used=True) == "ai_assisted"

    def test_market_data_fallback(self):
        engine = _make_engine()
        signals = [Signal("momentum", 0.65, 0.80, "YES", "up", 0.20)]
        assert engine._strategy_type("YES", signals, ai_used=False) == "market_data"

    def test_neutral_direction_cannot_be_cross_market(self):
        engine = _make_engine()
        signals = [Signal("consensus", 0.65, 0.70, "YES", "cheap", 0.34)]
        result = engine._strategy_type("NEUTRAL", signals, ai_used=False)
        assert result != "cross_market_gap"


# ── SignalEngine._trade_quality ────────────────────────────────────────────────

class TestTradeQuality:
    def test_neutral_direction_reduces_quality(self):
        engine  = _make_engine()
        market  = FakeMarket(volume_24h=50_000)
        yes_r   = _make_result(direction="YES")
        neut_r  = _make_result(direction="NEUTRAL")
        q_yes   = engine._trade_quality(market, yes_r,  [], 0.5)
        q_neut  = engine._trade_quality(market, neut_r, [], 0.5)
        assert q_yes > q_neut

    def test_low_volume_reduces_quality(self):
        engine   = _make_engine()
        result   = _make_result()
        q_high   = engine._trade_quality(FakeMarket(volume_24h=200_000), result, [], 0.5)
        q_low    = engine._trade_quality(FakeMarket(volume_24h=200),     result, [], 0.5)
        assert q_high > q_low

    def test_external_support_boosts_quality(self):
        engine   = _make_engine()
        market   = FakeMarket(volume_24h=50_000)
        result   = _make_result()
        no_sup   = []
        with_sup = [Signal("consensus", 0.65, 0.60, "YES", "cheap", 0.34)]
        q_no     = engine._trade_quality(market, result, no_sup,   0.5)
        q_yes    = engine._trade_quality(market, result, with_sup, 0.5)
        assert q_yes > q_no

    def test_higher_edge_boosts_quality(self):
        engine  = _make_engine()
        market  = FakeMarket(volume_24h=50_000)
        low_e   = _make_result(edge=0.01)
        high_e  = _make_result(edge=0.20)
        q_low   = engine._trade_quality(market, low_e,  [], 0.5)
        q_high  = engine._trade_quality(market, high_e, [], 0.5)
        assert q_high > q_low

    def test_quality_bounded_0_to_100(self):
        engine  = _make_engine()
        market  = FakeMarket(volume_24h=2_000_000)
        result  = _make_result(edge=0.50, direction="YES")
        signals = [Signal("consensus", 0.9, 0.90, "YES", "huge gap", 0.34)]
        q       = engine._trade_quality(market, result, signals, 1.0)
        assert 0 <= q <= 100

    def test_pre_score_contributes(self):
        engine   = _make_engine()
        market   = FakeMarket(volume_24h=50_000)
        result   = _make_result()
        q_low_p  = engine._trade_quality(market, result, [], pre_score=0.0)
        q_high_p = engine._trade_quality(market, result, [], pre_score=1.0)
        assert q_high_p > q_low_p


# ── SignalEngine.update_weights ────────────────────────────────────────────────

class TestUpdateWeights:
    def test_weights_sum_to_roughly_one(self):
        engine = _make_engine()
        performance = {
            "momentum":  0.65,
            "consensus": 0.72,
            "predictit": 0.58,
        }
        engine.update_weights(performance)
        total = sum(engine.weights[k] for k in performance)
        assert abs(total - 1.0) < 0.01

    def test_better_signal_gets_more_weight(self):
        engine = _make_engine()
        engine.update_weights({"momentum": 0.80, "consensus": 0.40})
        assert engine.weights["momentum"] > engine.weights["consensus"]

    def test_minimum_weight_floor(self):
        engine = _make_engine()
        # Очень низкая точность не должна давать вес ниже 0.03
        engine.update_weights({"momentum": 0.01, "consensus": 0.99})
        assert engine.weights["momentum"] >= 0.03

    def test_unknown_signal_ignored(self):
        engine = _make_engine()
        before = dict(engine.weights)
        engine.update_weights({"nonexistent_signal": 0.99})
        assert engine.weights == before
