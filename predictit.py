"""
Prometheus — PredictIt Signal
Третья платформа для арбитража.
PredictIt — крупный американский предикшн маркет, особенно силён в политике.
"""
from __future__ import annotations
import logging
from typing import Optional

import requests

log = logging.getLogger("prometheus.predictit")

_S = requests.Session()
_S.headers["User-Agent"] = "Prometheus/3.0"

_CACHE: dict = {}
_CACHE_TS: float = 0.0
_CACHE_TTL: float = 300.0  # 5 минут


def _get_markets() -> list[dict]:
    """Получить все активные рынки PredictIt."""
    import time
    global _CACHE, _CACHE_TS
    if time.time() - _CACHE_TS < _CACHE_TTL and _CACHE:
        return _CACHE.get("markets", [])
    try:
        r = _S.get("https://www.predictit.org/api/marketdata/all/", timeout=8)
        if r.status_code == 200:
            data = r.json()
            markets = data.get("markets", [])
            _CACHE = {"markets": markets}
            _CACHE_TS = time.time()
            log.debug(f"PredictIt: loaded {len(markets)} markets")
            return markets
    except Exception as e:
        log.debug(f"PredictIt fetch error: {e}")
    return _CACHE.get("markets", [])


def find_price(question: str) -> Optional[float]:
    """
    Ищем похожий рынок на PredictIt.
    Возвращает YES цену (0-1) или None.
    """
    markets = _get_markets()
    if not markets:
        return None

    # Ключевые слова из вопроса
    words = set(w.lower() for w in question.split() if len(w) > 3)

    best_match = None
    best_score = 0

    for m in markets:
        name = m.get("name", "").lower()
        score = sum(1 for w in words if w in name)
        if score > best_score and score >= 2:
            best_score = score
            best_match = m

    if not best_match:
        return None

    # Берём цену YES из первого контракта
    for contract in best_match.get("contracts", []):
        name = contract.get("name", "").lower()
        if "yes" in name or name == best_match.get("name", "").lower():
            price = contract.get("bestBuyYesCost") or contract.get("lastTradePrice")
            if price:
                return float(price)
        # Если один контракт — берём напрямую
        if len(best_match.get("contracts", [])) == 1:
            price = contract.get("bestBuyYesCost") or contract.get("lastTradePrice")
            if price:
                return float(price)

    return None


def arbitrage_signal(question: str, polymarket_price: float) -> Optional[dict]:
    """
    Сравниваем цену Polymarket с PredictIt.
    Возвращает сигнал если разница > 3%.
    """
    pi_price = find_price(question)
    if pi_price is None:
        return None

    gap = polymarket_price - pi_price

    if abs(gap) < 0.03:
        return {
            "found": True,
            "pi_price": pi_price,
            "pm_price": polymarket_price,
            "gap": gap,
            "signal": "NEUTRAL",
            "reasoning": f"PredictIt {pi_price:.2%} ≈ Polymarket {polymarket_price:.2%} — markets agree",
        }

    if gap > 0:  # Polymarket дороже → NO на Polymarket
        return {
            "found": True,
            "pi_price": pi_price,
            "pm_price": polymarket_price,
            "gap": gap,
            "signal": "NO",
            "reasoning": f"PM {polymarket_price:.2%} > PredictIt {pi_price:.2%} — Polymarket overpriced by {gap:.1%}",
        }
    else:  # PredictIt дороже → YES на Polymarket
        return {
            "found": True,
            "pi_price": pi_price,
            "pm_price": polymarket_price,
            "gap": gap,
            "signal": "YES",
            "reasoning": f"PredictIt {pi_price:.2%} > PM {polymarket_price:.2%} — Polymarket underpriced by {abs(gap):.1%}",
        }
