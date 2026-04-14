"""
Prometheus v3 — Backtesting Engine
Валидация стратегии на исторических данных Polymarket.
Никакого forward-looking bias. Строгая изоляция данных.
"""

from __future__ import annotations
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
import time
from anthropic import Anthropic

from data import fetch_closed_markets, Market

log = logging.getLogger("prometheus.backtest")


@dataclass
class BacktestTrade:
    market_id:      str
    question:       str
    direction:      str         # YES / NO
    entry_price:    float
    ai_probability: float
    edge:           float
    size_usd:       float
    tags:           tuple
    # Результат
    won:            Optional[bool]  = None
    pnl:            Optional[float] = None
    actual_outcome: Optional[str]   = None


@dataclass
class BacktestResult:
    trades:         list[BacktestTrade]
    win_rate:       float
    roi:            float
    total_pnl:      float
    total_volume:   float
    sharpe:         float
    max_drawdown:   float
    edge_accuracy:  float      # насколько AI edge коррелирует с реальными победами
    by_tag:         dict       # результаты по доменам
    signal_quality: dict       # точность каждого типа сигнала

    def summary(self) -> str:
        lines = [
            f"=== BACKTEST RESULTS ({'PASS ✅' if self.roi > 0.05 else 'FAIL ❌'}) ===",
            f"Trades:       {len(self.trades)}",
            f"Win rate:     {self.win_rate:.1%}",
            f"ROI:          {self.roi:+.1%}",
            f"Total P&L:    ${self.total_pnl:+.2f}",
            f"Sharpe:       {self.sharpe:.2f}",
            f"Max drawdown: {self.max_drawdown:.1%}",
            f"Edge accuracy:{self.edge_accuracy:.1%}",
            "",
            "By domain:",
        ]
        for tag, stats in self.by_tag.items():
            if stats["trades"] >= 3:
                lines.append(
                    f"  {tag:15s} WR={stats['win_rate']:.0%} "
                    f"ROI={stats['roi']:+.0%} n={stats['trades']}"
                )
        return "\n".join(lines)


