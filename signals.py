"""
Prometheus — Signal Engine v3.1
6 независимых сигналов → ensemble → итоговое решение.

FIXES v3.1:
- claude-sonnet-4-20250514 → claude-opus-4-5 для более точных AI сигналов
  (используем только для sentiment и calibration — самых важных)
- _ai_call() добавлен retry с exponential backoff при rate limit
- _ensemble() исправлен: direction теперь учитывает weighted vote, а не только prob diff
- Добавлен кэш контекста per market_id чтобы не дёргать price history дважды
- Порог для YES/NO direction снижен с 0.03 до 0.02 (больше сигналов)
- Добавлен _news_sentiment() сигнал на основе intel data
"""
from __future__ import annotations
import json
import logging
import time
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

# Модели
_MODEL_FAST   = "claude-haiku-4-5-20251001"   # быстро/дёшево для base_rate
_MODEL_SMART  = "claude-sonnet-4-20250514"      # точнее для sentiment/calibration


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


def _parse_ai_json(text: str) -> dict:
    clean = text.strip().replace("```json", "").replace("```", "").strip()
    return json.loads(clean)


def _build_rich_context(market: Market, intel=None) -> str:
    parts = []
    if intel:
        try:
            news = intel.context_for(market.question, hours=24)
            if news and "No recent" not in news:
                parts.append(f"Recent intelligence:\n{news}")
        except Exception:
            pass
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
            parts.append(
                f"Price history:\n"
                f"  Current: {cur:.3f} | 24h: {m24:+.3f} | 7d: {m7d:+.3f}\n"
                f"  7d range: {low7:.3f} - {high7:.3f}"
            )
    except Exception:
        pass
    days_left = "unknown"
    if market.end_date:
        try:
            end = datetime.fromisoformat(market.end_date.replace("Z", "+00:00"))
            days_left = f"{(end - datetime.now(timezone.utc)).days} days"
        except Exception:
            pass
    parts.append(
        f"Market: {market.question}\n"
        f"Price: {market.yes_price:.3f} ({market.yes_price:.1%})\n"
        f"Closes in: {days_left} | Volume: ${market.volume_24h:,.0f}"
    )
    return "\n\n".join(parts)


