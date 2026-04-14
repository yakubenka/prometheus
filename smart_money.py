"""
Prometheus — Smart Money Tracker
Находит инсайдеров и sharp-трейдеров по on-chain истории Polymarket.
"""
from __future__ import annotations
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Optional
from collections import defaultdict

from data import Trade, fetch_wallet_trades, fetch_large_trades

log = logging.getLogger("prometheus.smart_money")


class TraderClass(Enum):
    INSIDER     = "insider"
    SHARP       = "sharp"
    CONTRARIAN  = "contrarian"
    NOISE       = "noise"

    @property
    def emoji(self) -> str:
        return {"insider":"🔴","sharp":"🟡","contrarian":"🔵","noise":"⚪"}[self.value]


@dataclass
class WalletProfile:
    address:          str
    trader_class:     TraderClass = TraderClass.NOISE
    win_rate:         float = 0.0
    roi:              float = 0.0
    total_trades:     int   = 0
    resolved_trades:  int   = 0
    total_volume:     float = 0.0
    avg_edge:         float = 0.0
    calibration:      float = 0.0
    insider_score:    float = 0.0
    sharp_score:      float = 0.0
    specializations:  list[str] = field(default_factory=list)
    big_upsets:       int   = 0
    sample_size_ok:   bool  = False
    last_updated:     datetime = field(default_factory=datetime.utcnow)


@dataclass
class SmartSignal:
    wallet:      WalletProfile
    market_id:   str
    question:    str
    direction:   str
    entry_price: float
    their_size:  float
    our_size:    float
    strength:    float
    signal_type: str
    reasoning:   str
    timestamp:   datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class WalletAnalyzer:
    MIN_RESOLVED = 10
    CACHE_TTL    = 3600

    def __init__(self) -> None:
        self._cache: dict[str, WalletProfile] = {}

    def analyze(self, address: str, force: bool = False) -> WalletProfile:
        addr = address.lower()
        if not force and addr in self._cache:
            cached = self._cache[addr]
            if (datetime.utcnow() - cached.last_updated).total_seconds() < self.CACHE_TTL:
                return cached
        trades  = fetch_wallet_trades(addr)
        profile = self._build(addr, trades)
        self._cache[addr] = profile
        return profile

    def _build(self, address: str, trades: list[Trade]) -> WalletProfile:
        p = WalletProfile(address=address)
        if not trades:
            return p

        resolved = [t for t in trades if t.outcome is not None]
        p.total_trades    = len(trades)
        p.resolved_trades = len(resolved)
        p.total_volume    = sum(t.size for t in trades)
        p.sample_size_ok  = len(resolved) >= self.MIN_RESOLVED

        if not resolved:
            return p

        wins   = [t for t in resolved if t.outcome == "YES"]
        p.win_rate = len(wins) / len(resolved)

        total_pnl = sum(self._pnl(t) for t in resolved)
        vol_res   = sum(t.size for t in resolved)
        p.roi     = total_pnl / vol_res if vol_res > 0 else 0.0

        edges     = [abs((1.0 if t.outcome == "YES" else 0.0) - t.price) for t in resolved]
        p.avg_edge = sum(edges) / len(edges) if edges else 0

        p.calibration    = self._calibration(resolved)
        p.specializations = self._specializations(resolved)
        p.big_upsets      = sum(1 for t in resolved
                                if t.price < 0.25 and t.size > 2000 and t.outcome == "YES")
        p.insider_score   = self._insider_score(p, resolved)
        p.sharp_score     = self._sharp_score(p)
        p.trader_class    = self._classify(p)

        log.info(f"  Wallet {address[:10]}… | {p.trader_class.value} | "
                 f"WR={p.win_rate:.1%} ROI={p.roi:.1%} n={p.resolved_trades}")
        return p

    def _pnl(self, t: Trade) -> float:
        if t.outcome == "YES":
            return t.size * ((1.0 / max(t.price, 0.01)) - 1)
        return -t.size

    def _calibration(self, trades: list[Trade]) -> float:
        buckets: dict[float, list[bool]] = defaultdict(list)
        for t in trades:
            buckets[round(t.price * 10) / 10].append(t.outcome == "YES")
        errors = [
            (prob - sum(outcomes)/len(outcomes)) ** 2
            for prob, outcomes in buckets.items()
            if len(outcomes) >= 3
        ]
        return max(0.0, 1.0 - (sum(errors)/len(errors))*4) if errors else 0.5

    def _specializations(self, trades: list[Trade]) -> list[str]:
        DOMAINS = {
            "geopolitics": ["iran","israel","russia","ukraine","china","war","attack"],
            "politics":    ["election","president","congress","vote","trump"],
            "macro":       ["fed","rate","inflation","cpi","gdp","fomc","recession"],
            "crypto":      ["bitcoin","btc","eth","crypto","solana"],
        }
        results: dict[str, list[bool]] = defaultdict(list)
        for t in trades:
            q = t.question.lower()
            for domain, kws in DOMAINS.items():
                if any(kw in q for kw in kws):
                    results[domain].append(t.outcome == "YES")
        return [d for d, r in results.items()
                if len(r) >= 3 and sum(r)/len(r) > 0.65]

    def _insider_score(self, p: WalletProfile, trades: list[Trade]) -> float:
        if not p.sample_size_ok:
            return 0.0
        score = 0.0
        if p.win_rate > 0.80:  score += 0.30
        elif p.win_rate > 0.70: score += 0.15
        if {"geopolitics","politics"} & set(p.specializations): score += 0.20
        if p.big_upsets >= 3:  score += 0.30
        elif p.big_upsets >= 1: score += 0.15
        if p.total_volume > 100_000: score += 0.15
        elif p.total_volume > 30_000: score += 0.08
        return min(1.0, score)

    def _sharp_score(self, p: WalletProfile) -> float:
        if not p.sample_size_ok:
            return 0.0
        score = 0.0
        if p.roi > 0.20:    score += 0.30
        elif p.roi > 0.08:  score += 0.15
        if p.calibration > 0.75: score += 0.25
        elif p.calibration > 0.55: score += 0.12
        if p.win_rate > 0.62 and p.resolved_trades >= 20: score += 0.25
        elif p.win_rate > 0.58 and p.resolved_trades >= 10: score += 0.12
        if p.total_volume > 50_000: score += 0.12
        elif p.total_volume > 15_000: score += 0.06
        return min(1.0, score)

    def _classify(self, p: WalletProfile) -> TraderClass:
        if not p.sample_size_ok:             return TraderClass.NOISE
        if p.insider_score >= 0.55:          return TraderClass.INSIDER
        if p.sharp_score >= 0.50 and p.roi > 0.05: return TraderClass.SHARP
        if p.win_rate > 0.60 and p.avg_edge > 0.3: return TraderClass.CONTRARIAN
        return TraderClass.NOISE


