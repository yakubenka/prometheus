"""
Тесты для data.py
Покрывает: парсинг рынков, парсинг сделок, тег-экстракцию,
           свойства Market/Trade, fetch_current_price.
Без реальных HTTP запросов.
"""
import sys
import types
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

# Мокаем requests до импорта data.py
if "requests" not in sys.modules:
    _mock_requests = MagicMock()
    _mock_requests.Session.return_value = MagicMock()
    sys.modules["requests"] = _mock_requests
    sys.modules["requests.adapters"] = MagicMock()
    sys.modules["requests.packages"] = MagicMock()
if "urllib3" not in sys.modules:
    sys.modules["urllib3"] = MagicMock()
    sys.modules["urllib3.util"] = MagicMock()
    sys.modules["urllib3.util.retry"] = MagicMock()

import pytest
from data import (
    Market,
    Trade,
    _parse_market,
    _parse_trade,
    _extract_tags,
    fetch_current_price,
)


# ── Вспомогательные фабрики ────────────────────────────────────────────────────

def _raw_market(
    id="mkt-1",
    question="Will X happen?",
    yes_price=0.60,
    no_price=0.40,
    volume=50_000,
    token_yes="tok-yes",
    token_no="tok-no",
    slug=None,
) -> dict:
    return {
        "id":            id,
        "question":      question,
        "outcomePrices": f'["{yes_price}", "{no_price}"]',
        "clobTokenIds":  f'["{token_yes}", "{token_no}"]',
        "volume24hr":    volume,
        "endDate":       "2026-12-31T00:00:00Z",
        "description":   "test market",
        "groupSlug":     slug,
    }


def _raw_trade(
    id="tr-1",
    market="mkt-1",
    maker="0xabc",
    price=0.55,
    size=5000,
    side="BUY",
    outcome=None,
) -> dict:
    return {
        "id":        id,
        "market":    market,
        "question":  "Will X happen?",
        "maker":     maker,
        "price":     price,
        "size":      size,
        "side":      side,
        "timestamp": 1700000000,
        "outcome":   outcome,
    }


# ── Market properties ─────────────────────────────────────────────────────────

class TestMarketProperties:
    def _market(self, yes=0.60, no=0.40, vol=50_000, tid="tok", slug=None) -> Market:
        return Market(
            id="m1", question="Will X?", yes_price=yes, no_price=no,
            volume_24h=vol, end_date=None, token_id_yes=tid, token_id_no="tok-no",
            description="", tags=(), slug=slug,
        )

    def test_spread_near_zero_for_fair_market(self):
        m = self._market(yes=0.60, no=0.40)
        assert m.spread == pytest.approx(0.0, abs=1e-9)

    def test_spread_nonzero_for_wide_market(self):
        m = self._market(yes=0.60, no=0.35)
        assert m.spread == pytest.approx(0.05, abs=1e-9)

    def test_is_tradeable_true_for_valid_market(self):
        m = self._market(yes=0.60, no=0.40, vol=50_000, tid="tok")
        assert m.is_tradeable is True

    def test_is_tradeable_false_near_extremes(self):
        assert self._market(yes=0.99, no=0.01).is_tradeable is False
        assert self._market(yes=0.01, no=0.99).is_tradeable is False

    def test_is_tradeable_false_zero_volume(self):
        assert self._market(yes=0.50, no=0.50, vol=0).is_tradeable is False

    def test_is_tradeable_false_no_token_id(self):
        assert self._market(yes=0.50, no=0.50, tid=None).is_tradeable is False

    def test_is_tradeable_false_wide_spread(self):
        # spread = 0.15 → not tradeable
        assert self._market(yes=0.60, no=0.25).is_tradeable is False

    def test_polymarket_url_uses_slug_when_present(self):
        m = self._market(slug="my-slug")
        assert m.polymarket_url == "https://polymarket.com/event/my-slug"

    def test_polymarket_url_uses_search_without_slug(self):
        m = self._market(slug=None)
        assert "polymarket.com/?s=" in m.polymarket_url

    def test_make_slug_lowercases_and_dashes(self):
        assert Market._make_slug("Will Trump Win?") == "will-trump-win"

    def test_make_slug_removes_special_chars(self):
        assert Market._make_slug("BTC $100k by 2026!") == "btc-100k-by-2026"

    def test_make_slug_max_80_chars(self):
        long_q = "Will " + "x" * 200 + "?"
        assert len(Market._make_slug(long_q)) <= 80


# ── Trade properties ──────────────────────────────────────────────────────────

class TestTradeProperties:
    def _trade(self, size=5000.0) -> Trade:
        return Trade(
            id="t1", market_id="m1", question="Q?",
            maker="0xabc", price=0.50, size=size,
            side="BUY", timestamp=datetime.now(timezone.utc),
        )

    def test_is_large_true_when_size_above_3000(self):
        assert self._trade(size=3000.0).is_large is True
        assert self._trade(size=5000.0).is_large is True

    def test_is_large_false_when_size_below_3000(self):
        assert self._trade(size=2999.0).is_large is False

    def test_is_unusual_same_as_is_large(self):
        t = self._trade(size=4000.0)
        assert t.is_unusual == t.is_large


# ── _parse_market ─────────────────────────────────────────────────────────────

