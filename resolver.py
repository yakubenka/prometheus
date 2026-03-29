"""
Prometheus v3 — Position Resolver
Автоматически закрывает позиции когда рынки резолвятся.
Обновляет P&L. Запускает self-improvement loop.
"""

from __future__ import annotations
import json
import logging
from datetime import datetime, timezone
from typing import Optional
import requests

from data import fetch_current_price

log = logging.getLogger("prometheus.resolver")

GAMMA = "https://gamma-api.polymarket.com"
CLOB  = "https://clob.polymarket.com"


def check_market_outcome(market_id: str) -> Optional[str]:
    """
    Проверить резолюцию рынка.
    Возвращает "YES" / "NO" / None (ещё не резолвнут).
    """
    try:
        r = requests.get(f"{GAMMA}/markets/{market_id}", timeout=8)
        if r.status_code != 200:
            return None
        m = r.json()

        # Прямые поля резолюции
        winner = m.get("winner") or m.get("resolvedOutcome") or m.get("resolution")
        if winner:
            w = str(winner).upper()
            if w in ("YES", "1", "TRUE"):  return "YES"
            if w in ("NO",  "0", "FALSE"): return "NO"

        # По цене: если рынок закрыт и цена ~1.0 или ~0.0
        if m.get("closed") or m.get("active") == False:
            prices = m.get("outcomePrices")
            if isinstance(prices, str):
                prices = json.loads(prices)
            if prices:
                yes_price = float(prices[0])
                if yes_price >= 0.97: return "YES"
                if yes_price <= 0.03: return "NO"

        return None
    except Exception as e:
        log.debug(f"check_market_outcome error {market_id}: {e}")
        return None


def get_current_price(token_id: str) -> Optional[float]:
    """Текущая mid-цена токена для unrealised P&L."""
    try:
        r = requests.get(
            f"{CLOB}/midpoint",
            params={"token_id": token_id},
            timeout=5,
        )
        if r.status_code == 200:
            return float(r.json().get("mid", 0))
    except Exception:
        pass
    return None


class PositionResolver:
    """
    Запускается в каждом цикле бота.
    Проверяет открытые позиции → закрывает резолвнутые → считает P&L.
    """

    def __init__(self, risk_manager, telegram_fn=None):
        self.risk    = risk_manager
        self.tg      = telegram_fn or (lambda msg: None)
        self._signal_outcomes: list[dict] = []   # для self-improvement

    def run(self) -> list[dict]:
        """
        Проверить все открытые позиции.
        Возвращает список закрытых позиций (для обновления весов сигналов).
        """
        open_pos = self.risk.open_positions
        if not open_pos:
            return []

        closed_now = []
        for pos in open_pos:
            outcome = check_market_outcome(pos.market_id)
            if outcome is None:
                continue  # ещё не резолвнут

            won = (
                (pos.direction == "YES" and outcome == "YES") or
                (pos.direction == "NO"  and outcome == "NO")
            )

            closed = self.risk.close(pos.market_id, exit_price=1.0 if won else 0.0, won=won)
            if closed:
                closed_now.append({
                    "market_id":    pos.market_id,
                    "question":     pos.question,
                    "direction":    pos.direction,
                    "entry_price":  pos.entry_price,
                    "outcome":      outcome,
                    "won":          won,
                    "pnl":          closed.pnl,
                    "signal_type":  pos.signal_type,
                    "tags":         pos.tags,
                })

                emoji = "✅" if won else "❌"
                self.tg(
                    f"{emoji} *Позиция закрыта*\n\n"
                    f"{pos.question[:70]}\n\n"
                    f"Исход: *{outcome}* | Наша ставка: *{pos.direction}*\n"
                    f"P&L: *${closed.pnl:+.2f}*"
                )
                log.info(
                    f"{'WIN' if won else 'LOSS'} | {pos.question[:50]} | "
                    f"P&L ${closed.pnl:+.2f}"
                )

        # Записать для self-improvement
        self._signal_outcomes.extend(closed_now)
        return closed_now

    def signal_performance(self) -> dict[str, dict]:
        """
        Агрегировать точность по типу сигнала.
        Используется для обновления весов SignalEngine.
        """
        perf: dict[str, dict] = {}
        for rec in self._signal_outcomes:
            stype = rec.get("signal_type", "ai")
            if stype not in perf:
                perf[stype] = {"wins": 0, "total": 0, "pnl": 0.0}
            perf[stype]["total"] += 1
            if rec["won"]:
                perf[stype]["wins"] += 1
            perf[stype]["pnl"] += rec.get("pnl", 0)

        return {
            k: {
                **v,
                "win_rate": round(v["wins"] / v["total"], 3) if v["total"] else 0,
                "roi":      round(v["pnl"] / max(1, v["total"]) / 10, 3),
            }
            for k, v in perf.items()
        }

    def unrealised_pnl(self) -> dict[str, float]:
        """Посчитать unrealised P&L по открытым позициям."""
        result = {}
        for pos in self.risk.open_positions:
            if not pos.token_id:
                result[pos.market_id] = 0.0
                continue
            current = get_current_price(pos.token_id)
            if current is None:
                result[pos.market_id] = 0.0
                continue
            # P&L = (current_price - entry_price) / entry_price * size
            entry = pos.entry_price if pos.direction == "YES" else 1 - pos.entry_price
            pnl   = (current - entry) * pos.size_usd / max(entry, 0.01)
            result[pos.market_id] = round(pnl, 2)
        return result
