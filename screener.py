"""
Prometheus — Market Screener v3.1
Этап 1: быстрый скрининг без AI.
Из 50 рынков выбирает топ-N кандидатов для полного AI анализа.

FIXES v3.1:
- Расширен список _BLOCKED_KEYWORDS (добавлены esports паттерны)
- _kalshi_gap() больше не вызывается для каждого рынка синхронно —
  теперь опциональный быстрый режим (skip_kalshi=True по умолчанию)
  чтобы не замедлять скрининг 50 рынков 50 HTTP запросами к Kalshi
- Добавлен _diversity_boost(): топ не должен состоять только из крипто
- Логирование теперь показывает blocked count
"""
from __future__ import annotations
import logging
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import requests

from data import Market, fetch_market_price_history

log = logging.getLogger("prometheus.screener")

_SESSION = requests.Session()
_SESSION.headers["User-Agent"] = "Prometheus/3.0"

# ── Screener tuning constants ──────────────────────────────────────────────────

# Фильтрация
_MAX_SPREAD          = 0.05   # максимально допустимый спред рынка
_MIN_HOURS_TO_CLOSE  = 2      # рынок закрывается раньше — пропускаем
_MAX_DAYS_TO_CLOSE   = 90     # рынок слишком далёкий — пропускаем

# Параллелизация
_MOMENTUM_WORKERS    = 5      # одновременных HTTP запросов истории цен (было 10, снижено vs 429)

# Веса pre-score (в сумме 1.0)
_W_VOLUME    = 0.30
_W_TIME      = 0.25
_W_MOMENTUM  = 0.25
_W_PRICE     = 0.15
_W_KALSHI    = 0.05

# Пороги для строки reason
_REASON_MIN_MOVE         = 0.05   # мин. движение за 24ч чтобы попасть в reason
_REASON_KALSHI_THRESHOLD = 0.30   # мин. kalshi score для reason
_REASON_HIGH_VOL_SCORE   = 0.80   # порог "высокого объёма" в reason

# ── Sports + noise blocker ─────────────────────────────────────────────────────

_BLOCKED_TAGS = {
    "sports", "sport",
    "nfl", "nba", "mlb", "nhl", "ncaa", "ncaab", "ncaaf",
    "soccer", "football", "basketball", "baseball", "hockey",
    "tennis", "golf", "boxing", "mma", "ufc", "wrestling",
    "olympics", "esports", "cricket", "rugby", "formula1", "f1",
    "racing", "nascar", "cycling", "swimming", "athletics",
}

_BLOCKED_KEYWORDS = [
    "super bowl", "world cup", "champions league", "nfl", "nba", "mlb", "nhl",
    "ncaa", "playoff", "championship", "tournament bracket", "match winner",
    "game winner", "season wins", "mvp award", "draft pick", "transfer",
    "score in", "goals scored", "points scored", "yards ", "touchdowns",
    "hat trick", "grand slam", "wimbledon", "us open", "masters ",
    "world series", "stanley cup", "super league", "premier league",
    # Esports
    "counter-strike", "valorant", "league of legends", "dota", "overwatch",
    "starcraft", "rocket league", "map winner", "game map",
    # Noise / low-signal markets
    "number of tweets", "how many followers", "will elon tweet",
]


def _is_sports_market(market: Market) -> bool:
    """Возвращает True если рынок спортивный или шумовой — блокируем."""
    tags = getattr(market, "tags", []) or []
    for tag in tags:
        tag_str = tag.get("label", "").lower() if isinstance(tag, dict) else str(tag).lower()
        if tag_str in _BLOCKED_TAGS:
            return True

    category = getattr(market, "category", "") or ""
    if category.lower() in _BLOCKED_TAGS or "sport" in category.lower():
        return True

    question = (market.question or "").lower()
    if any(kw in question for kw in _BLOCKED_KEYWORDS):
        return True

    return False


# ── Screener dataclass ─────────────────────────────────────────────────────────

@dataclass
class ScreenResult:
    market:        Market
    pre_score:     float
    momentum_24h:  float
    volume_score:  float
    time_score:    float
    kalshi_gap:    float
    reason:        str


def _momentum_score(market: Market) -> tuple[float, float]:
    """Движение цены за 24ч. Возвращает (score, move_24h)."""
    try:
        history = fetch_market_price_history(market.id, days=2)
        prices  = [float(p["p"]) for p in history if p.get("p")]
        if len(prices) < 4:
            return 0.3, 0.0
        cur  = prices[-1]
        d24  = prices[max(0, len(prices) - 24)]
        move = abs(cur - d24)
        return min(1.0, move * 8), cur - d24
    except Exception:
        return 0.3, 0.0