class SmartMoneyMonitor:
    MAX_SIGNALS = 5

    def __init__(self, max_position_usd: float = 20.0) -> None:
        self.analyzer         = WalletAnalyzer()
        self.max_position_usd = max_position_usd
        self.known_wallets:   dict[str, WalletProfile] = {}

    def scan(self) -> list[SmartSignal]:
        log.info("🔍 Smart Money скан...")
        signals: list[SmartSignal] = []
        seen:    set[str]          = set()

        large  = fetch_large_trades(min_size=3000, hours_back=6)
        unusual = [t for t in large if t.is_unusual]
        log.info(f"Необычных сделок: {len(unusual)} из {len(large)}")

        for trade in unusual:
            if not trade.maker or trade.maker in seen:
                continue
            seen.add(trade.maker)
            profile = self.analyzer.analyze(trade.maker)
            self.known_wallets[trade.maker] = profile
            if profile.trader_class == TraderClass.NOISE:
                continue
            sig = self._make_signal(trade, profile)
            if sig:
                signals.append(sig)

        # Мониторим известных трейдеров
        cutoff = datetime.now(timezone.utc) - timedelta(hours=6)
        for addr, profile in self.known_wallets.items():
            if profile.trader_class == TraderClass.NOISE or addr in seen:
                continue
            for trade in fetch_wallet_trades(addr, limit=10):
                if trade.timestamp < cutoff or trade.size < 1000:
                    continue
                sig = self._make_signal(trade, profile)
                if sig:
                    signals.append(sig)

        # Дедупликация + сортировка
        seen_mkts: set[str] = set()
        unique:    list[SmartSignal] = []
        for s in sorted(signals, key=lambda x: x.strength, reverse=True):
            key = f"{s.market_id}_{s.direction}"
            if key not in seen_mkts:
                seen_mkts.add(key)
                unique.append(s)
            if len(unique) >= self.MAX_SIGNALS:
                break

        log.info(f"Smart Money сигналов: {len(unique)}")
        return unique

    def _make_signal(self, trade: Trade,
                     profile: WalletProfile) -> Optional[SmartSignal]:
        strength = self._strength(trade, profile)
        min_str  = 0.55 if profile.trader_class == TraderClass.INSIDER else 0.60
        if strength < min_str:
            return None

        direction = "YES" if trade.side == "BUY" else "NO"
        our_size  = round(min(self.max_position_usd * strength, self.max_position_usd), 2)
        stype     = (
            "insider_bet"  if profile.trader_class == TraderClass.INSIDER else
            "sharp_follow" if profile.trader_class == TraderClass.SHARP   else
            "contrarian"
        )
        reasoning = (
            f"{profile.trader_class.emoji} {profile.trader_class.value} | "
            f"WR {profile.win_rate:.0%} ROI {profile.roi:+.0%} "
            f"({profile.resolved_trades} trades) | "
            f"${trade.size:,.0f} @ {trade.price:.2%}"
        )
        if profile.specializations:
            reasoning += f" | {', '.join(profile.specializations)}"

        return SmartSignal(
            wallet=profile, market_id=trade.market_id, question=trade.question,
            direction=direction, entry_price=trade.price,
            their_size=trade.size, our_size=our_size,
            strength=strength, signal_type=stype, reasoning=reasoning,
        )

    def _strength(self, trade: Trade, profile: WalletProfile) -> float:
        s = 0.0
        if profile.trader_class == TraderClass.INSIDER:    s += 0.40
        elif profile.trader_class == TraderClass.SHARP:    s += 0.28
        elif profile.trader_class == TraderClass.CONTRARIAN: s += 0.18
        if profile.win_rate > 0.75:  s += 0.20
        elif profile.win_rate > 0.65: s += 0.10
        if trade.size > 50_000:  s += 0.20
        elif trade.size > 20_000: s += 0.15
        elif trade.size > 5_000:  s += 0.08
        if trade.is_unusual:     s += 0.12
        s += profile.insider_score * 0.15
        return min(1.0, s)
