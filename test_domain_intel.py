"""
Тесты для domain_intel.py
Покрывает: DomainStats properties, DomainPrior (record/get/save/load),
           portfolio_kelly_size, _tags_to_domains.
Без реальных HTTP запросов и файловой системы (tmp dirs через tmp_path).
"""
import sys
import types
import json
from unittest.mock import MagicMock

if "requests" not in sys.modules:
    _mock_requests = MagicMock()
    sys.modules["requests"] = _mock_requests
    sys.modules["requests.adapters"] = MagicMock()

import pytest
from domain_intel import (
    DomainStats,
    DomainPrior,
    portfolio_kelly_size,
    ALL_DOMAINS,
)


# ── DomainStats properties ────────────────────────────────────────────────────

class TestDomainStats:
    def _stats(self, wins=0, losses=0, pnl=0.0, vol=0.0) -> DomainStats:
        s = DomainStats(domain="politics")
        s.wins      = wins
        s.losses    = losses
        s.total_pnl = pnl
        s.total_vol = vol
        return s

    def test_total(self):
        s = self._stats(wins=3, losses=2)
        assert s.total == 5

    def test_win_rate_no_trades_returns_half(self):
        assert self._stats().win_rate == 0.5

    def test_win_rate_calculates_correctly(self):
        s = self._stats(wins=3, losses=1)
        assert s.win_rate == pytest.approx(0.75)

    def test_posterior_win_rate_starts_near_half(self):
        # prior alpha=2, beta=2 → 2/(0+4) = 0.5 with no trades
        s = self._stats()
        assert s.posterior_win_rate == pytest.approx(0.5)

    def test_posterior_win_rate_regresses_to_prior_on_few_trades(self):
        # 1 win, 0 losses → posterior < 1.0 because of prior
        s = self._stats(wins=1, losses=0)
        assert s.posterior_win_rate < 1.0
        assert s.posterior_win_rate > 0.5

    def test_posterior_win_rate_converges_on_many_trades(self):
        # 100 wins, 0 losses → very close to 1.0
        s = self._stats(wins=100, losses=0)
        assert s.posterior_win_rate > 0.95

    def test_roi_zero_when_no_volume(self):
        assert self._stats().roi == 0.0

    def test_roi_calculates_correctly(self):
        s = self._stats(pnl=10.0, vol=100.0)
        assert s.roi == pytest.approx(0.10)

    def test_roi_negative(self):
        s = self._stats(pnl=-20.0, vol=100.0)
        assert s.roi == pytest.approx(-0.20)

    def test_size_multiplier_neutral_when_no_data(self):
        # < 5 trades → return 1.0 regardless
        s = self._stats(wins=2, losses=2)
        assert s.size_multiplier == 1.0

    def test_size_multiplier_boosted_for_great_domain(self):
        # many wins + high ROI
        s = self._stats(wins=20, losses=5, pnl=50.0, vol=100.0)
        assert s.size_multiplier > 1.0

    def test_size_multiplier_reduced_for_poor_domain(self):
        # many losses
        s = self._stats(wins=2, losses=20)
        assert s.size_multiplier < 1.0

    def test_size_multiplier_minimum_0_3(self):
        s = self._stats(wins=0, losses=100)
        assert s.size_multiplier >= 0.3

    def test_size_multiplier_maximum_1_5(self):
        s = self._stats(wins=100, losses=0, pnl=999.0, vol=100.0)
        assert s.size_multiplier <= 1.5

    def test_confidence_adjustment_zero_when_no_data(self):
        assert self._stats(wins=2, losses=1).confidence_adjustment == 0.0

    def test_confidence_adjustment_positive_for_good_domain(self):
        s = self._stats(wins=50, losses=10)
        assert s.confidence_adjustment > 0

    def test_confidence_adjustment_negative_for_bad_domain(self):
        s = self._stats(wins=5, losses=50)
        assert s.confidence_adjustment < 0


# ── DomainPrior ───────────────────────────────────────────────────────────────

