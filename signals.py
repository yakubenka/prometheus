"""
Prometheus — Signal Engine v4.0
6 независимых сигналов → ensemble → итоговое решение.

УЛУЧШЕНИЯ v4.0:
1. Chain-of-Thought промпты — Claude рассуждает пошагово, не угадывает
2. description рынка включён в контекст — правила резолюции критически важны
3. Параллельные AI запросы — все 3 вызова одновременно через ThreadPoolExecutor
4. Adaptive min_edge по домену — крипто требует 10%, политика 4%
5. Position sizing по confidence — плавный, не бинарный
6. Volume anomaly detection — hourly spike как отдельный сигнал
7. Question type tagging — electoral/economic/geopolitical/binary для точности
"""
from __future__ import annotations
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, TYPE_CHECKING
import requests
from anthropic import Anthropic
from data import Market, fetch_market_price_history

if TYPE_CHECKING:
    from intel import IntelPipeline

log = logging.getLogger("prometheus.signals")

_S = requests.Session()
_S.headers["User-Agent"] = "Prometheus/3.0"

_MODEL_FAST  = "claude-haiku-4-5-20251001"
_MODEL_SMART = "claude-sonnet-4-20250514"

# Adaptive min_edge по типу рынка
_DOMAIN_MIN_EDGE = {
    "crypto":      0.10,   # волатильны, нужен большой edge
    "sports":      0.08,   # непредсказуемы
    "macro":       0.06,   # умеренно
    "politics":    0.04,   # стабильны, меньший edge OK
    "geopolitics": 0.05,
    "tech":        0.06,
    "other":       0.05,
}

# Confidence → Kelly multiplier (плавный)
_CONF_KELLY = {
    "high":   1.00,
    "medium": 0.70,
    "low":    0.40,
}


def get_domain_min_edge(tags: list, default: float = 0.05) -> float:
    """Минимальный edge для данного набора тегов."""
    for tag in (tags or []):
        t = str(tag).lower()
        if t in _DOMAIN_MIN_EDGE:
            return _DOMAIN_MIN_EDGE[t]
    return default


def get_confidence_kelly_mult(confidence: str) -> float:
    """Плавный multiplier размера позиции по confidence."""
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
    question_type:  str = "general"   # electoral/economic/geopolitical/binary


def _parse_ai_json(text: str) -> dict:
    clean = text.strip().replace("```json", "").replace("```", "").strip()
    return json.loads(clean)


def _classify_question(question: str) -> str:
    """
    Определить тип вопроса для точного трекинга точности.
    electoral / economic / geopolitical / celebrity / binary / general
    """
    q = question.lower()
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


def _build_rich_context(market: Market, intel=None) -> str:
    """
    Строим максимально богатый контекст для AI.
    НОВОЕ: включаем description рынка (правила резолюции критически важны).
    НОВОЕ: volume anomaly detection (hourly vs daily).
    """
    parts = []

    # 1. Intel — новости и данные
    if intel:
        try:
            # Ищем по вопросу И по description для лучшего покрытия
            search_text = market.question
            if market.description:
                search_text += " " + market.description[:200]
            news = intel.context_for(search_text, hours=24)
            if news and "No recent" not in news:
                parts.append(f"Recent intelligence:\n{news}")
        except Exception:
            pass

    # 2. История цены + volume anomaly
    try:
        history = fetch_market_price_history(market.id, days=7)
        prices  = [float(p["p"]) for p in history if p.get("p")]
        if len(prices) >= 4:
            cur   = prices[-1]
            p24   = prices[max(0, len(prices) - 24)]
            p7d   = prices[0]
            high7 = max(prices)
            low7  = min(prices)
            m24   = cur - p24
            m7d   = cur - p7d
            price_str = (
                f"Price history:\n"
                f"  Current: {cur:.3f} | 24h: {m24:+.3f} | 7d: {m7d:+.3f}\n"
                f"  7d range: {low7:.3f} - {high7:.3f}"
            )
            parts.append(price_str)
    except Exception:
        pass

    # 3. Время до закрытия
    days_left = "unknown"
    if market.end_date:
        try:
            end = datetime.fromisoformat(market.end_date.replace("Z", "+00:00"))
            dl  = (end - datetime.now(timezone.utc)).days
            days_left = f"{dl} days"
        except Exception:
            pass

    # 4. Основная информация + description (НОВОЕ)
    market_info = (
        f"Market: {market.question}\n"
        f"Current price: {market.yes_price:.3f} ({market.yes_price:.1%})\n"
        f"Closes in: {days_left} | Volume 24h: ${market.volume_24h:,.0f}"
    )
    if market.description and len(market.description) > 50:
        market_info += f"\nResolution rules: {market.description[:400]}"
    parts.append(market_info)

    return "\n\n".join(parts)


