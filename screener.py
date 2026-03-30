"""
Prometheus — Market Screener
Этап 1: быстрый скрининг без AI.
Из 50 рынков выбирает топ-N кандидатов для полного AI анализа.
"""
from __future__ import annotations
import logging
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests

from data import Market, fetch_market_price_history

log = logging.getLogger("prometheus.screener")

_SESSION = requests.Session()
_SESSION.headers["User-Agent"] = "Prometheus/3.0"


@dataclass
class ScreenResult:
    market:        Market
    pre_score:     float          # 0.0 – 1.0 итоговый скор
    momentum_24h:  float          # движение цены за 24ч
    volume_score:  float          # нормализованный объём
    time_score:    float          # оптимальность времени до закрытия
    kalshi_gap:    float          # разрыв с Kalshi (0 если нет данных)
    reason:        str            # почему попал в топ


def _momentum_score(market: Market) -> tuple[float, float]:
    """Движение цены за 24ч. Возвращает (score, move_24h)."""
    try:
        history = fetch_market_price_history(market.id, days=2)
        prices  = [float(p["p"]) for p in history if p.get("p")]
        if len(prices) < 4:
            return 0.3, 0.0
        cur   = prices[-1]
        d24   = prices[max(0, len(prices) - 24)]
        move  = abs(cur - d24)
        # Большое движение = интересный рынок
        score = min(1.0, move * 8)
        return score, cur - d24
    except Exception:
        return 0.3, 0.0


def _time_score(market: Market) -> float:
    """
    Оптимальное время до закрытия: 3-30 дней.
    Слишком близко (< 3 дней) — мало времени для движения.
    Слишком далеко (> 60 дней) — деньги заморожены надолго.
    """
    if not market.end_date:
        return 0.4
    try:
        end = datetime.fromisoformat(market.end_date.replace("Z", "+00:00"))
        days_left = (end - datetime.now(timezone.utc)).total_seconds() / 86400
        if days_left < 1:   return 0.0   # закрывается завтра
        if days_left < 3:   return 0.2
        if days_left <= 30: return 1.0   # идеально
        if days_left <= 60: return 0.6
        return 0.3                        # слишком далеко
    except Exception:
        return 0.4


def _volume_score(market: Market) -> float:
    """Объём — жидкие рынки легче торговать."""
    v = market.volume_24h
    if v >= 500_000: return 1.0
    if v >= 100_000: return 0.8
    if v >= 50_000:  return 0.6
    if v >= 10_000:  return 0.4
    return 0.2


def _kalshi_gap(question: str) -> float:
    """
    Ищем похожий рынок на Kalshi.
    Если есть разрыв в ценах — это потенциальный арбитраж.
    """
    try:
        words = [w for w in question.lower().split() if len(w) > 3][:4]
        r = _SESSION.get(
            "https://trading-api.kalshi.com/trade-api/v2/markets",
            params={"status": "open", "limit": 10, "search": " ".join(words)},
            timeout=4,
        )
        if r.status_code != 200:
            return 0.0
        for m in r.json().get("markets", []):
            title = m.get("title", "").lower()
            if sum(1 for w in words if w in title) >= 2:
                p = m.get("yes_ask") or m.get("last_price")
                if p:
                    return float(p) / 100
    except Exception:
        pass
    return 0.0


def _price_extremity(market: Market) -> float:
    """
    Рынки с ценой близкой к 0 или 1 менее интересны —
    там мало возможностей для edge.
    Лучшие возможности в диапазоне 20-80%.
    """
    p = market.yes_price
    if 0.20 <= p <= 0.80:
        return 1.0
    if 0.10 <= p <= 0.90:
        return 0.6
    return 0.2


def screen(markets: list[Market], top_n: int = 10) -> list[ScreenResult]:
    """
    Быстро скрининг всех рынков без AI.
    Возвращает топ-N кандидатов для полного анализа.
    """
    log.info(f"🔍 Скрининг {len(markets)} рынков...")
    results = []

    for market in markets:
        # Пропускаем рынки с плохим спредом
        if market.spread > 0.05:
            continue

        mom_score, move_24h = _momentum_score(market)
        vol_score            = _volume_score(market)
        time_sc              = _time_score(market)
        price_sc             = _price_extremity(market)

        # Kalshi gap (опционально — может быть 0)
        kalshi_price = _kalshi_gap(market.question)
        if kalshi_price > 0:
            gap = abs(market.yes_price - kalshi_price)
            kalshi_sc = min(1.0, gap * 10)  # 10% gap = max score
        else:
            kalshi_sc = 0.0

        # Итоговый pre-score
        pre_score = (
            vol_score   * 0.30 +
            time_sc     * 0.25 +
            mom_score   * 0.25 +
            price_sc    * 0.15 +
            kalshi_sc   * 0.05
        )

        # Причина интереса
        reasons = []
        if abs(move_24h) > 0.05:
            reasons.append(f"price moved {move_24h:+.1%}/24h")
        if kalshi_sc > 0.3:
            reasons.append(f"Kalshi gap {gap:.1%}")
        if vol_score >= 0.8:
            reasons.append(f"high vol ${market.volume_24h/1000:.0f}k")
        reason = " · ".join(reasons) if reasons else "general interest"

        results.append(ScreenResult(
            market       = market,
            pre_score    = round(pre_score, 3),
            momentum_24h = round(move_24h, 4),
            volume_score = round(vol_score, 3),
            time_score   = round(time_sc, 3),
            kalshi_gap   = round(kalshi_sc, 3),
            reason       = reason,
        ))

    # Сортируем по pre_score, берём топ-N
    results.sort(key=lambda x: x.pre_score, reverse=True)
    top = results[:top_n]

    log.info(f"🔍 Скрининг завершён: {len(top)} кандидатов из {len(results)}")
    for r in top:
        log.info(f"   {r.market.question[:50]} → {r.pre_score:.2f} ({r.reason})")

    return top