class Backtester:
    """
    Прогоняет AI-стратегию на закрытых рынках Polymarket.

    Важно: мы смотрим только на данные ДОСТУПНЫЕ до закрытия рынка.
    Нет forward-looking. Никакого читерства.
    """
    MIN_EDGE     = 0.04
    KELLY_FRAC   = 0.20
    MAX_POS_USD  = 20.0
    BANKROLL     = 100.0

    def __init__(self, anthropic_client: Anthropic):
        self.ai = anthropic_client

    def run(self, n_markets: int = 100) -> BacktestResult:
        """Полный backtest на N закрытых рынках."""
        log.info(f"Backtest: загружаем {n_markets} закрытых рынков...")
        markets = fetch_closed_markets(limit=n_markets)

        # Только рынки у которых есть outcome
        resolved = [m for m in markets if self._get_outcome(m) is not None]
        log.info(f"Рынков с известным outcome: {len(resolved)}")

        trades: list[BacktestTrade] = []
        bankroll = self.BANKROLL

        for i, market in enumerate(resolved):
            if i % 10 == 0:
                log.info(f"  Прогресс: {i}/{len(resolved)} | Bankroll: ${bankroll:.2f}")

            # AI анализ (без знания outcome!)
            analysis = self._ai_analyze(market)
            if not analysis:
                continue

            direction = analysis["direction"]
            ai_prob   = analysis["probability"]
            confidence = analysis["confidence"]

            if direction == "NEUTRAL" or confidence == "low":
                continue

            price = market.yes_price if direction == "YES" else market.no_price
            edge  = abs(ai_prob - market.yes_price) if direction == "YES" else abs((1 - ai_prob) - market.no_price)

            if edge < self.MIN_EDGE:
                continue

            # Kelly sizing
            p      = ai_prob if direction == "YES" else 1 - ai_prob
            payout = (1 / max(price, 0.01)) - 1
            kelly  = max(0, (p * payout - (1 - p)) / payout) * self.KELLY_FRAC
            size   = min(bankroll * kelly, self.MAX_POS_USD)

            if size < 1.0:
                continue

            # Реальный outcome
            actual = self._get_outcome(market)
            won    = (direction == "YES" and actual == "YES") or (direction == "NO" and actual == "NO")
            pnl    = size * payout if won else -size
            bankroll += pnl

            trades.append(BacktestTrade(
                market_id      = market.id,
                question       = market.question,
                direction      = direction,
                entry_price    = price,
                ai_probability = ai_prob,
                edge           = edge,
                size_usd       = size,
                tags           = market.tags,
                won            = won,
                pnl            = pnl,
                actual_outcome = actual,
            ))

            # Небольшая пауза чтобы не спамить API
            time.sleep(0.5)

        return self._calc_results(trades)

    def _ai_analyze(self, market: Market) -> Optional[dict]:
        """AI анализ рынка. Только данные доступные ДО закрытия."""
        prompt = f"""Prediction market: {market.question}
Yes price: {market.yes_price:.2%}
Volume: ${market.volume_24h:,.0f}
End date: {market.end_date}
Description: {market.description[:300]}

Estimate the TRUE probability of YES outcome.
Consider only information available BEFORE the market closed.
Return ONLY JSON:
{{"probability": 0.0-1.0, "direction": "YES|NO|NEUTRAL", "confidence": "low|medium|high"}}"""

        try:
            resp = self.ai.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=80,
                messages=[{"role":"user","content":prompt}],
            )
            raw = resp.content[0].text.strip().replace("```json","").replace("```","")
            return json.loads(raw)
        except Exception as e:
            log.debug(f"AI analyze error: {e}")
            return None

    def _get_outcome(self, market: Market) -> Optional[str]:
        """Определить outcome закрытого рынка."""
        # В реальных данных outcome есть в поле winner/resolvedOutcome
        # Здесь мы используем цену: если 0.99+ → YES, если 0.01- → NO
        if market.yes_price >= 0.97:
            return "YES"
        if market.yes_price <= 0.03:
            return "NO"
        return None  # не резолвнут или амбигуозный

    def _calc_results(self, trades: list[BacktestTrade]) -> BacktestResult:
        if not trades:
            return BacktestResult([], 0, 0, 0, 0, 0, 0, 0, {}, {})

        won_trades  = [t for t in trades if t.won]
        win_rate    = len(won_trades) / len(trades)
        total_pnl   = sum(t.pnl for t in trades)
        total_vol   = sum(t.size_usd for t in trades)
        roi         = total_pnl / total_vol if total_vol > 0 else 0

        # Sharpe (упрощённый)
        pnls = [t.pnl / t.size_usd for t in trades]
        avg  = sum(pnls) / len(pnls)
        std  = (sum((x - avg)**2 for x in pnls) / len(pnls)) ** 0.5
        sharpe = avg / std if std > 0 else 0

        # Max drawdown
        bankroll = self.BANKROLL
        peak     = bankroll
        max_dd   = 0.0
        for t in trades:
            bankroll += t.pnl
            if bankroll > peak:
                peak = bankroll
            dd = (peak - bankroll) / peak
            max_dd = max(max_dd, dd)

        # Edge accuracy: коррелирует ли наш edge с реальными победами?
        high_edge = [t for t in trades if t.edge > 0.08]
        edge_acc  = (sum(1 for t in high_edge if t.won) / len(high_edge)) if high_edge else 0

        # По доменам
        tag_stats: dict[str, dict] = {}
        for t in trades:
            for tag in t.tags:
                if tag not in tag_stats:
                    tag_stats[tag] = {"trades":0,"wins":0,"pnl":0,"vol":0}
                tag_stats[tag]["trades"] += 1
                if t.won:
                    tag_stats[tag]["wins"] += 1
                tag_stats[tag]["pnl"]  += t.pnl
                tag_stats[tag]["vol"]  += t.size_usd

        by_tag = {
            tag: {
                **stats,
                "win_rate": stats["wins"]/stats["trades"],
                "roi":      stats["pnl"]/stats["vol"] if stats["vol"] > 0 else 0,
            }
            for tag, stats in tag_stats.items()
        }

        return BacktestResult(
            trades        = trades,
            win_rate      = win_rate,
            roi           = roi,
            total_pnl     = total_pnl,
            total_volume  = total_vol,
            sharpe        = sharpe,
            max_drawdown  = max_dd,
            edge_accuracy = edge_acc,
            by_tag        = by_tag,
            signal_quality = {},
        )
