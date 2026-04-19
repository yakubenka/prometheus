"""
Prometheus — Signal Engine v5.0
Decision engine стал более датовым и экономным по AI:

- сначала считаем не-AI сигналы
- AI вызывается редко и только как sanity check
- итоговый вход требует quality score и внешнее подтверждение
"""
from __future__ import annotations
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, TYPE_CHECKING

import requests
from anthropic import Anthropic

from config import cfg
from data import Market, fetch_market_price_history, fetch_order_book
from econ_calendar import imminent_release_for

if TYPE_CHECKING:
    from intel import IntelPipeline

log = logging.getLogger("prometheus.signals")

_S = requests.Session()
_S.headers["User-Agent"] = "Prometheus/5.0"

_MODEL_FAST = "claude-haiku-4-5-20251001"

_DOMAIN_MIN_EDGE = {
    "crypto":      0.10,
    "sports":      0.08,
    "macro":       0.06,
    "politics":    0.04,
    "geopolitics": 0.05,
    "tech":        0.06,
    "other":       0.05,
}

_CONF_KELLY = {
    "high":   1.00,
    "medium": 0.70,
    "low":    0.40,
}

_AI_SIGNAL_NAMES = {"ai_guard"}
_NON_AI_SIGNAL_NAMES = {"momentum", "consensus", "predictit", "volume_spike",
                        "book_depth", "calibration", "base_rate", "sentiment"}


def get_domain_min_edge(tags: list, default: float = 0.05) -> float:
    for tag in (tags or []):
        t = str(tag).lower()
        if t in _DOMAIN_MIN_EDGE:
            return _DOMAIN_MIN_EDGE[t]
    return default


def get_confidence_kelly_mult(confidence: str) -> float:
    return _CONF_KELLY.get(confidence, 0.50)


@dataclass
class Signal:
    name:       str
    score:      float
    confidence: float
    direction:  str
    reasoning:  str
    weight:     float = 0.0


@dataclass
class EnsembleResult:
    final_score:    float
    direction:      str
    edge:           float
    ai_probability: float
    confidence:     str
    signals:        list[Signal] = field(default_factory=list)
    reasoning:      str = ""
    question_type:  str = "general"
    trade_quality:  int = 0
    strategy_type:  str = "market_data"
    ai_used:        bool = False
    ai_calls_used:  int = 0


def _parse_ai_json(text: str) -> dict:
    clean = text.strip().replace("```json", "").replace("```", "").strip()
    return json.loads(clean)


def _classify_question(question: str) -> str:
    q = (question or "").lower()
    if any(w in q for w in ["election", "vote", "president", "senator", "congress", "poll"]):
        return "electoral"
    if any(w in q for w in ["fed", "rate", "inflation", "gdp", "cpi", "recession", "market"]):
        return "economic"
    if any(w in q for w in ["war", "attack", "iran", "russia", "china", "ukraine", "military", "nato"]):
        return "geopolitical"
    if any(w in q for w in ["bitcoin", "btc", "eth", "crypto", "solana"]):
        return "crypto"
    if any(w in q for w in ["will", "reach", "exceed", "above", "below", "by"]):
        return "binary"
    return "general"


def _build_market_snapshot(market: Market, intel=None) -> str:
    parts = [
        f"Market: {market.question}",
        f"Current yes price: {market.yes_price:.3f}",
        f"24h volume: ${market.volume_24h:,.0f}",
    ]
    if market.description:
        parts.append(f"Resolution rules: {market.description[:300]}")
    if market.end_date:
        try:
            end = datetime.fromisoformat(market.end_date.replace("Z", "+00:00"))
            days_left = max(0.0, (end - datetime.now(timezone.utc)).total_seconds() / 86400)
            parts.append(f"Days left: {days_left:.1f}")
        except Exception:
            pass
    if intel:
        try:
            news = intel.context_for(market.question, hours=24)
            if news and "No recent" not in news:
                parts.append(f"Recent news:\n{news[:500]}")
        except Exception:
            pass
    return "\n".join(parts)


