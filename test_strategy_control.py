
from strategy_control import StrategyControl

def test_strategy_weaken_then_disable(tmp_path):
    ctl = StrategyControl(str(tmp_path), min_trades=3, weak_win_rate=0.5, reenable_days=7, weakened_mult=0.5)
    change = None
    for i in range(3):
        change = ctl.record_close(market_id=f"m{i}", strategy_type="market_data", pnl=-1.0, won=False, size_usd=5.0)
    assert change["to"] == "weakened"
    assert ctl.summary()["market_data"]["state"] == "weakened"
    change = ctl.record_close(market_id="m3", strategy_type="market_data", pnl=-1.0, won=False, size_usd=5.0)
    assert change["to"] == "disabled"
    assert ctl.summary()["market_data"]["state"] == "disabled"

def test_all_primary_strategies_weak(tmp_path):
    ctl = StrategyControl(str(tmp_path), min_trades=1)
    ctl.record_close(market_id="a1", strategy_type="market_data", pnl=-1.0, won=False, size_usd=5.0)
    ctl.record_close(market_id="a2", strategy_type="cross_market_gap", pnl=-1.0, won=False, size_usd=5.0)
    assert ctl.all_primary_strategies_weak() is True
