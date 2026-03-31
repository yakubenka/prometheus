"""
Prometheus — Signal Engine
5 независимых сигналов → ensemble → итоговое решение.
"""
from __future__ import annotations
import json
import logging
from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING
import requests
from anthropic import Anthropic
from data import Market, fetch_market_price_history

if TYPE_CHECKING:
    from intel import IntelPipeline

log = logging.getLogger("prometheus.signals")

_SESSION = requests.Session()
_SESSION.headers["User-Agent"] = "Prometheus/3.0"


@dataclass
class Signal:
    name:       str
    score:      float    # 0–1 (0.5 = нейтральный)
    confidence: float    # 0–1
    direction:  str      # YES / NO / NEUTRAL
    reasoning:  str
    weight:     float = 0.0


@dataclass
class EnsembleResult:
    final_score:    float
    direction:      str
    edge:           float
    ai_probability: float
    confidence:     str    # low / medium / high
    signals:        list[Signal] = field(default_factory=list)
    reasoning:      str = ""


def _parse_ai_json(text: str) -> dict:
    """Безопасный парсинг JSON из ответа AI."""
    clean = text.strip().replace("```json","").replace("```","").strip()
    return json.loads(clean)


class SignalEngine:
    DEFAULT_WEIGHTS = {
        "sentiment":   0.25,
        "momentum":    0.20,
        "calibration": 0.25,
        "consensus":   0.20,
        "base_rate":   0.10,
    }

    def __init__(self, ai: Anthropic,
                 intel: Optional["IntelPipeline"] = None) -> None:
        self.ai      = ai
        self.intel   = intel
        self.weights = dict(self.DEFAULT_WEIGHTS)

    def analyze(self, market: Market) -> EnsembleResult:
        signals = [
            self._sentiment(market),
            self._momentum(market),
            self._calibration(market),
            self._consensus(market),
            self._base_rate(market),
        ]
        for s in signals:
            s.weight = self.weights.get(s.name, 0.2)
        return self._ensemble(market, signals)

    # ── Signal 1: Sentiment ────────────────────────────────────────────────────

    def _sentiment(self, market: Market) -> Signal:
        context = (self.intel.context_for(market.question, hours=12)
                   if self.intel else "No intelligence data.")
        prompt = (
            f"Market: {market.question}\n"
            f"Current YES price: {market.yes_price:.2%}\n"
            f"Closes: {market.end_date}\n\n"
            f"Recent intelligence:\n{context}\n\n"
            f"Based on this intelligence, estimate TRUE probability of YES.\n"
            f'Return ONLY JSON: {{"probability":0.0-1.0,"confidence":"low|medium|high","reasoning":"one sentence"}}'
        )
        return self._ai_call("sentiment", prompt, market.yes_price)

    # ── Signal 2: Momentum ─────────────────────────────────────────────────────

    def _momentum(self, market: Market) -> Signal:
        history = fetch_market_price_history(market.id, days=3)
        prices  = [float(p["p"]) for p in history if p.get("p")]
        if len(prices) < 6:
            return Signal("momentum", market.yes_price, 0.2, "NEUTRAL", "insufficient history")

        cur     = prices[-1]
        d24     = prices[max(0, len(prices) - 24)]
        d72     = prices[0]
        move24  = cur - d24
        move72  = cur - d72

        if move24 > 0.05 and move72 > 0.07:
            return Signal("momentum", min(0.88, cur + move24 * 0.4), 0.70,
                          "YES", f"+{move24:.1%}/24h +{move72:.1%}/72h — bullish")
        if move24 < -0.05 and move72 < -0.07:
            return Signal("momentum", max(0.12, cur + move24 * 0.4), 0.70,
                          "NO", f"{move24:.1%}/24h {move72:.1%}/72h — bearish")
        return Signal("momentum", cur, 0.30, "NEUTRAL",
                      f"No clear trend (24h: {move24:+.1%})")

    # ── Signal 3: Calibration ──────────────────────────────────────────────────

    def _calibration(self, market: Market) -> Signal:
        prompt = (
            f"Market: {market.question}\n"
            f"Current price: {market.yes_price:.2%}\n\n"
            f"Based on historical prediction market base rates, "
            f"is this market over- or under-priced?\n"
            f'Return ONLY JSON: {{"base_rate":0.0-1.0,"confidence":"low|medium|high","reasoning":"one sentence"}}'
        )
        try:
            resp  = self.ai.messages.create(
                model="claude-sonnet-4-20250514", max_tokens=100,
                messages=[{"role":"user","content":prompt}],
            )
            data  = _parse_ai_json(resp.content[0].text)
            base  = float(data["base_rate"])
            conf  = {"low":0.3,"medium":0.6,"high":0.85}.get(data.get("confidence","medium"),0.5)
            diff  = base - market.yes_price
            direc = "YES" if diff > 0.04 else "NO" if diff < -0.04 else "NEUTRAL"
            return Signal("calibration", base, conf, direc, data.get("reasoning",""))
        except Exception as e:
            log.debug(f"Calibration: {e}")
            return Signal("calibration", market.yes_price, 0.2, "NEUTRAL", "error")

    # ── Signal 4: Consensus (Kalshi comparison) ────────────────────────────────

    def _consensus(self, market: Market) -> Signal:
        kalshi = self._kalshi_price(market.question)
        if kalshi is None:
            return Signal("consensus", market.yes_price, 0.15, "NEUTRAL", "no Kalshi match")
        spread = market.yes_price - kalshi
        avg    = (market.yes_price + kalshi) / 2
        if abs(spread) < 0.02:
            return Signal("consensus", avg, 0.65, "NEUTRAL",
                          f"PM {market.yes_price:.2%} ≈ Kalshi {kalshi:.2%}")
        if spread > 0:
            return Signal("consensus", kalshi, min(0.85, abs(spread)*7), "NO",
                          f"PM {market.yes_price:.2%} > Kalshi {kalshi:.2%} — PM overpriced")
        return Signal("consensus", kalshi, min(0.85, abs(spread)*7), "YES",
                      f"Kalshi {kalshi:.2%} > PM {market.yes_price:.2%} — PM underpriced")

    # ── Signal 5: Base Rate ────────────────────────────────────────────────────

    def _base_rate(self, market: Market) -> Signal:
        prompt = (
            f"Question: {market.question}\n"
            f"Closing: {market.end_date}\n\n"
            f"Estimate BASE RATE probability of YES using ONLY prior knowledge. "
            f"Ignore market prices.\n"
            f'Return ONLY JSON: {{"probability":0.0-1.0,"reasoning":"one sentence"}}'
        )
        try:
            resp  = self.ai.messages.create(
                model="claude-sonnet-4-20250514", max_tokens=80,
                messages=[{"role":"user","content":prompt}],
            )
            data  = _parse_ai_json(resp.content[0].text)
            prob  = float(data["probability"])
            diff  = prob - market.yes_price
            direc = "YES" if diff > 0.04 else "NO" if diff < -0.04 else "NEUTRAL"
            return Signal("base_rate", prob, 0.50, direc, data.get("reasoning",""))
        except Exception as e:
            log.debug(f"Base rate: {e}")
            return Signal("base_rate", 0.5, 0.1, "NEUTRAL", "error")

    # ── Ensemble ───────────────────────────────────────────────────────────────

    def _ensemble(self, market: Market, signals: list[Signal]) -> EnsembleResult:
        total_w = sum(s.weight * s.confidence for s in signals)
        if total_w == 0:
            return EnsembleResult(0.5, "NEUTRAL", 0, 0.5, "low", signals)

        prob  = sum(s.score * s.weight * s.confidence for s in signals) / total_w
        edge  = abs(prob - market.yes_price)

        if   prob > market.yes_price + 0.03: direction = "YES"
        elif prob < market.yes_price - 0.03: direction = "NO"
        else:                                direction = "NEUTRAL"

        yes_v    = sum(1 for s in signals if s.direction == "YES")
        no_v     = sum(1 for s in signals if s.direction == "NO")
        agreement = max(yes_v, no_v) / len(signals)
        avg_conf  = sum(s.confidence for s in signals) / len(signals)

        if   avg_conf > 0.55 and agreement >= 0.55 and edge > 0.05: conf = "high"
        elif avg_conf > 0.30 and agreement >= 0.40 and edge > 0.02: conf = "medium"
        else:                                                          conf = "low"

        top3      = sorted(signals, key=lambda s: s.confidence*s.weight, reverse=True)[:3]
        reasoning = " · ".join(f"{s.name}: {s.reasoning}" for s in top3 if s.reasoning)

        return EnsembleResult(
            final_score    = round(prob, 4),
            direction      = direction,
            edge           = round(edge, 4),
            ai_probability = round(prob, 4),
            confidence     = conf,
            signals        = signals,
            reasoning      = reasoning,
        )

    def update_weights(self, performance: dict[str, float]) -> None:
        total = sum(max(0.01, v) for v in performance.values())
        for name, acc in performance.items():
            if name in self.weights:
                self.weights[name] = round(max(0.05, acc / total), 3)
        log.info(f"Signal weights updated: {self.weights}")

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _ai_call(self, name: str, prompt: str, fallback: float) -> Signal:
        try:
            resp  = self.ai.messages.create(
                model="claude-sonnet-4-20250514", max_tokens=120,
                messages=[{"role":"user","content":prompt}],
            )
            data  = _parse_ai_json(resp.content[0].text)
            prob  = float(data.get("probability", data.get("base_rate", 0.5)))
            conf  = {"low":0.30,"medium":0.60,"high":0.85}.get(
                data.get("confidence","medium"), 0.5)
            diff  = prob - fallback
            direc = "YES" if diff > 0.03 else "NO" if diff < -0.03 else "NEUTRAL"
            return Signal(name, prob, conf, direc, data.get("reasoning",""))
        except Exception as e:
            log.debug(f"{name} AI call error: {e}")
            return Signal(name, 0.5, 0.1, "NEUTRAL", "error")

    def _kalshi_price(self, question: str) -> Optional[float]:
        try:
            words = [w for w in question.lower().split() if len(w) > 3][:4]
            r = _SESSION.get(
                "https://trading-api.kalshi.com/trade-api/v2/markets",
                params={"status":"open","limit":20,"search":" ".join(words)},
                timeout=5,
            )
            if r.status_code != 200:
                return None
            for m in r.json().get("markets", []):
                title = m.get("title","").lower()
                if sum(1 for w in words if w in title) >= 2:
                    p = m.get("yes_ask") or m.get("last_price")
                    if p:
                        return float(p) / 100
        except Exception:
            pass
        return None
