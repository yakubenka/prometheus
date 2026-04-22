
"""
Prometheus — Strategy control
Отдельная статистика по типам стратегий, ослабление и временное отключение.
"""
from __future__ import annotations
import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger("prometheus.strategy_control")

TRACKED_STRATEGIES = ("cross_market_gap", "market_data", "ai_assisted", "smart_money", "near_resolution")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class StrategyStats:
    name: str
    trades: int = 0
    wins: int = 0
    pnl: float = 0.0
    volume: float = 0.0
    state: str = "active"  # active|weakened|disabled
    weaken_count: int = 0
    disabled_until: str = ""
    last_change_at: str = ""
    last_reason: str = ""

    @property
    def win_rate(self) -> float:
        return round(self.wins / self.trades, 3) if self.trades else 0.0

    @property
    def roi(self) -> float:
        return round(self.pnl / self.volume, 3) if self.volume else 0.0


class StrategyControl:
    def __init__(
        self,
        data_dir: str,
        *,
        min_trades: int = 10,
        weak_win_rate: float = 0.45,
        reenable_days: int = 7,
        weakened_mult: float = 0.5,
        all_weak_min_usd: float = 1.0,
    ) -> None:
        self._dir = Path(data_dir)
        self.min_trades = min_trades
        self.weak_win_rate = weak_win_rate
        self.reenable_days = reenable_days
        self.weakened_mult = weakened_mult
        self.all_weak_min_usd = all_weak_min_usd
        self._stats = {name: StrategyStats(name=name) for name in TRACKED_STRATEGIES}
        self._processed_market_ids: set[str] = set()
        self._load()

    def record_close(self, *, market_id: str, strategy_type: str, pnl: float, won: bool, size_usd: float) -> Optional[dict]:
        if strategy_type not in self._stats or market_id in self._processed_market_ids:
            return None
        self.refresh_states()
        stat = self._stats[strategy_type]
        stat.trades += 1
        stat.wins += 1 if won else 0
        stat.pnl = round(stat.pnl + float(pnl or 0.0), 2)
        stat.volume = round(stat.volume + float(size_usd or 0.0), 2)
        self._processed_market_ids.add(market_id)
        change = self._evaluate(strategy_type)
        self._save()
        return change

    def refresh_states(self) -> list[dict]:
        now = _utc_now()
        changes: list[dict] = []
        for stat in self._stats.values():
            if stat.state != "disabled" or not stat.disabled_until:
                continue
            try:
                until = datetime.fromisoformat(stat.disabled_until)
            except Exception:
                until = now
            if now >= until:
                prev = stat.state
                stat.state = "weakened"
                stat.last_change_at = now.isoformat()
                stat.last_reason = "cooldown_finished"
                changes.append({
                    "strategy": stat.name,
                    "from": prev,
                    "to": stat.state,
                    "reason": stat.last_reason,
                    "stats": self.summary_for(stat.name),
                })
        if changes:
            self._save()
        return changes

    def size_multiplier(self, strategy_type: str) -> float:
        self.refresh_states()
        stat = self._stats.get(strategy_type)
        if not stat:
            return 1.0
        if stat.state == "disabled":
            return 0.0
        if stat.state == "weakened":
            return self.weakened_mult
        return 1.0

    def should_trade(self, strategy_type: str) -> bool:
        return self.size_multiplier(strategy_type) > 0

    def all_primary_strategies_weak(self) -> bool:
        relevant = [self._stats[s] for s in ("cross_market_gap", "market_data")]
        return all(s.state in {"weakened", "disabled"} for s in relevant)

    def summary(self) -> dict:
        self.refresh_states()
        return {name: self.summary_for(name) for name in self._stats}

    def summary_for(self, strategy_type: str) -> dict:
        stat = self._stats[strategy_type]
        return {
            "name": stat.name,
            "trades": stat.trades,
            "wins": stat.wins,
            "win_rate": stat.win_rate,
            "pnl": round(stat.pnl, 2),
            "volume": round(stat.volume, 2),
            "roi": stat.roi,
            "state": stat.state,
            "size_multiplier": self.size_multiplier_inline(stat),
            "disabled_until": stat.disabled_until,
            "weaken_count": stat.weaken_count,
            "last_reason": stat.last_reason,
        }

    def size_multiplier_inline(self, stat: StrategyStats) -> float:
        if stat.state == "disabled":
            return 0.0
        if stat.state == "weakened":
            return self.weakened_mult
        return 1.0

    def _evaluate(self, strategy_type: str) -> Optional[dict]:
        stat = self._stats[strategy_type]
        if stat.trades < self.min_trades:
            return None
        poor = (stat.pnl < 0) and (stat.win_rate < self.weak_win_rate)
        if not poor:
            if stat.state != "active":
                prev = stat.state
                stat.state = "active"
                stat.disabled_until = ""
                stat.last_change_at = _utc_now().isoformat()
                stat.last_reason = "recovered"
                return {
                    "strategy": strategy_type,
                    "from": prev,
                    "to": "active",
                    "reason": stat.last_reason,
                    "stats": self.summary_for(strategy_type),
                }
            return None

        now = _utc_now()
        if stat.state == "active":
            stat.state = "weakened"
            stat.weaken_count += 1
            stat.last_change_at = now.isoformat()
            stat.last_reason = f"poor_after_{stat.trades}_trades"
            return {
                "strategy": strategy_type,
                "from": "active",
                "to": "weakened",
                "reason": stat.last_reason,
                "stats": self.summary_for(strategy_type),
            }

        if stat.state == "weakened":
            stat.state = "disabled"
            stat.disabled_until = (now + timedelta(days=self.reenable_days)).isoformat()
            stat.last_change_at = now.isoformat()
            stat.last_reason = f"still_poor_after_{stat.trades}_trades"
            return {
                "strategy": strategy_type,
                "from": "weakened",
                "to": "disabled",
                "reason": stat.last_reason,
                "stats": self.summary_for(strategy_type),
            }
        return None

    def _load(self) -> None:
        try:
            path = self._dir / "strategy_control.json"
            if not path.exists():
                return
            data = json.loads(path.read_text())
            for name, payload in (data.get("stats") or {}).items():
                if name in self._stats and isinstance(payload, dict):
                    self._stats[name] = StrategyStats(**{**asdict(self._stats[name]), **payload})
            self._processed_market_ids = set(data.get("processed_market_ids") or [])
        except Exception as e:
            log.warning(f"StrategyControl load error: {e}")

    def _save(self) -> None:
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            path = self._dir / "strategy_control.json"
            payload = {
                "stats": {name: asdict(stat) for name, stat in self._stats.items()},
                "processed_market_ids": sorted(self._processed_market_ids),
            }
            path.write_text(json.dumps(payload, indent=2))
        except Exception as e:
            log.warning(f"StrategyControl save error: {e}")