class TestDomainPrior:
    def test_initialises_all_domains(self, tmp_path):
        dp = DomainPrior(data_dir=str(tmp_path))
        for domain in ALL_DOMAINS:
            assert domain in dp._stats

    def test_record_win_increments_wins(self, tmp_path):
        dp = DomainPrior(data_dir=str(tmp_path))
        dp.record(["crypto"], won=True, pnl=5.0, size=10.0)
        assert dp._stats["crypto"].wins == 1
        assert dp._stats["crypto"].losses == 0

    def test_record_loss_increments_losses(self, tmp_path):
        dp = DomainPrior(data_dir=str(tmp_path))
        dp.record(["politics"], won=False, pnl=-3.0, size=10.0)
        assert dp._stats["politics"].losses == 1

    def test_record_updates_pnl_and_volume(self, tmp_path):
        dp = DomainPrior(data_dir=str(tmp_path))
        dp.record(["macro"], won=True, pnl=7.5, size=20.0)
        assert dp._stats["macro"].total_pnl == pytest.approx(7.5)
        assert dp._stats["macro"].total_vol == pytest.approx(20.0)

    def test_record_unknown_tag_falls_back_to_other(self, tmp_path):
        dp = DomainPrior(data_dir=str(tmp_path))
        dp.record(["unknown_domain"], won=True, pnl=1.0, size=5.0)
        assert dp._stats["other"].wins == 1

    def test_record_multiple_tags(self, tmp_path):
        dp = DomainPrior(data_dir=str(tmp_path))
        dp.record(["crypto", "macro"], won=True, pnl=5.0, size=10.0)
        assert dp._stats["crypto"].wins == 1
        assert dp._stats["macro"].wins == 1

    def test_get_size_multiplier_returns_one_for_new_domain(self, tmp_path):
        dp = DomainPrior(data_dir=str(tmp_path))
        assert dp.get_size_multiplier(["crypto"]) == pytest.approx(1.0)

    def test_get_size_multiplier_averages_multiple_domains(self, tmp_path):
        dp = DomainPrior(data_dir=str(tmp_path))
        # Добавляем много данных в два домена
        for _ in range(20):
            dp.record(["crypto"], won=True, pnl=1.0, size=1.0)
        for _ in range(20):
            dp.record(["politics"], won=False, pnl=-1.0, size=1.0)
        mult = dp.get_size_multiplier(["crypto", "politics"])
        # Должно быть среднее — больше 0.5 и меньше 1.5
        assert 0.5 < mult < 1.5

    def test_get_confidence_adj_zero_for_new_domain(self, tmp_path):
        dp = DomainPrior(data_dir=str(tmp_path))
        assert dp.get_confidence_adj(["crypto"]) == pytest.approx(0.0)

    def test_save_and_load_roundtrip(self, tmp_path):
        dp = DomainPrior(data_dir=str(tmp_path))
        dp.record(["crypto"], won=True, pnl=5.0, size=10.0)
        dp.record(["politics"], won=False, pnl=-2.0, size=8.0)

        # Загружаем в новый экземпляр из того же файла
        dp2 = DomainPrior(data_dir=str(tmp_path))
        assert dp2._stats["crypto"].wins == 1
        assert dp2._stats["politics"].losses == 1
        assert dp2._stats["crypto"].total_pnl == pytest.approx(5.0)

    def test_save_is_atomic_no_tmp_file_leftover(self, tmp_path):
        dp = DomainPrior(data_dir=str(tmp_path))
        dp.record(["crypto"], won=True, pnl=1.0, size=1.0)
        # После записи .tmp файл не должен оставаться
        tmp_file = (tmp_path / "domain_stats.tmp")
        assert not tmp_file.exists()

    def test_load_from_missing_file_is_safe(self, tmp_path):
        # Не должно падать если файла нет
        dp = DomainPrior(data_dir=str(tmp_path / "nonexistent"))
        assert dp._stats["crypto"].wins == 0

    def test_load_from_corrupt_file_is_safe(self, tmp_path):
        corrupt = tmp_path / "domain_stats.json"
        corrupt.write_text("not valid json {{{{")
        dp = DomainPrior(data_dir=str(tmp_path))
        # Не должно падать, статистика остаётся нулевой
        assert dp._stats["crypto"].wins == 0

    def test_tags_to_domains_empty_falls_back_to_other(self, tmp_path):
        dp = DomainPrior(data_dir=str(tmp_path))
        assert dp._tags_to_domains([]) == ["other"]

    def test_tags_to_domains_filters_unknown(self, tmp_path):
        dp = DomainPrior(data_dir=str(tmp_path))
        result = dp._tags_to_domains(["crypto", "UNKNOWN_TAG"])
        assert result == ["crypto"]

    def test_stats_summary_excludes_empty_domains(self, tmp_path):
        dp = DomainPrior(data_dir=str(tmp_path))
        dp.record(["crypto"], won=True, pnl=5.0, size=10.0)
        summary = dp.stats_summary()
        assert "crypto" in summary
        assert "politics" not in summary  # ни одной записи


