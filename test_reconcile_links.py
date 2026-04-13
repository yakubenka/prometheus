
from data import best_polymarket_url
from risk import RiskManager


def test_best_polymarket_url_prefers_saved_market_url():
    url = best_polymarket_url(
        question="Will X happen?",
        market_url="/event/foo/bar",
        event_url="/event/foo",
        market_slug="foo-bar",
        event_slug="foo",
        slug="foo",
    )
    assert url == "https://polymarket.com/event/foo/bar"


def test_best_polymarket_url_falls_back_to_search_not_broken_slug():
    url = best_polymarket_url(
        question="LoL: HANJIN BRION vs Kiwoom DRX - Game 2 Winner",
        market_slug="lol-bro2-drx-2026-04-09-game-2",
        event_slug="lol-bro2-drx-2026-04-09",
    )
    assert url.startswith("https://polymarket.com/?s=")


def test_reconcile_marks_missing_live_position_as_externally_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("DRY_RUN", "false")
    rm = RiskManager(data_dir=str(tmp_path))
    rm.open(
        market_id="m1",
        question="Test market",
        direction="YES",
        entry_price=0.2,
        size_usd=5.0,
        tags=["sports"],
        signal_type="market_data",
        token_id="tok1",
        market_url="https://polymarket.com/event/test",
        condition_id="cond1",
    )
    result = rm.reconcile_open_positions(real_positions={})
    assert len(result["externally_closed"]) == 1
    assert rm.open_positions == []
    assert rm.closed_positions[0].reconcile_state == "externally_closed"
    assert rm.closed_positions[0].closed_reason == "external_manual_close"
