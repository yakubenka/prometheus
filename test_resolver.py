"""
Тесты для resolver.py
Покрывает: вычисление шаров, определение исхода рынка, верификацию закрытия,
           агрегацию performance, P&L логику.
Все HTTP вызовы замоканы — никаких реальных запросов к Polymarket/Gamma.
"""
import sys
import types
import json
from dataclasses import dataclass
from unittest.mock import patch, MagicMock

# Мокаем requests до импортов из проекта
if "requests" not in sys.modules:
    _mock_requests = MagicMock()
    _mock_requests.Session.return_value = MagicMock()
    sys.modules["requests"] = _mock_requests
    sys.modules["requests.adapters"] = MagicMock()
    sys.modules["requests.packages"] = MagicMock()

# Мокаем py_clob_client и psycopg2 до импортов из проекта
sys.modules.setdefault("psycopg2", types.SimpleNamespace(connect=lambda *a, **k: None))
sys.modules.setdefault("py_clob_client", types.SimpleNamespace())
sys.modules.setdefault("py_clob_client.client", types.SimpleNamespace(ClobClient=object))
sys.modules.setdefault("py_clob_client.clob_types",
    types.SimpleNamespace(MarketOrderArgs=object, OrderType=object, ApiCreds=object))
sys.modules.setdefault("py_clob_client.order_builder",
    types.SimpleNamespace())
sys.modules.setdefault("py_clob_client.order_builder.constants",
    types.SimpleNamespace(BUY="BUY", SELL="SELL"))

import pytest

from resolver import (
    calc_position_token_entry_price,
    calc_position_shares,
    check_market_outcome,
    verify_position_closed,
    fetch_polymarket_positions,
    PositionResolver,
)
from risk import Position, RiskManager


# ── Вспомогательные фабрики ────────────────────────────────────────────────────

def _pos(
    market_id:   str   = "mkt-001",
    direction:   str   = "YES",
    entry_price: float = 0.40,
    size_usd:    float = 10.0,
    token_id:    str   = "tok-001",
    signal_type: str   = "market_data",
    tags:        list  = None,
) -> Position:
    return Position(
        market_id   = market_id,
        question    = "Will this resolve YES?",
        direction   = direction,
        entry_price = entry_price,
        size_usd    = size_usd,
        opened_at   = "2026-01-01T00:00:00+00:00",
        tags        = tags or [],
        token_id    = token_id,
        signal_type = signal_type,
    )


def _gamma_response(
    winner:        str  = None,
    closed:        bool = False,
    outcome_prices: list = None,
) -> dict:
    """Строит словарь похожий на ответ Gamma API."""
    d = {"active": not closed, "closed": closed}
    if winner:
        d["winner"] = winner
    if outcome_prices is not None:
        d["outcomePrices"] = json.dumps(outcome_prices)
    return d


# ── calc_position_token_entry_price ───────────────────────────────────────────

class TestCalcPositionTokenEntryPrice:
    def test_yes_direction_returns_entry_price(self):
        assert calc_position_token_entry_price("YES", 0.40) == 0.40

    def test_no_direction_returns_complement(self):
        assert calc_position_token_entry_price("NO", 0.80) == pytest.approx(0.20)

    def test_no_at_zero_point_five(self):
        assert calc_position_token_entry_price("NO", 0.50) == pytest.approx(0.50)


# ── calc_position_shares ──────────────────────────────────────────────────────

class TestCalcPositionShares:
    def test_yes_direction(self):
        pos = _pos(direction="YES", entry_price=0.50, size_usd=10.0)
        # 10.0 / 0.50 = 20 shares
        assert calc_position_shares(pos) == pytest.approx(20.0, rel=1e-3)

    def test_no_direction_uses_no_price(self):
        pos = _pos(direction="NO", entry_price=0.80, size_usd=10.0)
        # NO token price = 1 - 0.80 = 0.20 → 10.0 / 0.20 = 50 shares
        assert calc_position_shares(pos) == pytest.approx(50.0, rel=1e-3)

    def test_with_explicit_params(self):
        # Передаём параметры напрямую (без Position объекта)
        shares = calc_position_shares("YES", entry_price=0.25, size_usd=5.0)
        assert shares == pytest.approx(20.0, rel=1e-3)

    def test_zero_price_guard(self):
        # entry_price=0 не должен вызывать ZeroDivisionError
        pos = _pos(direction="YES", entry_price=0.0, size_usd=10.0)
        result = calc_position_shares(pos)
        assert result > 0