class TestParseMarket:
    def test_valid_raw_returns_market(self):
        raw = _raw_market()
        m = _parse_market(raw)
        assert m is not None
        assert m.id == "mkt-1"
        assert m.yes_price == pytest.approx(0.60)
        assert m.no_price  == pytest.approx(0.40)

    def test_volume_from_volume24hr(self):
        raw = _raw_market(volume=12_345)
        m = _parse_market(raw)
        assert m.volume_24h == pytest.approx(12_345)

    def test_token_ids_parsed(self):
        raw = _raw_market(token_yes="yes-tok", token_no="no-tok")
        m = _parse_market(raw)
        assert m.token_id_yes == "yes-tok"
        assert m.token_id_no  == "no-tok"

    def test_slug_from_group_slug(self):
        raw = _raw_market(slug="my-event")
        m = _parse_market(raw)
        assert m.slug == "my-event"

    def test_missing_outcome_prices_returns_none(self):
        raw = _raw_market()
        raw.pop("outcomePrices")
        assert _parse_market(raw) is None

    def test_invalid_json_prices_returns_none(self):
        raw = _raw_market()
        raw["outcomePrices"] = "not-json"
        assert _parse_market(raw) is None

    def test_prices_as_list_not_string(self):
        raw = _raw_market()
        raw["outcomePrices"] = [0.70, 0.30]
        m = _parse_market(raw)
        assert m is not None
        assert m.yes_price == pytest.approx(0.70)

    def test_description_truncated_at_500(self):
        raw = _raw_market()
        raw["description"] = "x" * 1000
        m = _parse_market(raw)
        assert len(m.description) <= 500

    def test_question_falls_back_to_title(self):
        raw = _raw_market()
        raw.pop("question")
        raw["title"] = "Fallback title"
        m = _parse_market(raw)
        assert m is not None
        assert m.question == "Fallback title"


# ── _parse_trade ──────────────────────────────────────────────────────────────

class TestParseTrade:
    def test_valid_raw_returns_trade(self):
        raw = _raw_trade()
        t = _parse_trade(raw)
        assert t is not None
        assert t.price == pytest.approx(0.55)
        assert t.size  == pytest.approx(5000)

    def test_maker_lowercased(self):
        raw = _raw_trade(maker="0xABCDEF")
        t = _parse_trade(raw)
        assert t.maker == "0xabcdef"

    def test_side_uppercased(self):
        raw = _raw_trade(side="buy")
        t = _parse_trade(raw)
        assert t.side == "BUY"

    def test_outcome_yes_parsed(self):
        raw = _raw_trade(outcome="yes")
        t = _parse_trade(raw)
        assert t.outcome == "YES"

    def test_outcome_no_parsed(self):
        raw = _raw_trade(outcome="NO")
        t = _parse_trade(raw)
        assert t.outcome == "NO"

    def test_outcome_numeric_yes(self):
        raw = _raw_trade(outcome="1")
        t = _parse_trade(raw)
        assert t.outcome == "YES"

    def test_outcome_none_when_missing(self):
        raw = _raw_trade(outcome=None)
        t = _parse_trade(raw)
        assert t.outcome is None

    def test_timestamp_from_unix_int(self):
        raw = _raw_trade()
        raw["timestamp"] = 1700000000
        t = _parse_trade(raw)
        assert t.timestamp.tzinfo is not None

    def test_timestamp_from_iso_string(self):
        raw = _raw_trade()
        raw["timestamp"] = "2026-01-01T12:00:00Z"
        t = _parse_trade(raw)
        assert t.timestamp.year == 2026

    def test_missing_id_returns_trade_with_empty_id(self):
        raw = _raw_trade()
        raw.pop("id")
        t = _parse_trade(raw)
        assert t is not None

    def test_fallback_market_id_from_conditionId(self):
        raw = _raw_trade()
        raw.pop("market")
        raw["conditionId"] = "cond-999"
        t = _parse_trade(raw)
        assert t.market_id == "cond-999"


# ── _extract_tags ─────────────────────────────────────────────────────────────

class TestExtractTags:
    def test_crypto_from_bitcoin(self):
        assert "crypto" in _extract_tags("bitcoin price reaches 100k")

    def test_politics_from_election(self):
        assert "politics" in _extract_tags("trump wins election 2026")

    def test_macro_from_fed(self):
        assert "macro" in _extract_tags("fed raises interest rate by 0.25")

    def test_geopolitics_from_russia(self):
        assert "geopolitics" in _extract_tags("russia attacks nato ally")

    def test_sports_from_nba(self):
        assert "sports" in _extract_tags("nba finals game 7 tonight")

    def test_tech_from_openai(self):
        assert "tech" in _extract_tags("openai releases new model")

    def test_multiple_tags(self):
        tags = _extract_tags("trump bitcoin crypto election")
        assert "crypto" in tags
        assert "politics" in tags

    def test_empty_text_returns_empty_tuple(self):
        assert _extract_tags("") == ()

    def test_unrelated_text_returns_empty_tuple(self):
        assert _extract_tags("sunny weather in paris") == ()

    def test_returns_tuple(self):
        assert isinstance(_extract_tags("bitcoin"), tuple)


# ── fetch_current_price ───────────────────────────────────────────────────────

class TestFetchCurrentPrice:
    @patch("data.SESSION")
    def test_returns_price_on_success(self, mock_session):
        mock_session.get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"price": "0.65"},
        )
        mock_session.get.return_value.raise_for_status = lambda: None
        result = fetch_current_price("tok-001")
        assert result == pytest.approx(0.65)

    @patch("data._safe_get", return_value={"price": "0"})
    def test_returns_zero_on_zero_price(self, _):
        # API вернул {"price": "0"} → fetch вернёт 0.0
        assert fetch_current_price("tok-001") == pytest.approx(0.0)

    @patch("data._safe_get", return_value=None)
    def test_returns_none_on_network_error(self, _):
        assert fetch_current_price("tok-001") is None