def _detect_volume_anomaly(market: Market) -> Optional[Signal]:
    """
    НОВОЕ: резкий рост объёма (5x за час) = кто-то что-то знает.
    Это самый сильный сигнал — сильнее любого AI.
    """
    try:
        # Получаем часовую историю объёма
        r = _S.get(
            "https://gamma-api.polymarket.com/markets",
            params={"id": market.id},
            timeout=5,
        )
        if r.status_code != 200:
            return None

        data = r.json()
        items = data if isinstance(data, list) else data.get("data", [data])
        if not items:
            return None

        m = items[0] if isinstance(items, list) else items
        vol_1h  = float(m.get("volume1hr", 0) or 0)
        vol_24h = float(m.get("volume24hr", 0) or market.volume_24h or 0)

        if vol_24h <= 0 or vol_1h <= 0:
            return None

        # Ожидаемый объём за час = vol_24h / 24
        expected_1h = vol_24h / 24
        if expected_1h <= 0:
            return None

        spike = vol_1h / expected_1h

        if spike >= 8.0:
            # Экстремальный spike — очень сильный сигнал
            return Signal(
                "volume_spike", market.yes_price, 0.80,
                "NEUTRAL",  # не знаем направление, но что-то происходит
                f"Volume spike {spike:.1f}x normal (${vol_1h:,.0f}/h vs avg ${expected_1h:,.0f}/h)"
            )
        elif spike >= 4.0:
            return Signal(
                "volume_spike", market.yes_price, 0.50,
                "NEUTRAL",
                f"Volume elevated {spike:.1f}x (${vol_1h:,.0f}/h)"
            )
        return None
    except Exception as e:
        log.debug(f"Volume anomaly: {e}")
        return None