# ── check_market_outcome ──────────────────────────────────────────────────────

class TestCheckMarketOutcome:
    @patch("resolver.requests.get")
    def test_returns_yes_when_winner_yes(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: _gamma_response(winner="YES"),
        )
        assert check_market_outcome("mkt-001") == "YES"

    @patch("resolver.requests.get")
    def test_returns_no_when_winner_no(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: _gamma_response(winner="NO"),
        )
        assert check_market_outcome("mkt-001") == "NO"

    @patch("resolver.requests.get")
    def test_returns_yes_from_outcome_prices(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: _gamma_response(closed=True, outcome_prices=[0.99, 0.01]),
        )
        assert check_market_outcome("mkt-001") == "YES"

    @patch("resolver.requests.get")
    def test_returns_no_from_outcome_prices(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: _gamma_response(closed=True, outcome_prices=[0.01, 0.99]),
        )
        assert check_market_outcome("mkt-001") == "NO"

    @patch("resolver.requests.get")
    def test_returns_none_when_market_open(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"active": True, "closed": False},
        )
        assert check_market_outcome("mkt-001") is None

    @patch("resolver.requests.get")
    def test_fallback_to_price_on_api_error(self, mock_get):
        mock_get.return_value = MagicMock(status_code=500)
        # Цена 0.99 → YES
        assert check_market_outcome("mkt-001", current_price=0.99) == "YES"
        # Цена 0.01 → NO
        assert check_market_outcome("mkt-002", current_price=0.01) == "NO"

    @patch("resolver.requests.get")
    def test_fallback_to_none_on_ambiguous_price(self, mock_get):
        mock_get.return_value = MagicMock(status_code=500)
        assert check_market_outcome("mkt-001", current_price=0.50) is None

    @patch("resolver.requests.get")
    def test_returns_none_on_exception(self, mock_get):
        mock_get.side_effect = Exception("network error")
        assert check_market_outcome("mkt-001") is None

    @patch("resolver.requests.get")
    def test_winner_numeric_strings(self, mock_get):
        """Gamma иногда возвращает "1" вместо "YES"."""
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: _gamma_response(winner="1"),
        )
        assert check_market_outcome("mkt-001") == "YES"

        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: _gamma_response(winner="0"),
        )
        assert check_market_outcome("mkt-002") == "NO"


# ── verify_position_closed ────────────────────────────────────────────────────

class TestVerifyPositionClosed:
    @patch("resolver.fetch_polymarket_positions")
    def test_true_when_token_absent(self, mock_fetch):
        mock_fetch.return_value = {}
        assert verify_position_closed("tok", "0xabc", retries=1, wait_sec=0) is True

    @patch("resolver.fetch_polymarket_positions")
    def test_false_when_token_present_with_size(self, mock_fetch):
        mock_fetch.return_value = {"tok": {"size": 2.5}}
        assert verify_position_closed("tok", "0xabc", retries=1, wait_sec=0) is False

    @patch("resolver.fetch_polymarket_positions")
    def test_true_when_size_below_threshold(self, mock_fetch):
        # size <= 0.01 считается закрытым
        mock_fetch.return_value = {"tok": {"size": 0.005}}
        assert verify_position_closed("tok", "0xabc", retries=1, wait_sec=0) is True

    def test_false_when_no_token_id(self):
        assert verify_position_closed("", "0xabc", retries=1, wait_sec=0) is False

    def test_false_when_no_funder(self):
        assert verify_position_closed("tok", "", retries=1, wait_sec=0) is False

    @patch("resolver.fetch_polymarket_positions")
    def test_retries_correct_number_of_times(self, mock_fetch):
        mock_fetch.return_value = {"tok": {"size": 5.0}}
        verify_position_closed("tok", "0xfunder", retries=3, wait_sec=0)
        assert mock_fetch.call_count == 3