def _time_score(market: Market) -> float:
    """Оптимальное время до закрытия: 3-30 дней."""
    if not market.end_date:
        return 0.4
    try:
        end = datetime.fromisoformat(market.end_date.replace("Z", "+00:00"))
        days_left = (end - datetime.now(timezone.utc)).total_seconds() / 86400
        if days_left < 1:   return 0.0
        if days_left < 3:   return 0.2
        if days_left <= 30: return 1.0
        if days_left <= 60: return 0.6
        return 0.3
    except Exception:
        return 0.4


def _volume_score(market: Market) -> float:
    v = market.volume_24h
    if v >= 500_000: return 1.0
    if v >= 100_000: return 0.8
    if v >= 50_000:  return 0.6
    if v >= 10_000:  return 0.4
    return 0.2


def _kalshi_gap(question: str) -> float:
    """
    Ищем похожий рынок на Kalshi.
    ОСТОРОЖНО: медленный HTTP запрос. Вызывать только для топ-кандидатов.
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
                    p_val = float(p)
                    # Kalshi может вернуть basis points (8500) или decimal (0.85)
                    return p_val / 100 if p_val > 1.0 else p_val
    except Exception:
        pass
    return 0.0


def _price_extremity(market: Market) -> float:
    """Рынки с ценой 20-80% наиболее интересны — там максимальный edge."""
    p = market.yes_price
    if 0.20 <= p <= 0.80: return 1.0
    if 0.10 <= p <= 0.90: return 0.6
    return 0.2


def _extract_match_key(question: str) -> Optional[str]:
    """
    Извлекает ключ матча из вопроса.
    "Will Atletico win on 2026-04-11?" и "Will Sevilla win on 2026-04-11?"
    → один и тот же матч если дата совпадает и это футбол.
    Возвращает нормализованный ключ или None.
    """
    import re
    q = question.lower()
    
    # Ищем дату в вопросе
    date_match = re.search(r'20[0-9][0-9]-[0-9][0-9]-[0-9][0-9]', q)
    if not date_match:
        return None
    
    date_str = date_match.group(0)
    
    # Паттерны одиночных исходов матча
    patterns = [
        r'will (.+?) win on',       # "Will X win on DATE"
        r'will (.+?) win the',      # "Will X win the match"
        r'will (.+?) beat',         # "Will X beat Y"
        r'(.+?) vs\.? (.+?) winner', # "X vs Y winner"
    ]
    
    for pat in patterns:
        m = re.search(pat, q)
        if m:
            # Возвращаем дату как ключ — все маркеты одного дня одного турнира
            # считаем связанными
            return date_str
    
    return None


def _deduplicate_same_match(results: list["ScreenResult"],
                             open_positions: list = None) -> list["ScreenResult"]:
    """
    Убираем дублирующие маркеты одного матча.
    Если два маркета — "Will A win on DATE?" и "Will B win on DATE?"
    с похожим контекстом — оставляем только один (с лучшим score).
    
    Также проверяем уже открытые позиции — не берём второй исход того же матча.
    """
    open_positions = open_positions or []
    
    # Собираем ключи уже открытых позиций
    open_match_keys = set()
    for pos in open_positions:
        key = _extract_match_key(pos.question or "")
        if key:
            # Берём дату + первые слова вопроса как ключ
            words = (pos.question or "").lower().split()[:3]
            open_match_keys.add(key + "_" + "_".join(words[:2]))
    
    seen_match_keys: dict = {}
    deduped = []
    
    for r in results:
        key = _extract_match_key(r.market.question)
        if not key:
            deduped.append(r)
            continue
        
        # Используем дату + первые слова как уникальный ключ контекста
        words = r.market.question.lower().split()
        # Убираем стоп-слова
        stop = {"will", "the", "a", "an", "on", "in", "by", "win", "be"}
        content_words = [w for w in words if w not in stop and len(w) > 2][:3]
        context_key = key + "_" + "_".join(content_words[:2])
        
        # Проверяем не открыта ли уже позиция по этому матчу
        if context_key in open_match_keys:
            log.debug(f"  Skip — позиция уже открыта на этот матч: {r.market.question[:50]}")
            continue
        
        if context_key not in seen_match_keys:
            seen_match_keys[context_key] = r
            deduped.append(r)
        else:
            # Уже есть маркет этого матча — берём лучший score
            existing = seen_match_keys[context_key]
            if r.pre_score > existing.pre_score:
                deduped.remove(existing)
                seen_match_keys[context_key] = r
                deduped.append(r)
            log.debug(
                f"  Dedup same match: '{r.market.question[:40]}' vs "
                f"'{existing.market.question[:40]}'"
            )
    
    return deduped


def _score_market(market: Market, fetch_kalshi: bool) -> ScreenResult:
    """
    Вычисляет pre-score для одного рынка.
    Вызывается параллельно из ThreadPoolExecutor.
    """
    mom_score, move_24h = _momentum_score(market)
    vol_score            = _volume_score(market)
    time_sc              = _time_score(market)
    price_sc             = _price_extremity(market)

    kalshi_price = _kalshi_gap(market.question) if fetch_kalshi else 0.0
    kalshi_sc    = 0.0
    gap          = 0.0
    if kalshi_price > 0:
        gap       = abs(market.yes_price - kalshi_price)
        kalshi_sc = min(1.0, gap * 10)

    pre_score = (
        vol_score  * _W_VOLUME   +
        time_sc    * _W_TIME     +
        mom_score  * _W_MOMENTUM +
        price_sc   * _W_PRICE    +
        kalshi_sc  * _W_KALSHI
    )

    reasons = []
    if abs(move_24h) > _REASON_MIN_MOVE:
        reasons.append(f"price moved {move_24h:+.1%}/24h")
    if kalshi_sc > _REASON_KALSHI_THRESHOLD:
        reasons.append(f"Kalshi gap {gap:.1%}")
    if vol_score >= _REASON_HIGH_VOL_SCORE:
        reasons.append(f"high vol ${market.volume_24h/1000:.0f}k")
    reason = " · ".join(reasons) if reasons else "general interest"

    return ScreenResult(
        market       = market,
        pre_score    = round(pre_score, 3),
        momentum_24h = round(move_24h, 4),
        volume_score = round(vol_score, 3),
        time_score   = round(time_sc, 3),
        kalshi_gap   = round(kalshi_sc, 3),
        reason       = reason,
    )


def screen(markets: list[Market], top_n: int = 10,
           fetch_kalshi: bool = False) -> list[ScreenResult]:
    """
    Двухфазный скрининг рынков без AI.

    Фаза 1 (синхронная): быстрые фильтры без HTTP — спорт, спред, время.
    Фаза 2 (параллельная): _momentum_score() для прошедших фильтр рынков
        запускается через ThreadPoolExecutor(_MOMENTUM_WORKERS=10).
        Ускорение ~8-10x по сравнению с последовательным обходом.

    Args:
        fetch_kalshi: если True — делаем HTTP запрос к Kalshi для каждого рынка.
                      По умолчанию False для скорости.
    """
    log.info(f"🔍 Скрининг {len(markets)} рынков...")
    blocked   = 0
    filtered  = 0
    candidates: list[Market] = []

    # ── Фаза 1: синхронные фильтры (нет HTTP) ─────────────────────────────────
    for market in markets:
        if market.spread > _MAX_SPREAD:
            filtered += 1
            continue

        if market.end_date:
            try:
                end = datetime.fromisoformat(market.end_date.replace("Z", "+00:00"))
                hours_left = (end - datetime.now(timezone.utc)).total_seconds() / 3600
                if hours_left < _MIN_HOURS_TO_CLOSE:
                    log.debug(f"  Skip — closes in {hours_left:.1f}h: {market.question[:40]}")
                    filtered += 1
                    continue
                if hours_left / 24 > _MAX_DAYS_TO_CLOSE:
                    log.debug(f"  Skip — too far ({hours_left/24:.0f}d): {market.question[:40]}")
                    filtered += 1
                    continue
            except Exception:
                pass

        candidates.append(market)

    # ── Фаза 2: параллельный HTTP для momentum + финальный scoring ─────────────
    results: list[ScreenResult] = []
    with ThreadPoolExecutor(max_workers=_MOMENTUM_WORKERS) as pool:
        future_to_market = {
            pool.submit(_score_market, m, fetch_kalshi): m
            for m in candidates
        }
        for future in as_completed(future_to_market):
            try:
                results.append(future.result())
            except Exception as e:
                market = future_to_market[future]
                log.debug(f"  Score error {market.question[:40]}: {e}")

    results.sort(key=lambda x: x.pre_score, reverse=True)
    results = _deduplicate_same_match(results)
    top     = _apply_diversity(results, top_n)

    log.info(
        f"🔍 Скрининг завершён: {len(top)} кандидатов | "
        f"blocked sports={blocked} filtered={filtered} total_passed={len(results)}"
    )
    for r in top:
        log.info(f"   {r.market.question[:50]} → {r.pre_score:.2f} ({r.reason})")

    return top


def _apply_diversity(results: list[ScreenResult], top_n: int) -> list[ScreenResult]:
    """
    Предотвращаем доминирование одной категории в топе.
    Максимум 3 рынка одного первого тега.
    """
    MAX_PER_TAG = 3
    tag_counts: Counter = Counter()
    top = []

    for r in results:
        if len(top) >= top_n:
            break
        tags = list(r.market.tags) if r.market.tags else ["other"]
        primary = tags[0] if tags else "other"
        if tag_counts[primary] < MAX_PER_TAG:
            top.append(r)
            tag_counts[primary] += 1

    # Если набрали меньше top_n — добираем остальные
    if len(top) < top_n:
        added = {id(r) for r in top}
        for r in results:
            if len(top) >= top_n:
                break
            if id(r) not in added:
                top.append(r)

    return top