def _detect_volume_anomaly(market: Market) -> Optional[Signal]:
    try:
        r = _S.get("https://gamma-api.polymarket.com/markets", params={"id": market.id}, timeout=5)
        if r.status_code != 200:
            return None
        data = r.json()
        items = data if isinstance(data, list) else data.get("data", [data])
        if not items:
            return None
        m = items[0] if isinstance(items, list) else items
        vol_1h = float(m.get("volume1hr", 0) or 0)
        vol_24h = float(m.get("volume24hr", 0) or market.volume_24h or 0)
        if vol_1h <= 0 or vol_24h <= 0:
            return None
        expected_1h = vol_24h / 24
        if expected_1h <= 0:
            return None
        spike = vol_1h / expected_1h
        if spike >= 6.0:
            return Signal("volume_spike", market.yes_price, 0.70, "NEUTRAL", f"Volume spike {spike:.1f}x")
        if spike >= 3.5:
            return Signal("volume_spike", market.yes_price, 0.45, "NEUTRAL", f"Volume elevated {spike:.1f}x")
        return None
    except Exception as e:
        log.debug(f"Volume anomaly error: {e}")
        return None


class SignalEngine:
    DEFAULT_WEIGHTS = {
        "momentum":     0.18,
        "consensus":    0.28,
        "predictit":    0.20,
        "volume_spike": 0.06,
        "book_depth":   0.08,
        "calibration":  0.06,
        "base_rate":    0.06,
        "sentiment":    0.06,
        "ai_guard":     0.14,
    }

    # ── Category-specific weight overrides ────────────────────────────────────
    # Не заменяют DEFAULT_WEIGHTS полностью — только патчат ключи.
    # Обоснование каждого профиля:
    #
    # electoral: опросы → calibration важна (longshot кандидаты переоценены),
    #            consensus/predictit сильны, momentum слаб (цены вязкие)
    #
    # economic: book_depth и momentum важны (реакция рынка на данные быстрая),
    #           base_rate важна (исторические паттерны ставок ФРС хорошо работают)
    #
    # geopolitical: sentiment из новостей критичен, consensus слаб (Kalshi мало рынков),
    #               calibration важна (события сильно переоценены рынком)
    #
    # crypto: book_depth максимален (крипто-рынок отзывчив к стакану),
    #         momentum силён, calibration слабее (меньше longshot bias)
    #
    # binary / general: DEFAULT_WEIGHTS без изменений
    _CATEGORY_WEIGHT_PATCHES: dict[str, dict[str, float]] = {
        "electoral": {
            "momentum":    0.10,
            "consensus":   0.32,
            "predictit":   0.24,
            "calibration": 0.10,
            "base_rate":   0.08,
            "sentiment":   0.06,
            "book_depth":  0.04,
            "ai_guard":    0.14,
        },
        "economic": {
            "momentum":    0.22,
            "consensus":   0.26,
            "predictit":   0.16,
            "book_depth":  0.12,
            "base_rate":   0.10,
            "calibration": 0.04,
            "sentiment":   0.06,
            "ai_guard":    0.14,
        },
        "geopolitical": {
            "sentiment":   0.14,
            "calibration": 0.12,
            "base_rate":   0.10,
            "momentum":    0.14,
            "consensus":   0.20,
            "predictit":   0.14,
            "book_depth":  0.04,
            "ai_guard":    0.16,
        },
        "crypto": {
            "book_depth":  0.16,
            "momentum":    0.24,
            "consensus":   0.22,
            "predictit":   0.12,
            "sentiment":   0.08,
            "calibration": 0.04,
            "base_rate":   0.04,
            "ai_guard":    0.12,
        },
    }

    def __init__(self, ai: Optional[Anthropic], intel: Optional["IntelPipeline"] = None) -> None:
        self.ai = ai
        self.intel = intel
        self.weights = dict(self.DEFAULT_WEIGHTS)
        self._ai_budget_remaining = cfg.ai_request_budget_per_cycle

    def _weights_for(self, question_type: str) -> dict[str, float]:
        """
        Возвращает веса сигналов для данной категории вопроса.
        Если есть patch — берём его, иначе current self.weights (с адаптацией от learning).
        """
        patch = self._CATEGORY_WEIGHT_PATCHES.get(question_type)
        if not patch:
            return self.weights
        # Мержим: patch переопределяет DEFAULT, learning-адаптация поверх
        merged = dict(self.DEFAULT_WEIGHTS)
        merged.update(patch)
        # Применяем relative scaling от learning: если learning поднял signal X
        # на 20% от дефолта, применяем тот же +20% к категорийному весу
        for name in merged:
            default = self.DEFAULT_WEIGHTS.get(name, 0.05)
            if default > 0:
                ratio = self.weights.get(name, default) / default
                merged[name] = round(merged[name] * ratio, 4)
        return merged

    def reset_cycle_budget(self) -> None:
        self._ai_budget_remaining = cfg.ai_request_budget_per_cycle

    def analyze(self, market: Market, pre_score: float = 0.0) -> EnsembleResult:
        question_type  = _classify_question(market.question)
        cat_weights    = self._weights_for(question_type)
        signals        = self._run_non_ai_parallel(market, cat_weights)
        initial        = self._ensemble(market, signals)
        quality        = self._trade_quality(market, initial, signals, pre_score)
        strategy       = self._strategy_type(initial.direction, signals, ai_used=False)

        ai_signal = None
        if self._should_call_ai(initial, quality, signals):
            ai_signal = self._ai_guard(market)
            if ai_signal:
                ai_signal.weight = cat_weights.get("ai_guard", 0.14)
                signals.append(ai_signal)
                self._ai_budget_remaining = max(0, self._ai_budget_remaining - 1)

        result = self._ensemble(market, signals)
        result.question_type = question_type
        result.trade_quality = self._trade_quality(market, result, signals, pre_score)
        result.strategy_type = self._strategy_type(result.direction, signals, ai_used=ai_signal is not None)
        result.ai_used = ai_signal is not None
        result.ai_calls_used = 1 if ai_signal is not None else 0

        if result.trade_quality < cfg.min_trade_quality:
            result.direction = "NEUTRAL"
            result.confidence = "low"
            result.reasoning = f"quality={result.trade_quality} below threshold"
        elif result.strategy_type == "ai_assisted" and result.edge < max(cfg.ai_unconfirmed_edge, get_domain_min_edge(list(market.tags), cfg.min_edge)):
            result.direction = "NEUTRAL"
            result.confidence = "low"
            result.reasoning = "AI-assisted setup without enough edge"

        return result

    def _run_non_ai_parallel(
        self, market: Market, weights: dict[str, float] | None = None
    ) -> list[Signal]:
        if weights is None:
            weights = self.weights
        tasks = {
            "momentum":    lambda: self._momentum(market),
            "consensus":   lambda: self._consensus(market),
            "predictit":   lambda: self._predictit(market),
            "volume_spike":lambda: _detect_volume_anomaly(market) or Signal("volume_spike", market.yes_price, 0.10, "NEUTRAL", "no anomaly"),
            "book_depth":  lambda: self._book_depth(market),
            "calibration": lambda: self._calibration(market),
            "base_rate":   lambda: self._base_rate(market),
            "sentiment":   lambda: self._sentiment(market),
        }
        results: dict[str, Signal] = {}
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(fn): name for name, fn in tasks.items()}
            for future in as_completed(futures, timeout=20):
                name = futures[future]
                try:
                    results[name] = future.result()
                except Exception as e:
                    log.debug(f"Signal {name} failed: {e}")
                    results[name] = Signal(name, market.yes_price, 0.10, "NEUTRAL", "error")
        out = []
        for name in tasks:
            sig = results.get(name, Signal(name, market.yes_price, 0.10, "NEUTRAL", "error"))
            sig.weight = weights.get(sig.name, 0.05)   # category-specific weight
            out.append(sig)
        return out

    def _momentum(self, market: Market) -> Signal:
        try:
            history = fetch_market_price_history(market.id, days=3)
            prices = [float(p["p"]) for p in history if p.get("p")]
            if len(prices) < 6:
                return Signal("momentum", market.yes_price, 0.2, "NEUTRAL", "insufficient history")
            cur = prices[-1]
            d24 = prices[max(0, len(prices) - 24)]
            d72 = prices[0]
            m24 = cur - d24
            m72 = cur - d72
            if m24 > 0.08 and m72 > 0.10:
                return Signal("momentum", min(0.90, cur + m24 * 0.20), 0.60, "YES", f"up {m24:.1%}/24h")
            if m24 < -0.08 and m72 < -0.10:
                return Signal("momentum", max(0.10, cur + m24 * 0.20), 0.60, "NO", f"down {abs(m24):.1%}/24h")
            return Signal("momentum", cur, 0.25, "NEUTRAL", f"flat {m24:+.1%}/24h")
        except Exception as e:
            log.debug(f"Momentum error: {e}")
            return Signal("momentum", market.yes_price, 0.15, "NEUTRAL", "error")

    def _consensus(self, market: Market) -> Signal:
        """
        Кросс-биржевой консенсус: Polymarket vs Kalshi + Manifold Markets.
        Чем больше внешних источников подтверждают ценовой gap — тем выше уверенность.
        """
        prices: list[tuple[str, float]] = []  # (source, price)

        kalshi = self._kalshi_price(market.question)
        if kalshi is not None:
            prices.append(("Kalshi", kalshi))

        manifold = self._manifold_price(market.question)
        if manifold is not None:
            prices.append(("Manifold", manifold))

        if not prices:
            return Signal("consensus", market.yes_price, 0.10, "NEUTRAL", "no external match")

        # Взвешенный консенсус: Kalshi весит больше (реальные деньги)
        source_weights = {"Kalshi": 0.65, "Manifold": 0.35}
        total_w  = sum(source_weights.get(s, 0.50) for s, _ in prices)
        ext_price = sum(source_weights.get(s, 0.50) * p for s, p in prices) / total_w

        spread = market.yes_price - ext_price
        sources_str = "+".join(s for s, _ in prices)

        if abs(spread) < 0.02:
            return Signal("consensus", ext_price, 0.60, "NEUTRAL",
                          f"PM≈{sources_str} {spread:+.1%}")
        # Больше источников → выше уверенность
        conf_mult = 1.0 + 0.15 * (len(prices) - 1)  # 2 источника: +15%
        if spread > 0:
            conf = min(0.92, abs(spread) * 9 * conf_mult)
            return Signal("consensus", ext_price, conf, "NO",
                          f"PM rich vs {sources_str} by {spread:.1%}")
        conf = min(0.92, abs(spread) * 9 * conf_mult)
        return Signal("consensus", ext_price, conf, "YES",
                      f"PM cheap vs {sources_str} by {abs(spread):.1%}")

    def _predictit(self, market: Market) -> Signal:
        try:
            from predictit import arbitrage_signal
            result = arbitrage_signal(market.question, market.yes_price)
            if result is None:
                return Signal("predictit", market.yes_price, 0.10, "NEUTRAL", "no PredictIt match")
            if result["signal"] == "NEUTRAL":
                return Signal("predictit", result["pi_price"], 0.35, "NEUTRAL", result["reasoning"])
            conf = min(0.85, abs(result["gap"]) * 8)
            return Signal("predictit", result["pi_price"], conf, result["signal"], result["reasoning"])
        except Exception as e:
            log.debug(f"PredictIt error: {e}")
            return Signal("predictit", market.yes_price, 0.10, "NEUTRAL", "error")

    def _book_depth(self, market: Market) -> Signal:
        """
        Дисбаланс стакана заявок YES-токена из CLOB.
        Большой bid-side → покупатели давят вверх → YES.
        """
        token_id = market.token_id_yes
        if not token_id:
            return Signal("book_depth", market.yes_price, 0.10, "NEUTRAL", "no token_id")
        try:
            book = fetch_order_book(token_id)
            if not book:
                return Signal("book_depth", market.yes_price, 0.10, "NEUTRAL", "no book data")
            bids = book.get("bids", [])
            asks = book.get("asks", [])
            bid_depth = sum(float(b.get("size", 0)) for b in bids[:5])
            ask_depth = sum(float(a.get("size", 0)) for a in asks[:5])
            total = bid_depth + ask_depth
            if total < 100:
                return Signal("book_depth", market.yes_price, 0.10, "NEUTRAL", "insufficient depth")
            imbalance = (bid_depth - ask_depth) / total
            label = f"bid/ask={bid_depth:.0f}/{ask_depth:.0f} imb={imbalance:+.1%}"
            if imbalance > 0.30:
                conf = min(0.70, 0.40 + imbalance * 0.50)
                return Signal("book_depth", market.yes_price, conf, "YES", label)
            if imbalance < -0.30:
                conf = min(0.70, 0.40 + abs(imbalance) * 0.50)
                return Signal("book_depth", market.yes_price, conf, "NO", label)
            return Signal("book_depth", market.yes_price, 0.20, "NEUTRAL", label)
        except Exception as e:
            log.debug(f"Book depth error: {e}")
            return Signal("book_depth", market.yes_price, 0.10, "NEUTRAL", "error")

    def _calibration(self, market: Market) -> Signal:
        """
        Favourite-longshot bias: ставки < 15% переоценены рынком,
        тяжёлые фавориты > 85% — недооценены.
        """
        p = market.yes_price
        if p < 0.12:
            adj = round(p * 0.75, 3)
            return Signal("calibration", adj, 0.50, "NO",  f"longshot overpriced {p:.2f}→{adj:.2f}")
        if p < 0.20:
            adj = round(p * 0.88, 3)
            return Signal("calibration", adj, 0.35, "NO",  f"longshot bias {p:.2f}→{adj:.2f}")
        if p > 0.88:
            adj = round(min(0.97, p * 1.05), 3)
            return Signal("calibration", adj, 0.50, "YES", f"favorite underpriced {p:.2f}→{adj:.2f}")
        if p > 0.80:
            adj = round(min(0.95, p * 1.03), 3)
            return Signal("calibration", adj, 0.35, "YES", f"favorite bias {p:.2f}→{adj:.2f}")
        return Signal("calibration", p, 0.15, "NEUTRAL", "no bias in [0.20, 0.80]")

    def _base_rate(self, market: Market) -> Signal:
        """
        Базовые вероятности по типу вопроса + блокировка перед экономическими релизами.
        """
        # Экономический релиз в течение 2h → не торговать
        release = imminent_release_for(market.question, market.tags, hours=2.0)
        if release:
            return Signal("base_rate", market.yes_price, 0.85, "NEUTRAL",
                          f"pre-{release} blackout")

        q_type = _classify_question(market.question)
        p = market.yes_price
        priors = {
            "electoral":    0.50,
            "economic":     0.45,
            "geopolitical": 0.30,
            "crypto":       0.50,
            "binary":       0.50,
            "general":      0.50,
        }
        prior = priors.get(q_type, 0.50)
        # Лёгкое давление prior: 80% рынок, 20% base rate
        adj = round(0.80 * p + 0.20 * prior, 3)
        diff = adj - p
        if abs(diff) < 0.01:
            return Signal("base_rate", p, 0.20, "NEUTRAL", f"{q_type} prior ≈ market")
        direction = "YES" if diff > 0 else "NO"
        return Signal("base_rate", adj, 0.25, direction,
                      f"{q_type} prior {prior:.0%} → {direction}")

    def _sentiment(self, market: Market) -> Signal:
        """
        Новостной сентимент домена из intel pipeline (последние 4 часа).
        70%+ позитивных новостей по домену → BULLISH → сигнал YES.
        70%+ негативных → BEARISH → сигнал NO.
        """
        if not self.intel:
            return Signal("sentiment", market.yes_price, 0.10, "NEUTRAL", "no intel")
        try:
            from domain_intel import get_domain_sentiment
            domain = next(
                (t for t in market.tags
                 if t in {"crypto", "politics", "macro", "geopolitics", "sports", "tech"}),
                None,
            )
            if not domain:
                return Signal("sentiment", market.yes_price, 0.10, "NEUTRAL", "no domain tag")
            ds = get_domain_sentiment(domain, self.intel.db)
            if not ds or ds.signal == "NEUTRAL" or ds.news_count < 3:
                n = ds.news_count if ds else 0
                return Signal("sentiment", market.yes_price, 0.10, "NEUTRAL",
                              f"{domain} neutral ({n} news)")
            direction = "YES" if ds.signal == "BULLISH" else "NO"
            nudge     = 0.02 * (1 if direction == "YES" else -1)
            adj_score = round(max(0.05, min(0.95, market.yes_price + nudge)), 3)
            return Signal(
                "sentiment", adj_score, ds.confidence, direction,
                f"{domain} {ds.signal.lower()} {ds.bullish_count}↑/{ds.bearish_count}↓ ({ds.news_count}n)",
            )
        except Exception as e:
            log.debug(f"Sentiment signal error: {e}")
            return Signal("sentiment", market.yes_price, 0.10, "NEUTRAL", "error")

    def _ai_guard(self, market: Market) -> Optional[Signal]:
        if cfg.ai_mode == "off" or not self.ai or self._ai_budget_remaining <= 0:
            return None
        snapshot = _build_market_snapshot(market, self.intel)
        prompt = (
            f"{snapshot}\n\n"
            "Decide if this market is TRADEABLE at all.\n"
            "Focus only on: resolution ambiguity, obvious missing context, and whether the current market price looks clearly wrong.\n"
            "Do NOT invent precise certainty.\n"
            'Return ONLY JSON: {"tradable":true,"probability":0.0,"confidence":"low|medium|high","risk_flag":"none|ambiguous_rules|missing_context|news_unclear","reasoning":"max 80 chars"}'
        )
        try:
            resp = self._raw_ai_call(prompt, max_tokens=120, model=_MODEL_FAST)
            data = _parse_ai_json(resp)
            tradable = bool(data.get("tradable", False))
            prob = float(data.get("probability", market.yes_price))
            conf = {"low": 0.30, "medium": 0.55, "high": 0.72}.get(data.get("confidence", "low"), 0.30)
            risk_flag = data.get("risk_flag", "none")
            reasoning = data.get("reasoning", "") or risk_flag
            diff = prob - market.yes_price
            if not tradable or risk_flag in {"ambiguous_rules", "missing_context"}:
                return Signal("ai_guard", market.yes_price, 0.80, "NEUTRAL", f"AI veto: {risk_flag}")
            direction = "YES" if diff > 0.04 else "NO" if diff < -0.04 else "NEUTRAL"
            return Signal("ai_guard", prob, conf, direction, reasoning[:120])
        except Exception as e:
            log.debug(f"AI guard error: {e}")
            return None

    def _should_call_ai(self, result: EnsembleResult, quality: int, signals: list[Signal]) -> bool:
        if cfg.ai_mode == "off" or not self.ai or self._ai_budget_remaining <= 0:
            return False
        if cfg.ai_mode == "full":
            return True
        if quality < cfg.ai_min_quality_for_call:
            return False
        if quality >= cfg.ai_high_quality_skip and self._external_support_count(result.direction, signals) >= 1:
            return False
        return result.direction != "NEUTRAL" or quality >= cfg.ai_min_quality_for_call + 10

    def _trade_quality(self, market: Market, result: EnsembleResult, signals: list[Signal], pre_score: float) -> int:
        score = 0.0
        score += min(30.0, result.edge * 180.0)
        score += min(12.0, max(0.0, pre_score) * 12.0)
        if market.volume_24h >= cfg.min_volume * 2:
            score += 12.0
        elif market.volume_24h >= cfg.min_volume:
            score += 8.0
        elif market.volume_24h >= cfg.min_volume * 0.5:
            score += 3.0
        else:
            score -= 20.0

        if 0.02 <= market.yes_price <= 0.98:
            score += 6.0
        if 0.05 <= market.yes_price <= 0.95:
            score += 4.0
        if hasattr(market, "spread") and market.spread <= 0.03:
            score += 6.0

        ext_support = self._external_support_count(result.direction, signals)
        if ext_support >= 2:
            score += 22.0
        elif ext_support == 1:
            score += 12.0

        momentum = next((s for s in signals if s.name == "momentum" and s.direction == result.direction), None)
        if momentum:
            score += 6.0

        vol = next((s for s in signals if s.name == "volume_spike" and s.confidence >= 0.45), None)
        if vol:
            score += 4.0

        calib = next((s for s in signals if s.name == "calibration"
                      and s.direction == result.direction and s.confidence >= 0.35), None)
        if calib:
            score += 5.0

        book = next((s for s in signals if s.name == "book_depth"
                     and s.direction == result.direction and s.confidence >= 0.40), None)
        if book:
            score += 6.0

        if result.direction == "NEUTRAL":
            score -= 20.0

        return int(max(0, min(100, round(score))))

    def _external_support_count(self, direction: str, signals: list[Signal]) -> int:
        if direction == "NEUTRAL" or not signals:
            return 0
        return sum(
            1 for s in signals
            if s.name in {"consensus", "predictit", "book_depth"}
            and s.direction == direction and s.confidence >= 0.25
        )

    def _strategy_type(self, direction: str, signals: list[Signal], ai_used: bool) -> str:
        if direction != "NEUTRAL" and self._external_support_count(direction, signals) >= 1:
            return "cross_market_gap"
        if ai_used:
            return "ai_assisted"
        return "market_data"

    def _ensemble(self, market: Market, signals: list[Signal]) -> EnsembleResult:
        no_data = {
            "no Kalshi match", "no PredictIt match", "error",
            "insufficient history", "no anomaly",
            "no intel", "no domain tag",  # sentiment neutrals
        }
        # Также фильтруем нейтральный sentiment с маленьким числом новостей
        active = [
            s for s in signals
            if not (
                s.direction == "NEUTRAL" and (
                    s.reasoning in no_data or
                    (s.name == "sentiment" and s.confidence <= 0.10)
                )
            )
        ]
        if not active:
            active = signals

        total_w = sum(max(0.01, s.weight) * max(0.05, s.confidence) for s in active)
        if total_w <= 0:
            return EnsembleResult(0.5, "NEUTRAL", 0.0, 0.5, "low", signals)

        prob = sum(s.score * max(0.01, s.weight) * max(0.05, s.confidence) for s in active) / total_w
        edge = abs(prob - market.yes_price)
        yes_w = sum(s.weight * s.confidence for s in active if s.direction == "YES")
        no_w = sum(s.weight * s.confidence for s in active if s.direction == "NO")

        if prob > market.yes_price + 0.02 and yes_w > no_w:
            direction = "YES"
        elif prob < market.yes_price - 0.02 and no_w > yes_w:
            direction = "NO"
        else:
            direction = "NEUTRAL"

        external_support = self._external_support_count(direction, active)
        non_ai_support = [s for s in active if s.name in _NON_AI_SIGNAL_NAMES and s.direction == direction]
        ai_only = direction != "NEUTRAL" and not non_ai_support
        if ai_only:
            return EnsembleResult(round(prob, 4), "NEUTRAL", round(edge, 4), round(prob, 4), "low", signals, "ai-only setup")

        avg_conf = sum(s.confidence for s in active) / max(len(active), 1)
        vote_margin = abs(yes_w - no_w) / max(total_w, 0.01)
        if edge >= 0.12 and external_support >= 1 and vote_margin > 0.20:
            conf = "high"
        elif edge >= 0.04 and (external_support >= 1 or avg_conf >= 0.35 or vote_margin >= 0.15):
            conf = "medium"
        else:
            conf = "low"

        top3 = sorted(active, key=lambda s: s.confidence * max(0.01, s.weight), reverse=True)[:3]
        reasoning = " · ".join(
            f"[{s.name}] {s.reasoning[:70]}"
            for s in top3
            if s.reasoning and s.reasoning not in no_data
        )

        return EnsembleResult(
            final_score=round(prob, 4),
            direction=direction,
            edge=round(edge, 4),
            ai_probability=round(prob, 4),
            confidence=conf,
            signals=signals,
            reasoning=reasoning,
        )

    def find_correlated_markets(
        self, markets: list[Market], min_overlap: float = 0.30, min_spread: float = 0.12
    ) -> list[tuple[Market, Market, float]]:
        """
        Ищет пары рынков с похожими вопросами но значительной разницей цен.
        Используется для кросс-маркет арбитража и уведомлений.

        Returns список (market1, market2, spread), отсортированный по spread убыванию.
        """
        pairs: list[tuple[Market, Market, float]] = []
        for i, m1 in enumerate(markets):
            words1 = {w.lower() for w in m1.question.split() if len(w) > 4}
            tags1  = set(m1.tags)
            for m2 in markets[i + 1:]:
                # Нужны хотя бы общие теги домена
                if not (tags1 & set(m2.tags)):
                    continue
                words2   = {w.lower() for w in m2.question.split() if len(w) > 4}
                union    = words1 | words2
                if not union:
                    continue
                overlap  = len(words1 & words2) / len(union)
                if overlap < min_overlap:
                    continue
                spread = abs(m1.yes_price - m2.yes_price)
                if spread >= min_spread:
                    pairs.append((m1, m2, round(spread, 3)))
        return sorted(pairs, key=lambda x: x[2], reverse=True)

    def update_weights(self, performance: dict) -> None:
        total = sum(max(0.01, v) for v in performance.values())
        for name, acc in performance.items():
            if name in self.weights:
                self.weights[name] = round(max(0.03, acc / total), 3)

    def _raw_ai_call(self, prompt: str, *, max_tokens: int = 150, model: str = _MODEL_FAST) -> str:
        if not self.ai:
            raise RuntimeError("AI disabled")
        msg = self.ai.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()

    def _kalshi_price(self, question: str) -> Optional[float]:
        try:
            r = _S.get("https://trading-api.kalshi.com/trade-api/v2/markets", timeout=5)
            if r.status_code != 200:
                return None
            data = r.json().get("markets", [])
            q_words = [w for w in question.lower().split() if len(w) > 3][:6]
            best = None
            best_score = 0
            for m in data[:500]:
                title = (m.get("title") or "").lower()
                score = sum(1 for w in q_words if w in title)
                if score > best_score:
                    best_score = score
                    best = m
            if not best or best_score < 2:
                return None
            yes_bid = best.get("yes_bid")
            yes_ask = best.get("yes_ask")
            if yes_bid is None and yes_ask is None:
                return None
            vals = [v / 100 for v in [yes_bid, yes_ask] if isinstance(v, (int, float))]
            if not vals:
                return None
            return sum(vals) / len(vals)
        except Exception as e:
            log.debug(f"Kalshi fetch error: {e}")
            return None

    def _manifold_price(self, question: str) -> Optional[float]:
        """
        Цена рынка с Manifold Markets (play-money, но отражает агрегированное мнение).
        API: GET https://api.manifold.markets/v0/search-markets?term=...&limit=5
        Использовать только как дополнительный голос, не как основной арбитраж.
        """
        try:
            q_words = [w for w in question.lower().split() if len(w) > 3][:5]
            term    = " ".join(q_words[:4])
            r = _S.get(
                "https://api.manifold.markets/v0/search-markets",
                params={"term": term, "limit": 8, "sort": "liquidity"},
                timeout=5,
            )
            if r.status_code != 200:
                return None
            markets = r.json()
            if not isinstance(markets, list) or not markets:
                return None
            # Fuzzy match: ищем рынок с максимальным совпадением слов
            best_score = 0
            best_prob  = None
            for m in markets:
                if m.get("outcomeType") != "BINARY":
                    continue
                title = (m.get("question") or "").lower()
                score = sum(1 for w in q_words if w in title)
                prob  = m.get("probability")
                if score > best_score and prob is not None:
                    best_score = score
                    best_prob  = float(prob)
            if best_score < 2 or best_prob is None:
                return None
            return round(best_prob, 4)
        except Exception as e:
            log.debug(f"Manifold fetch error: {e}")
            return None