# ── portfolio_kelly_size ──────────────────────────────────────────────────────

class TestPortfolioKellySize:

    class _FakePos:
        def __init__(self, size):
            self.size_usd = size

    def test_returns_positive_for_edge_case(self):
        size = portfolio_kelly_size(
            ai_probability=0.70,
            market_price=0.50,
            direction="YES",
            bankroll=100.0,
            kelly_fraction=0.20,
            open_positions=[],
            max_position_usd=20.0,
        )
        assert size > 0

    def test_capped_at_max_position_usd(self):
        size = portfolio_kelly_size(
            ai_probability=0.99,
            market_price=0.01,
            direction="YES",
            bankroll=1000.0,
            kelly_fraction=0.50,
            open_positions=[],
            max_position_usd=15.0,
        )
        assert size <= 15.0

    def test_zero_for_negative_edge(self):
        # AI prob < market price → no edge → kelly=0 → size=0
        size = portfolio_kelly_size(
            ai_probability=0.40,
            market_price=0.60,
            direction="YES",
            bankroll=100.0,
            kelly_fraction=0.20,
            open_positions=[],
            max_position_usd=20.0,
        )
        assert size == pytest.approx(0.0)

    def test_reduced_when_portfolio_exposed(self):
        open_positions = [self._FakePos(50.0)]  # уже 50% капитала занято
        size_exposed = portfolio_kelly_size(
            ai_probability=0.70,
            market_price=0.50,
            direction="YES",
            bankroll=100.0,
            kelly_fraction=0.20,
            open_positions=open_positions,
            max_position_usd=20.0,
        )
        size_empty = portfolio_kelly_size(
            ai_probability=0.70,
            market_price=0.50,
            direction="YES",
            bankroll=100.0,
            kelly_fraction=0.20,
            open_positions=[],
            max_position_usd=20.0,
        )
        assert size_exposed < size_empty

    def test_domain_multiplier_scales_size(self):
        base = portfolio_kelly_size(
            ai_probability=0.70, market_price=0.50, direction="YES",
            bankroll=100.0, kelly_fraction=0.20, open_positions=[],
            max_position_usd=20.0, domain_multiplier=1.0,
        )
        boosted = portfolio_kelly_size(
            ai_probability=0.70, market_price=0.50, direction="YES",
            bankroll=100.0, kelly_fraction=0.20, open_positions=[],
            max_position_usd=20.0, domain_multiplier=1.5,
        )
        assert boosted >= base

    def test_no_direction_uses_complement_price(self):
        # direction=NO → price = 1 - market_price
        size = portfolio_kelly_size(
            ai_probability=0.70,
            market_price=0.20,  # NO price = 0.80
            direction="NO",
            bankroll=100.0,
            kelly_fraction=0.20,
            open_positions=[],
            max_position_usd=20.0,
        )
        assert size >= 0

    def test_probability_clamped_at_extremes(self):
        # Не должно падать на граничных значениях
        for prob in (0.0, 0.001, 0.999, 1.0):
            size = portfolio_kelly_size(
                ai_probability=prob, market_price=0.50, direction="YES",
                bankroll=100.0, kelly_fraction=0.20, open_positions=[],
                max_position_usd=20.0,
            )
            assert size >= 0
