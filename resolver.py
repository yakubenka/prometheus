"""
Prometheus v3.2 — Position Resolver

FIXES v3.2:
- Использует tg.position_closed() вместо raw tg() текста
- tg передаётся как объект Telegram, не как функция send
- stop-loss корректный PNL для NO направлений
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


def check_market_outcome(market_id: str, current_price: float = None) -> Optional[str]:
    if current_price is not None:
        if current_price >= 0.99:  return "YES"
        if current_price <= 0.01:  return "NO"
    try:
        r = requests.get(f"{GAMMA}/markets/{market_id}", timeout=8)
        if r.status_code != 200:
            return None
        m = r.json()
        winner = m.get("winner") or m.get("resolvedOutcome") or m.get("resolution")
        if winner:
            w = str(winner).upper()
            if w in ("YES", "1", "TRUE"):  return "YES"
            if w in ("NO",  "0", "FALSE"): return "NO"
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
    try:
        r = requests.get(f"{CLOB}/midpoint", params={"token_id": token_id}, timeout=5)
        if r.status_code == 200:
            return float(r.json().get("mid", 0))
    except Exception:
        pass
    return None


class PositionResolver:
    def __init__(self, risk_manager, telegram=None):
        self.risk = risk_manager
        # telegram может быть объектом Telegram или функцией send
        self.tg   = telegram
        self._signal_outcomes: list[dict] = []

    def _notify(self, pos, outcome: str, won: bool, pnl: float,
                exit_price: float, loss_pct: float = None) -> None:
        """Отправить уведомление о закрытии позиции."""
        if not self.tg:
            return
        try:
            # Строим URL по slug позиции
            import urllib.parse as _up
            slug = getattr(pos, 'slug', None)
            if slug:
                pm_url = f"https://polymarket.com/event/{slug}"
            else:
                words  = ' '.join((pos.question or '').split()[:6])
                pm_url = f"https://polymarket.com/markets?_s={_up.quote(words)}"

            if hasattr(self.tg, "position_closed"):
                self.tg.position_closed(
                    question    = pos.question,
                    direction   = pos.direction,
                    entry_price = pos.entry_price,
                    exit_price  = exit_price,
                    size        = pos.size_usd,
                    pnl         = pnl,
                    outcome     = outcome,
                    signal_type = pos.signal_type,
                    url         = pm_url,
                )
            else:
                # Fallback: tg — это функция send()
                icon   = "✅" if won else ("🛑" if outcome == "STOP_LOSS" else "❌")
                result = "WIN" if won else ("STOP-LOSS" if outcome == "STOP_LOSS" else "LOSS")
                pnl_s  = f"+${pnl:.2f}" if won else f"−${abs(pnl):.2f}"
                msg = (
                    f"{icon} *{result}*\n\n"
                    f"*{pos.question[:70]}*\n\n"
                    f"Ставка: *{pos.direction}*  |  Исход: *{outcome}*\n"
                    f"P&L: *{pnl_s}*\n\n"
                    f"🔗 [Polymarket]({pm_url})"
                )
                if loss_pct is not None:
                    msg += f"\nПотери: *{loss_pct:.0%}*"
                self.tg(msg)
        except Exception as e:
            log.debug(f"Telegram notify error: {e}")

    def run(self) -> list[dict]:
        open_pos = self.risk.open_positions
        if not open_pos:
            return []

        closed_now = []
        for pos in open_pos:
            cur_price = get_current_price(pos.token_id) if pos.token_id else None

            # 1. Резолюция рынка
            outcome = check_market_outcome(pos.market_id, current_price=cur_price)
            if outcome is not None:
                won = (
                    (pos.direction == "YES" and outcome == "YES") or
                    (pos.direction == "NO"  and outcome == "NO")
                )
                exit_p  = 1.0 if won else 0.0
                closed  = self.risk.close(pos.market_id, exit_price=exit_p, won=won)
                if closed:
                    closed_now.append(self._make_closed_dict(pos, outcome, won, closed.pnl))
                    self._notify(pos, outcome, won, closed.pnl, exit_p)
                    log.info(f"{'WIN' if won else 'LOSS'} | {pos.question[:50]} | P&L ${closed.pnl:+.2f}")
                continue

            # 2. Stop-loss — потеряли 40%+ от входной цены
            if cur_price is not None and cur_price > 0:
                entry_token   = pos.entry_price if pos.direction == "YES" else 1.0 - pos.entry_price
                current_token = cur_price       if pos.direction == "YES" else 1.0 - cur_price
                loss_pct = (entry_token - current_token) / entry_token if entry_token > 0 else 0

                if loss_pct >= 0.40:
                    pnl    = round((current_token - entry_token) / max(entry_token, 0.01) * pos.size_usd, 2)
                    closed = self.risk.close_with_pnl(pos.market_id, exit_price=cur_price, pnl=pnl)
                    if closed:
                        closed_now.append(self._make_closed_dict(pos, "STOP_LOSS", False, pnl))
                        self._notify(pos, "STOP_LOSS", False, pnl, cur_price, loss_pct)
                        log.info(f"🛑 STOP-LOSS | {pos.question[:50]} | -{loss_pct:.0%} | P&L ${pnl:+.2f}")

        self._signal_outcomes.extend(closed_now)
        return closed_now

    def _make_closed_dict(self, pos, outcome, won, pnl) -> dict:
        return {
            "market_id":   pos.market_id,
            "question":    pos.question,
            "direction":   pos.direction,
            "entry_price": pos.entry_price,
            "outcome":     outcome,
            "won":         won,
            "pnl":         pnl,
            "signal_type": pos.signal_type,
            "tags":        pos.tags,
        }

    def signal_performance(self) -> dict:
        perf: dict = {}
        for rec in self._signal_outcomes:
            stype = rec.get("signal_type", "ai")
            if stype not in perf:
                perf[stype] = {"wins": 0, "total": 0, "pnl": 0.0}
            perf[stype]["total"] += 1
            if rec["won"]:
                perf[stype]["wins"] += 1
            perf[stype]["pnl"] += rec.get("pnl", 0)
        return {
            k: {**v,
                "win_rate": round(v["wins"] / v["total"], 3) if v["total"] else 0,
                "roi":      round(v["pnl"] / max(1, v["total"]) / 10, 3),
               }
            for k, v in perf.items()
        }

    def unrealised_pnl(self) -> dict:
        result = {}
        for pos in self.risk.open_positions:
            if not pos.token_id:
                result[pos.market_id] = 0.0
                continue
            current = get_current_price(pos.token_id)
            if current is None:
                result[pos.market_id] = 0.0
                continue
            entry = pos.entry_price if pos.direction == "YES" else 1 - pos.entry_price
            cur   = current         if pos.direction == "YES" else 1 - current
            pnl   = (cur - entry) * pos.size_usd / max(entry, 0.01)
            result[pos.market_id] = round(pnl, 2)
        return result
