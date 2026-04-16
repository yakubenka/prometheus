"""
Prometheus — Liquidity Filter
Проверяем реальную глубину рынка перед входом.
Мусорные рынки с низкой ликвидностью отсеиваются.
"""
from __future__ import annotations
import logging
from dataclasses import dataclass
from typing import Optional

import requests

log = logging.getLogger("prometheus.liquidity")

CLOB = "https://clob.polymarket.com"
_S   = requests.Session()
_S.headers["User-Agent"] = "Prometheus/3.0"


@dataclass
class LiquiditySnapshot:
    token_id:       str
    best_bid:       float   # лучшая цена покупки
    best_ask:       float   # лучшая цена продажи
    spread:         float   # разница bid/ask
    bid_depth_100:  float   # $ объём в пределах 10% от лучшей цены
    ask_depth_100:  float
    is_liquid:      bool    # прошёл ли фильтр
    reason:         str


def check_liquidity(token_id: str,
                    min_depth_usd: float = 200.0,
                    max_spread:    float = 0.08) -> Optional[LiquiditySnapshot]:
    """
    Проверяет ликвидность токена через Polymarket CLOB.

    Args:
        token_id:      ID токена YES или NO
        min_depth_usd: минимальный объём ордеров рядом с ценой
        max_spread:    максимальный bid-ask spread

    Returns:
        LiquiditySnapshot или None если API недоступен
    """
    if not token_id:
        return None
    try:
        r = _S.get(f"{CLOB}/orderbook/{token_id}", timeout=5)
        if r.status_code != 200:
            return None

        book = r.json()
        bids = [(float(b["price"]), float(b["size"])) for b in book.get("bids", []) if b.get("price") and b.get("size")]
        asks = [(float(a["price"]), float(a["size"])) for a in book.get("asks", []) if a.get("price") and a.get("size")]

        if not bids or not asks:
            return LiquiditySnapshot(token_id, 0, 0, 1.0, 0, 0, False, "empty orderbook")

        best_bid = max(b[0] for b in bids)
        best_ask = min(a[0] for a in asks)
        spread   = best_ask - best_bid

        # Считаем глубину в пределах 10% от лучшей цены
        bid_threshold = best_bid * 0.90
        ask_threshold = best_ask * 1.10
        bid_depth = sum(p*s for p, s in bids if p >= bid_threshold)
        ask_depth = sum(p*s for p, s in asks if p <= ask_threshold)

        # Фильтр
        if spread > max_spread:
            return LiquiditySnapshot(token_id, best_bid, best_ask, spread,
                                     bid_depth, ask_depth, False,
                                     f"spread too wide: {spread:.3f} > {max_spread}")
        if min(bid_depth, ask_depth) < min_depth_usd:
            return LiquiditySnapshot(token_id, best_bid, best_ask, spread,
                                     bid_depth, ask_depth, False,
                                     f"insufficient depth: ${min(bid_depth, ask_depth):.0f} < ${min_depth_usd}")

        return LiquiditySnapshot(token_id, best_bid, best_ask, spread,
                                 bid_depth, ask_depth, True,
                                 f"OK spread={spread:.3f} depth=${min(bid_depth, ask_depth):.0f}")

    except Exception as e:
        log.debug(f"Liquidity check failed for {token_id}: {e}")
        return None


def liquidity_size_mult(snap: LiquiditySnapshot) -> float:
    """
    Непрерывный множитель размера позиции по качеству ликвидности.
    Используется в Kelly sizing ДО финального кэпа.

    Spread penalty:  > 2% начинает снижать размер, линейно до 0.30 при 8%+
    Depth bonus:     меньше $300 в стакане → снижаем размер
    Итог:            хорошая ликвидность = 1.0, тонкий рынок = 0.20–0.50
    """
    # Штраф за широкий спред
    spread_penalty = max(0.30, 1.0 - max(0.0, snap.spread - 0.02) * 11.67)

    # Награда за глубину: $500+ = полный размер
    min_depth = min(snap.bid_depth_100, snap.ask_depth_100)
    depth_mult = min(1.0, min_depth / 500.0)

    mult = round(spread_penalty * (0.40 + 0.60 * depth_mult), 3)
    return max(0.20, mult)


def is_market_liquid(market, direction: str,
                     min_depth_usd: float = 150.0) -> tuple[bool, str]:
    """
    Проверяет ликвидность рынка для конкретного направления.
    Возвращает (is_liquid, reason).
    """
    token_id = market.token_id_yes if direction == "YES" else market.token_id_no
    if not token_id:
        # Нет token_id — не можем проверить, пропускаем фильтр
        return True, "no token_id — skipping check"

    snap = check_liquidity(token_id, min_depth_usd=min_depth_usd)
    if snap is None:
        # API недоступен — не блокируем
        return True, "liquidity API unavailable — allowing"

    return snap.is_liquid, snap.reason