# ── PositionResolver._make_closed_dict ────────────────────────────────────────

class TestMakeClosedDict:
    def _make_resolver(self) -> PositionResolver:
        resolver       = PositionResolver.__new__(PositionResolver)
        resolver.risk  = None
        resolver.tg    = None
        resolver._signal_outcomes = []
        return resolver

    def test_structure(self):
        resolver = self._make_resolver()
        pos      = _pos(direction="YES", entry_price=0.40, size_usd=10.0)
        result   = resolver._make_closed_dict(pos, "YES", won=True, pnl=5.0)

        assert result["market_id"]   == pos.market_id
        assert result["direction"]   == "YES"
        assert result["entry_price"] == 0.40
        assert result["outcome"]     == "YES"
        assert result["won"]         is True
        assert result["pnl"]         == 5.0
        assert result["size_usd"]    == 10.0
        assert result["signal_type"] == pos.signal_type

    def test_stop_loss_outcome(self):
        resolver = self._make_resolver()
        pos      = _pos()
        result   = resolver._make_closed_dict(pos, "STOP_LOSS", won=False, pnl=-4.0)
        assert result["outcome"] == "STOP_LOSS"
        assert result["won"]     is False
        assert result["pnl"]     < 0


# ── PositionResolver.signal_performance ───────────────────────────────────────

class TestSignalPerformance:
    def _resolver_with_outcomes(self, outcomes: list) -> PositionResolver:
        resolver                 = PositionResolver.__new__(PositionResolver)
        resolver.risk            = None
        resolver.tg              = None
        resolver._signal_outcomes = outcomes
        return resolver

    def test_win_rate_calculation(self):
        outcomes = [
            {"signal_type": "market_data", "won": True,  "pnl": 5.0},
            {"signal_type": "market_data", "won": True,  "pnl": 3.0},
            {"signal_type": "market_data", "won": False, "pnl": -2.0},
        ]
        resolver = self._resolver_with_outcomes(outcomes)
        perf     = resolver.signal_performance()
        assert perf["market_data"]["win_rate"] == pytest.approx(2 / 3, rel=1e-3)
        assert perf["market_data"]["total"]    == 3

    def test_multiple_strategy_types(self):
        outcomes = [
            {"signal_type": "market_data",    "won": True,  "pnl": 5.0},
            {"signal_type": "smart_money",    "won": False, "pnl": -2.0},
            {"signal_type": "cross_market_gap", "won": True, "pnl": 4.0},
        ]
        resolver = self._resolver_with_outcomes(outcomes)
        perf     = resolver.signal_performance()
        assert "market_data"      in perf
        assert "smart_money"      in perf
        assert "cross_market_gap" in perf

    def test_empty_outcomes(self):
        resolver = self._resolver_with_outcomes([])
        assert resolver.signal_performance() == {}

    def test_perfect_win_rate(self):
        outcomes = [
            {"signal_type": "smart_money", "won": True, "pnl": 10.0},
            {"signal_type": "smart_money", "won": True, "pnl": 8.0},
        ]
        resolver = self._resolver_with_outcomes(outcomes)
        perf     = resolver.signal_performance()
        assert perf["smart_money"]["win_rate"] == 1.0

    def test_zero_win_rate(self):
        outcomes = [
            {"signal_type": "ai_assisted", "won": False, "pnl": -3.0},
            {"signal_type": "ai_assisted", "won": False, "pnl": -5.0},
        ]
        resolver = self._resolver_with_outcomes(outcomes)
        perf     = resolver.signal_performance()
        assert perf["ai_assisted"]["win_rate"] == 0.0