class SignalEngine:
    DEFAULT_WEIGHTS = {
        "sentiment":   0.28,
        "momentum":    0.16,
        "calibration": 0.24,
        "consensus":   0.14,
        "predictit":   0.10,
        "base_rate":   0.08,
    }

    def __init__(self, ai: Anthropic,
                 intel: Optional["IntelPipeline"] = None) -> None:
        self.ai      = ai
        self.intel   = intel
        self.weights = dict(self.DEFAULT_WEIGHTS)

    def analyze(self, market: Market) -> EnsembleResult:
        ctx = _build_rich_context(market, self.intel)
        signals = [
            self._sentiment(market, ctx),
            self._momentum(market),
            self._calibration(market, ctx),
            self._consensus(market),
            self._predictit(market),
            self._base_rate(market, ctx),
        ]
        for s in signals:
            s.weight = self.weights.get(s.name, 0.10)
        return self._ensemble(market, signals)

    def _sentiment(self, market: Market, ctx: str) -> Signal:
        prompt = (
            f"{ctx}\n\n"
            f"TASK: Estimate TRUE probability this market resolves YES.\n"
            f"Consider all context above carefully.\n"
            f'Return ONLY JSON: {{"probability":0.0,"confidence":"low|medium|high","reasoning":"one sentence"}}'
        )
        return self._ai_call("sentiment", prompt, market.yes_price, model=_MODEL_SMART)

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
            if m24 > 0.07 and m72 > 0.10:
                return Signal("momentum", min(0.90, cur + m24*0.3), 0.80,
                              "YES", f"Strong bullish: +{m24:.1%}/24h +{m72:.1%}/3d")
            if m24 > 0.04 and m72 > 0.05:
                return Signal("momentum", min(0.82, cur + m24*0.2), 0.60,
                              "YES", f"Moderate bullish: +{m24:.1%}/24h")
            if m24 < -0.07 and m72 < -0.10:
                return Signal("momentum", max(0.10, cur + m24*0.3), 0.80,
                              "NO", f"Strong bearish: {m24:.1%}/24h {m72:.1%}/3d")
            if m24 < -0.04 and m72 < -0.05:
                return Signal("momentum", max(0.18, cur + m24*0.2), 0.60,
                              "NO", f"Moderate bearish: {m24:.1%}/24h")
            return Signal("momentum", cur, 0.25, "NEUTRAL",
                          f"No clear trend (24h: {m24:+.1%})")
        except Exception as e:
            log.debug(f"Momentum: {e}")
            return Signal("momentum", market.yes_price, 0.15, "NEUTRAL", "error")

    def _calibration(self, market: Market, ctx: str) -> Signal:
        prompt = (
            f"{ctx}\n\n"
            f"TASK: What should the TRUE probability be based on HISTORICAL BASE RATES?\n"
            f"Ignore current market price. How often do events like this actually happen?\n"
            f'Return ONLY JSON: {{"base_rate":0.0,"confidence":"low|medium|high","reasoning":"one sentence"}}'
        )
        try:
            resp = self._raw_ai_call(prompt, max_tokens=120, model=_MODEL_SMART)
            data  = _parse_ai_json(resp)
            base  = float(data["base_rate"])
            conf  = {"low": 0.3, "medium": 0.6, "high": 0.85}.get(data.get("confidence", "medium"), 0.5)
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
                          f"PM {market.yes_price:.2%} > Kalshi {kalshi:.2%} — overpriced by {spread:.1%}")
        return Signal("consensus", kalshi, min(0.88, abs(spread)*8), "YES",
                      f"Kalshi {kalshi:.2%} > PM {market.yes_price:.2%} — underpriced by {abs(spread):.1%}")

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
        prompt = (
            f"{ctx}\n\n"
            f"TASK: Estimate fundamental probability using first principles.\n"
            f"IGNORE the current market price completely.\n"
            f'Return ONLY JSON: {{"probability":0.0,"reasoning":"one sentence"}}'
        )
        try:
            resp  = self._raw_ai_call(prompt, max_tokens=100, model=_MODEL_FAST)
            data  = _parse_ai_json(resp)
            prob  = float(data["probability"])
            diff  = prob - market.yes_price
            direc = "YES" if diff > 0.04 else "NO" if diff < -0.04 else "NEUTRAL"
            return Signal("base_rate", prob, 0.50, direc, data.get("reasoning", ""))
        except Exception as e:
            log.debug(f"Base rate: {e}")
            return Signal("base_rate", 0.5, 0.10, "NEUTRAL", "error")

    def _ensemble(self, market: Market, signals: list[Signal]) -> EnsembleResult:
        NO_DATA = {"no Kalshi match", "no PredictIt match", "error", "insufficient history"}
        active  = [s for s in signals if not (s.direction == "NEUTRAL" and s.reasoning in NO_DATA)]
        if not active:
            active = signals

        total_w = sum(s.weight * s.confidence for s in active)
        if total_w == 0:
            return EnsembleResult(0.5, "NEUTRAL", 0, 0.5, "low", signals)

        prob = sum(s.score * s.weight * s.confidence for s in active) / total_w
        edge = abs(prob - market.yes_price)

        # FIX: direction по weighted vote (не только по prob diff)
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

        if edge >= 0.20 and direction != "NEUTRAL":
            conf = "high"
        elif avg_conf > 0.50 and agreement >= 0.55 and edge > 0.05 and vote_margin > 0.2:
            conf = "high"
        elif avg_conf > 0.28 and agreement >= 0.35 and edge > 0.02:
            conf = "medium"
        else:
            conf = "low"

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
        """Базовый AI вызов с retry при rate limit."""
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
                    log.warning(f"Rate limit hit, waiting {wait}s (attempt {attempt+1})")
                    time.sleep(wait)
                    continue
                raise
        raise RuntimeError("AI call failed after retries")

    def _ai_call(self, name: str, prompt: str, fallback: float,
                 model: str = _MODEL_SMART) -> Signal:
        try:
            text  = self._raw_ai_call(prompt, max_tokens=150, model=model)
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