class SignalEngine:
    DEFAULT_WEIGHTS = {
        "sentiment":     0.26,
        "momentum":      0.15,
        "calibration":   0.22,
        "consensus":     0.14,
        "predictit":     0.10,
        "base_rate":     0.08,
        "volume_spike":  0.05,
    }

    def __init__(self, ai: Anthropic,
                 intel: Optional["IntelPipeline"] = None) -> None:
        self.ai      = ai
        self.intel   = intel
        self.weights = dict(self.DEFAULT_WEIGHTS)

    def analyze(self, market: Market) -> EnsembleResult:
        ctx           = _build_rich_context(market, self.intel)
        question_type = _classify_question(market.question)

        # Параллельные AI запросы через ThreadPoolExecutor
        # consensus и predictit — HTTP запросы (не AI), тоже параллельно
        signals = self._run_parallel(market, ctx)

        # Volume anomaly — быстро, без AI
        vol_sig = _detect_volume_anomaly(market)
        if vol_sig:
            vol_sig.weight = self.weights.get("volume_spike", 0.05)
            signals.append(vol_sig)
            log.info(f"  📊 Volume anomaly: {vol_sig.reasoning}")

        for s in signals:
            if s.weight == 0.0:
                s.weight = self.weights.get(s.name, 0.05)

        result = self._ensemble(market, signals)
        result.question_type = question_type
        return result

    def _run_parallel(self, market: Market, ctx: str) -> list[Signal]:
        """
        НОВОЕ: запускаем все сигналы параллельно.
        AI вызовы (sentiment, calibration, base_rate) + HTTP (consensus, predictit) — всё одновременно.
        Ускорение: с ~15 сек до ~5 сек на рынок.
        """
        # Small jitter on AI calls to avoid simultaneous rate limit hits
        def _with_jitter(fn, delay=0.0):
            def wrapper():
                if delay > 0:
                    time.sleep(delay)
                return fn()
            return wrapper

        tasks = {
            "sentiment":   _with_jitter(lambda: self._sentiment(market, ctx), 0.0),
            "momentum":    lambda: self._momentum(market),   # no AI, no jitter
            "calibration": _with_jitter(lambda: self._calibration(market, ctx), 0.3),
            "consensus":   lambda: self._consensus(market),  # HTTP only
            "predictit":   lambda: self._predictit(market),  # HTTP only
            "base_rate":   _with_jitter(lambda: self._base_rate(market, ctx), 0.6),
        }

        results: dict[str, Signal] = {}

        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {executor.submit(fn): name for name, fn in tasks.items()}
            try:
                for future in as_completed(futures, timeout=45):
                    name = futures[future]
                    try:
                        results[name] = future.result()
                    except Exception as e:
                        log.debug(f"Signal {name} failed: {e}")
                        results[name] = Signal(name, market.yes_price, 0.10,
                                               "NEUTRAL", "error")
            except Exception as timeout_err:
                # Некоторые futures не завершились вовремя — используем что есть
                log.debug(f"Signals timeout: {timeout_err}")
                for future, name in futures.items():
                    if name not in results:
                        if future.done():
                            try:
                                results[name] = future.result()
                            except Exception:
                                pass
                        if name not in results:
                            results[name] = Signal(name, market.yes_price, 0.10,
                                                   "NEUTRAL", "error")

        # Возвращаем в фиксированном порядке
        return [results.get(name, Signal(name, market.yes_price, 0.10, "NEUTRAL", "error"))
                for name in tasks]

    def _sentiment(self, market: Market, ctx: str) -> Signal:
        """
        НОВОЕ: Chain-of-Thought промпт.
        Claude рассуждает пошагово: факты → аргументы за → против → итог.
        """
        prompt = (
            f"{ctx}\n\n"
            f"TASK: Estimate the TRUE probability this market resolves YES.\n\n"
            f"Think step by step:\n"
            f"1. What are the KEY FACTS from the context above?\n"
            f"2. What are the strongest arguments FOR YES?\n"
            f"3. What are the strongest arguments AGAINST YES (for NO)?\n"
            f"4. What is your final probability estimate?\n\n"
            f"Be precise. Consider the resolution rules carefully.\n"
            f"The market price is {market.yes_price:.1%} — your job is to find if it's WRONG.\n\n"
            f'Return ONLY JSON: {{"probability":0.0,"confidence":"low|medium|high","reasoning":"one sentence max 100 chars"}}'
        )
        return self._ai_call("sentiment", prompt, market.yes_price, model=_MODEL_SMART,
                              max_tokens=250)

    def _momentum(self, market: Market) -> Signal:
        try:
            history = fetch_market_price_history(market.id, days=3)
            prices  = [float(p["p"]) for p in history if p.get("p")]
            if len(prices) < 6:
                return Signal("momentum", market.yes_price, 0.2, "NEUTRAL", "insufficient history")
            cur = prices[-1]
            d24 = prices[max(0, len(prices) - 24)]
            d72 = prices[0]
            m24 = cur - d24
            m72 = cur - d72

            # Добавляем acceleration: ускоряется ли движение?
            d12  = prices[max(0, len(prices) - 12)]
            m12  = cur - d12
            accel = m12 - (m24 / 2)  # положительный = ускорение

            if m24 > 0.07 and m72 > 0.10:
                conf = 0.85 if accel > 0 else 0.75
                return Signal("momentum", min(0.90, cur + m24*0.3), conf,
                              "YES", f"Strong bullish +{m24:.1%}/24h +{m72:.1%}/3d{'↑' if accel>0 else ''}")
            if m24 > 0.04 and m72 > 0.05:
                return Signal("momentum", min(0.82, cur + m24*0.2), 0.60,
                              "YES", f"Moderate bullish +{m24:.1%}/24h")
            if m24 < -0.07 and m72 < -0.10:
                conf = 0.85 if accel < 0 else 0.75
                return Signal("momentum", max(0.10, cur + m24*0.3), conf,
                              "NO", f"Strong bearish {m24:.1%}/24h {m72:.1%}/3d{'↓' if accel<0 else ''}")
            if m24 < -0.04 and m72 < -0.05:
                return Signal("momentum", max(0.18, cur + m24*0.2), 0.60,
                              "NO", f"Moderate bearish {m24:.1%}/24h")
            return Signal("momentum", cur, 0.25, "NEUTRAL",
                          f"No clear trend (24h: {m24:+.1%})")
        except Exception as e:
            log.debug(f"Momentum: {e}")
            return Signal("momentum", market.yes_price, 0.15, "NEUTRAL", "error")

    def _calibration(self, market: Market, ctx: str) -> Signal:
        """
        НОВОЕ: Chain-of-Thought для calibration.
        Явно просим привести аналогичные исторические случаи.
        """
        prompt = (
            f"{ctx}\n\n"
            f"TASK: What is the TRUE probability based on HISTORICAL BASE RATES?\n\n"
            f"Think step by step:\n"
            f"1. What TYPE of event is this? (election/economic/geopolitical/other)\n"
            f"2. What are 2-3 COMPARABLE historical events and their outcomes?\n"
            f"3. Based on base rates from history, what probability is justified?\n"
            f"IMPORTANT: Completely ignore the current market price of {market.yes_price:.1%}.\n\n"
            f'Return ONLY JSON: {{"base_rate":0.0,"confidence":"low|medium|high","reasoning":"one sentence max 100 chars"}}'
        )
        try:
            resp  = self._raw_ai_call(prompt, max_tokens=200, model=_MODEL_SMART)
            data  = _parse_ai_json(resp)
            base  = float(data["base_rate"])
            conf  = {"low": 0.3, "medium": 0.6, "high": 0.85}.get(
                data.get("confidence", "medium"), 0.5)
            diff  = base - market.yes_price
            direc = "YES" if diff > 0.04 else "NO" if diff < -0.04 else "NEUTRAL"
            return Signal("calibration", base, conf, direc, data.get("reasoning", ""))
        except Exception as e:
            log.debug(f"Calibration: {e}")
            return Signal("calibration", market.yes_price, 0.2, "NEUTRAL", "error")

    def _consensus(self, market: Market) -> Signal:
        kalshi = self._kalshi_price(market.question)
        if kalshi is None:
            return Signal("consensus", market.yes_price, 0.10, "NEUTRAL", "no Kalshi match")
        spread = market.yes_price - kalshi
        if abs(spread) < 0.02:
            return Signal("consensus", (market.yes_price + kalshi) / 2, 0.65,
                          "NEUTRAL", f"PM {market.yes_price:.2%} ≈ Kalshi {kalshi:.2%}")
        if spread > 0:
            return Signal("consensus", kalshi, min(0.88, abs(spread)*8), "NO",
                          f"PM {market.yes_price:.2%} > Kalshi {kalshi:.2%} overpriced by {spread:.1%}")
        return Signal("consensus", kalshi, min(0.88, abs(spread)*8), "YES",
                      f"Kalshi {kalshi:.2%} > PM {market.yes_price:.2%} underpriced by {abs(spread):.1%}")

    def _predictit(self, market: Market) -> Signal:
        try:
            from predictit import arbitrage_signal
            result = arbitrage_signal(market.question, market.yes_price)
            if result is None:
                return Signal("predictit", market.yes_price, 0.10, "NEUTRAL", "no PredictIt match")
            if result["signal"] == "NEUTRAL":
                return Signal("predictit", result["pi_price"], 0.45, "NEUTRAL", result["reasoning"])
            conf = min(0.85, abs(result["gap"]) * 8)
            return Signal("predictit", result["pi_price"], conf, result["signal"], result["reasoning"])
        except Exception as e:
            log.debug(f"PredictIt: {e}")
            return Signal("predictit", market.yes_price, 0.10, "NEUTRAL", "error")

    def _base_rate(self, market: Market, ctx: str) -> Signal:
        """
        НОВОЕ: используем description для правил резолюции.
        """
        prompt = (
            f"{ctx}\n\n"
            f"TASK: What is the probability from FIRST PRINCIPLES?\n"
            f"Consider: what would a careful, unbiased analyst estimate BEFORE seeing market prices?\n"
            f"Focus on: resolution criteria, current state of affairs, likely trajectory.\n"
            f"IGNORE the current market price of {market.yes_price:.1%} completely.\n\n"
            f'Return ONLY JSON: {{"probability":0.0,"reasoning":"one sentence max 100 chars"}}'
        )
        try:
            resp  = self._raw_ai_call(prompt, max_tokens=150, model=_MODEL_FAST)
            data  = _parse_ai_json(resp)
            prob  = float(data["probability"])
            diff  = prob - market.yes_price
            direc = "YES" if diff > 0.04 else "NO" if diff < -0.04 else "NEUTRAL"
            return Signal("base_rate", prob, 0.50, direc, data.get("reasoning", ""))
        except Exception as e:
            log.debug(f"Base rate: {e}")
            return Signal("base_rate", 0.5, 0.10, "NEUTRAL", "error")

    def find_correlated_markets(self, markets: list) -> list[tuple]:
        """
        НОВОЕ: Cross-market correlation.
        Ищем пары рынков которые должны двигаться вместе.
        Если одно отстаёт от другого — арбитражная возможность.
        
        Примеры пар:
        - "Will Trump win?" + "Will Republicans take Senate?"
        - "Will Fed cut rates?" + "Will Bitcoin exceed $100k?"
        - "Will Ukraine ceasefire?" + "Will Russia withdraw troops?"
        """
        CORR_PAIRS = [
            # Политика США
            (["trump", "republican", "gop"], ["senate", "house", "congress"]),
            # Макро
            (["fed", "rate cut", "fomc"], ["bitcoin", "btc", "s&p", "nasdaq"]),
            (["recession", "gdp"], ["unemployment", "jobs"]),
            # Геополитика
            (["ceasefire", "peace"], ["troops", "withdrawal", "war end"]),
            (["iran", "nuclear"], ["oil", "crude", "sanctions"]),
        ]

        pairs = []
        for m1 in markets:
            for m2 in markets:
                if m1.id == m2.id:
                    continue
                q1 = m1.question.lower()
                q2 = m2.question.lower()
                for group1_kws, group2_kws in CORR_PAIRS:
                    in_g1_m1 = any(kw in q1 for kw in group1_kws)
                    in_g2_m2 = any(kw in q2 for kw in group2_kws)
                    in_g2_m1 = any(kw in q1 for kw in group2_kws)
                    in_g1_m2 = any(kw in q2 for kw in group1_kws)
                    if (in_g1_m1 and in_g2_m2) or (in_g2_m1 and in_g1_m2):
                        spread = abs(m1.yes_price - m2.yes_price)
                        if spread > 0.10:  # значимое расхождение
                            pairs.append((m1, m2, spread))
        return pairs

    def _ensemble(self, market: Market, signals: list[Signal]) -> EnsembleResult:
        NO_DATA = {"no Kalshi match", "no PredictIt match", "error", "insufficient history"}
        active  = [s for s in signals
                   if not (s.direction == "NEUTRAL" and s.reasoning in NO_DATA)]
        if not active:
            active = signals

        # Volume spike — если есть, повышаем уверенность доминирующего направления
        vol_signal = next((s for s in active if s.name == "volume_spike"), None)
        vol_boost  = 0.10 if vol_signal and vol_signal.confidence > 0.5 else 0.0

        total_w = sum(s.weight * s.confidence for s in active)
        if total_w == 0:
            return EnsembleResult(0.5, "NEUTRAL", 0, 0.5, "low", signals)

        prob = sum(s.score * s.weight * s.confidence for s in active) / total_w
        edge = abs(prob - market.yes_price)

        yes_weighted = sum(s.weight * s.confidence for s in active if s.direction == "YES")
        no_weighted  = sum(s.weight * s.confidence for s in active if s.direction == "NO")
        vote_margin  = abs(yes_weighted - no_weighted) / max(total_w, 0.01)

        if prob > market.yes_price + 0.02 and yes_weighted > no_weighted:
            direction = "YES"
        elif prob < market.yes_price - 0.02 and no_weighted > yes_weighted:
            direction = "NO"
        else:
            direction = "NEUTRAL"

        yes_v     = sum(1 for s in active if s.direction == "YES")
        no_v      = sum(1 for s in active if s.direction == "NO")
        agreement = max(yes_v, no_v) / len(active)
        avg_conf  = sum(s.confidence for s in active) / len(active)

        # Confidence с volume boost
        if edge >= 0.20 and direction != "NEUTRAL":
            conf = "high"
        elif avg_conf > 0.50 and agreement >= 0.55 and edge > 0.05 and vote_margin > 0.2:
            conf = "high"
        elif avg_conf > 0.28 and agreement >= 0.35 and edge > 0.02:
            conf = "medium"
        else:
            conf = "low"

        # Volume spike повышает уверенность на одну ступень если подтверждает
        if vol_boost > 0 and conf == "medium":
            conf = "high"
        elif vol_boost > 0 and conf == "low":
            conf = "medium"

        top3 = sorted(active, key=lambda s: s.confidence * s.weight, reverse=True)[:3]
        reasoning = " · ".join(
            f"[{s.name}] {s.reasoning[:80]}"
            for s in top3
            if s.reasoning and s.reasoning not in NO_DATA
        )

        return EnsembleResult(
            final_score    = round(prob, 4),
            direction      = direction,
            edge           = round(edge, 4),
            ai_probability = round(prob, 4),
            confidence     = conf,
            signals        = signals,
            reasoning      = reasoning,
        )

    def update_weights(self, performance: dict) -> None:
        total = sum(max(0.01, v) for v in performance.values())
        for name, acc in performance.items():
            if name in self.weights:
                self.weights[name] = round(max(0.05, acc / total), 3)
        log.info(f"Веса обновлены: {self.weights}")

    def _raw_ai_call(self, prompt: str, max_tokens: int = 150,
                     model: str = _MODEL_SMART, retries: int = 2) -> str:
        for attempt in range(retries + 1):
            try:
                resp = self.ai.messages.create(
                    model=model, max_tokens=max_tokens,
                    messages=[{"role": "user", "content": prompt}],
                )
                return resp.content[0].text
            except Exception as e:
                err_str = str(e).lower()
                if "rate_limit" in err_str or "overload" in err_str:
                    wait = 2 ** attempt
                    log.warning(f"Rate limit, waiting {wait}s")
                    time.sleep(wait)
                    continue
                raise
        raise RuntimeError("AI call failed after retries")

    def _ai_call(self, name: str, prompt: str, fallback: float,
                 model: str = _MODEL_SMART, max_tokens: int = 150) -> Signal:
        try:
            text  = self._raw_ai_call(prompt, max_tokens=max_tokens, model=model)
            data  = _parse_ai_json(text)
            prob  = float(data.get("probability", data.get("base_rate", 0.5)))
            conf  = {"low": 0.30, "medium": 0.60, "high": 0.85}.get(
                data.get("confidence", "medium"), 0.5)
            diff  = prob - fallback
            direc = "YES" if diff > 0.03 else "NO" if diff < -0.03 else "NEUTRAL"
            return Signal(name, prob, conf, direc, data.get("reasoning", ""))
        except Exception as e:
            log.debug(f"{name} AI error: {e}")
            return Signal(name, 0.5, 0.10, "NEUTRAL", "error")

    def _kalshi_price(self, question: str) -> Optional[float]:
        try:
            words = [w for w in question.lower().split() if len(w) > 3][:4]
            r = _S.get(
                "https://trading-api.kalshi.com/trade-api/v2/markets",
                params={"status": "open", "limit": 20, "search": " ".join(words)},
                timeout=5,
            )
            if r.status_code != 200:
                return None
            for m in r.json().get("markets", []):
                title = m.get("title", "").lower()
                if sum(1 for w in words if w in title) >= 2:
                    p = m.get("yes_ask") or m.get("last_price")
                    if p:
                        return float(p) / 100
        except Exception:
            pass
        return None
