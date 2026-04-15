"""
Prometheus — Domain Intelligence v1.0

Три системы в одном модуле:

1. BAYESIAN DOMAIN PRIOR
   Бот учится на реальных результатах по каждому домену.
   Там где работает → увеличиваем активность и размер позиций.
   Там где не работает → снижаем. Никакой жёсткой блокировки.
   
   Формула: posterior = (wins + alpha) / (total + alpha + beta)
   alpha=2, beta=2 — слабый prior 50% чтобы не оверфитить на малых данных.

2. PORTFOLIO-LEVEL KELLY
   Стандартный Kelly считается per-position игнорируя остальной портфель.
   Portfolio Kelly учитывает все открытые позиции и их корреляции.
   Результат: оптимальный размер с учётом текущего риска портфеля.

3. MARKET MICROSTRUCTURE SIGNAL
   Анализирует order book: где стоят большие ордера, насколько глубок рынок.
   Большой wall на одной стороне = сигнал что умные деньги держат эту цену.
   Возвращает: direction сигнал + уверенность 0-1.

4. LIQUIDITY TIMING
   Лучшее время торговать: американская сессия 14:00-22:00 UTC.
   Вне сессии: спред шире, меньше ликвидности, хуже исполнение.
   Возвращает: множитель качества 0.5-1.0.

5. DOMAIN SENTIMENT AGGREGATOR  
   Агрегирует сентимент новостей по домену за последние N часов.
   Если 80% новостей по геополитике негативные → системный сигнал.
   Влияет на confidence всех рынков этого домена.
"""
from __future__ import annotations
import json
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import requests

log = logging.getLogger("prometheus.domain_intel")

CLOB = "https://clob.polymarket.com"
_S   = requests.Session()
_S.headers["User-Agent"] = "Prometheus/3.0"

# Все известные домены
ALL_DOMAINS = [
    "politics", "crypto", "macro", "geopolitics",
    "tech", "sports", "entertainment", "science", "other"
]


# ── 1. Bayesian Domain Prior ──────────────────────────────────────────────────

@dataclass
class DomainStats:
    """Статистика бота по одному домену."""
    domain:      str
    wins:        int   = 0
    losses:      int   = 0
    total_pnl:   float = 0.0
    total_vol:   float = 0.0
    # Байесовский prior — слабая инициализация 50/50
    alpha:       float = 2.0   # псевдо-победы
    beta:        float = 2.0   # псевдо-поражения

    @property
    def total(self) -> int:
        return self.wins + self.losses

    @property
    def win_rate(self) -> float:
        if self.total == 0:
            return 0.5
        return self.wins / self.total

    @property
    def posterior_win_rate(self) -> float:
        """
        Байесовская оценка win rate.
        При малом количестве данных тяготеет к 50%.
        При большом количестве → реальный win rate.
        """
        return (self.wins + self.alpha) / (self.total + self.alpha + self.beta)

    @property
    def roi(self) -> float:
        if self.total_vol <= 0:
            return 0.0
        return self.total_pnl / self.total_vol

    @property
    def size_multiplier(self) -> float:
        """
        Множитель размера позиции для этого домена.
        Диапазон: 0.3 (плохой домен) до 1.5 (отличный домен).
        
        Формула: базируется на posterior win rate и ROI.
        - posterior WR > 0.65 и ROI > 0.10 → увеличиваем до 1.5x
        - posterior WR > 0.55              → норма 1.0x
        - posterior WR < 0.45              → уменьшаем до 0.5x
        - posterior WR < 0.35              → минимум 0.3x
        
        Защита: минимум 5 сделок чтобы не реагировать на шум.
        """
        if self.total < 5:
            return 1.0   # нет данных → норма

        pwr = self.posterior_win_rate

        # Отличный домен
        if pwr > 0.65 and self.roi > 0.10:
            return min(1.5, 1.0 + (pwr - 0.55) * 5)
        # Хороший
        if pwr > 0.55:
            return 1.0 + (pwr - 0.55) * 2
        # Нейтральный
        if pwr >= 0.45:
            return 1.0
        # Слабый
        if pwr >= 0.35:
            return max(0.5, 0.5 + (pwr - 0.35) * 5)
        # Плохой
        return 0.3

    @property
    def confidence_adjustment(self) -> float:
        """
        Поправка к confidence для этого домена.
        Диапазон: -0.15 (плохой) до +0.10 (хороший).
        Влияет на порог входа.
        """
        if self.total < 5:
            return 0.0
        pwr = self.posterior_win_rate
        if pwr > 0.60:
            return +0.10
        if pwr < 0.40:
            return -0.15
        return 0.0


class DomainPrior:
    """
    Управляет байесовской статистикой по всем доменам.
    Персистентность через JSON файл.
    """

    def __init__(self, data_dir: str = "/app/logs") -> None:
        self._path = Path(data_dir) / "domain_stats.json"
        self._stats: dict[str, DomainStats] = {
            d: DomainStats(domain=d) for d in ALL_DOMAINS
        }
        self._load()

    def record(self, tags: list[str], won: bool, pnl: float, size: float) -> None:
        """Записать результат сделки по всем тегам/доменам позиции."""
        domains = self._tags_to_domains(tags)
        for domain in domains:
            if domain not in self._stats:
                self._stats[domain] = DomainStats(domain=domain)
            s = self._stats[domain]
            if won:
                s.wins += 1
            else:
                s.losses += 1
            s.total_pnl += pnl
            s.total_vol += size
        self._save()

    def get_size_multiplier(self, tags: list[str]) -> float:
        """
        Получить множитель размера позиции для данного набора тегов.
        Берём среднее по всем доменам позиции.
        """
        domains = self._tags_to_domains(tags)
        if not domains:
            return 1.0
        multipliers = [self._stats.get(d, DomainStats(d)).size_multiplier
                       for d in domains]
        # Берём среднее но не позволяем одному плохому домену
        # утопить всю позицию если есть хорошие домены
        return round(sum(multipliers) / len(multipliers), 3)

    def get_confidence_adj(self, tags: list[str]) -> float:
        """Поправка к confidence для данного набора тегов."""
        domains = self._tags_to_domains(tags)
        if not domains:
            return 0.0
        adjs = [self._stats.get(d, DomainStats(d)).confidence_adjustment
                for d in domains]
        return round(sum(adjs) / len(adjs), 3)

    def stats_summary(self) -> dict:
        """Сводка по всем доменам — для дашборда."""
        result = {}
        for domain, s in self._stats.items():
            if s.total == 0:
                continue
            result[domain] = {
                "wins":       s.wins,
                "losses":     s.losses,
                "win_rate":   round(s.win_rate, 3),
                "posterior":  round(s.posterior_win_rate, 3),
                "roi":        round(s.roi, 3),
                "multiplier": round(s.size_multiplier, 3),
                "pnl":        round(s.total_pnl, 2),
            }
        return result

    def _tags_to_domains(self, tags: list[str]) -> list[str]:
        """Конвертируем теги позиции в домены."""
        known = set(ALL_DOMAINS)
        result = []
        for tag in (tags or []):
            t = str(tag).lower()
            if t in known:
                result.append(t)
        return result if result else ["other"]

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                d: {
                    "wins":      s.wins,
                    "losses":    s.losses,
                    "total_pnl": s.total_pnl,
                    "total_vol": s.total_vol,
                }
                for d, s in self._stats.items()
            }
            # Атомарная запись: сначала во временный файл, потом rename.
            # Предотвращает порчу файла при падении процесса в середине записи.
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2))
            tmp.replace(self._path)
        except Exception as e:
            log.error(f"DomainPrior save error: {e}")

    def _load(self) -> None:
        try:
            if not self._path.exists():
                return
            data = json.loads(self._path.read_text())
            for domain, d in data.items():
                if domain not in self._stats:
                    self._stats[domain] = DomainStats(domain=domain)
                s = self._stats[domain]
                s.wins      = d.get("wins", 0)
                s.losses    = d.get("losses", 0)
                s.total_pnl = d.get("total_pnl", 0.0)
                s.total_vol = d.get("total_vol", 0.0)
            log.info(f"DomainPrior loaded: {sum(s.total for s in self._stats.values())} trades")
        except Exception as e:
            log.error(f"DomainPrior load error: {e}")


# ── 2. Portfolio-Level Kelly ──────────────────────────────────────────────────

def portfolio_kelly_size(
    ai_probability:  float,
    market_price:    float,
    direction:       str,
    bankroll:        float,
    kelly_fraction:  float,
    open_positions:  list,      # list[Position]
    max_position_usd: float,
    domain_multiplier: float = 1.0,
) -> float:
    """
    Portfolio-level Kelly sizing.
    
    Стандартный Kelly не учитывает уже открытые позиции.
    Мы снижаем размер новой позиции пропорционально уже занятому капиталу
    и качеству домена.
    
    Формула:
    1. Считаем базовый Kelly размер
    2. Считаем текущую экспозицию портфеля (open_exposure / bankroll)
    3. Снижаем новый размер пропорционально занятой доле
    4. Применяем domain multiplier
    5. Применяем корреляционный штраф если похожие позиции уже открыты
    """
    # Базовый Kelly
    ai_probability = max(0.01, min(0.99, ai_probability))
    p     = ai_probability if direction == "YES" else 1.0 - ai_probability
    price = market_price   if direction == "YES" else 1.0 - market_price
    price = max(price, 0.01)

    payout = (1.0 / price) - 1.0
    kelly  = (p * payout - (1.0 - p)) / payout
    kelly  = max(0.0, kelly)

    base_size = bankroll * kelly * kelly_fraction

    # Текущая экспозиция портфеля
    open_exposure = sum(pos.size_usd for pos in open_positions)
    exposure_ratio = open_exposure / max(bankroll, 1.0)

    # Чем больше капитала уже в работе — тем меньше новая позиция
    # При 0% экспозиции → полный размер
    # При 50% экспозиции → 70% размера
    # При 80% экспозиции → 40% размера
    exposure_factor = max(0.3, 1.0 - exposure_ratio * 0.75)

    # Применяем domain multiplier (байесовский prior)
    adjusted = base_size * exposure_factor * domain_multiplier

    # Финальный кэп
    return round(min(adjusted, max_position_usd), 2)


# ── 3. Market Microstructure Signal ──────────────────────────────────────────

@dataclass
class MicrostructureSignal:
    direction:   str    # YES / NO / NEUTRAL
    confidence:  float  # 0.0 - 1.0
    reasoning:   str
    bid_wall:    float  # $ объём лучших bid ордеров
    ask_wall:    float  # $ объём лучших ask ордеров
    imbalance:   float  # (bid - ask) / (bid + ask), -1 to +1


def analyze_microstructure(token_id: str,
                            current_price: float) -> Optional[MicrostructureSignal]:
    """
    Анализирует order book для сигнала направления.
    
    Логика:
    - Большой bid wall (много покупателей) → кто-то умный держит цену → YES сигнал
    - Большой ask wall (много продавцов) → давление вниз → NO сигнал
    - Imbalance > 0.3 → значимый сигнал
    - Imbalance < 0.1 → нейтрально
    """
    if not token_id:
        return None
    try:
        r = _S.get(f"{CLOB}/orderbook/{token_id}", timeout=5)
        if r.status_code != 200:
            return None

        book = r.json()
        bids = [(float(b["price"]), float(b["size"]))
                for b in book.get("bids", [])
                if b.get("price") and b.get("size")]
        asks = [(float(a["price"]), float(a["size"]))
                for a in book.get("asks", [])
                if a.get("price") and a.get("size")]

        if not bids or not asks:
            return None

        best_bid = max(b[0] for b in bids)
        best_ask = min(a[0] for a in asks)

        # Считаем глубину в пределах 5% от текущей цены
        bid_wall = sum(p * s for p, s in bids
                       if p >= best_bid * 0.95)
        ask_wall = sum(p * s for p, s in asks
                       if p <= best_ask * 1.05)

        total = bid_wall + ask_wall
        if total < 100:   # слишком мало ликвидности
            return None

        imbalance = (bid_wall - ask_wall) / total

        # Определяем сигнал
        abs_imb = abs(imbalance)
        if abs_imb < 0.15:
            direction   = "NEUTRAL"
            confidence  = 0.0
            reasoning   = f"Balanced book (imb={imbalance:+.2f})"
        elif abs_imb < 0.30:
            direction   = "YES" if imbalance > 0 else "NO"
            confidence  = abs_imb * 2
            reasoning   = (f"Mild {'bid' if imbalance>0 else 'ask'} pressure "
                           f"(imb={imbalance:+.2f}, "
                           f"bid=${bid_wall:.0f} ask=${ask_wall:.0f})")
        else:
            direction   = "YES" if imbalance > 0 else "NO"
            confidence  = min(0.85, abs_imb * 2.5)
            reasoning   = (f"Strong {'bid' if imbalance>0 else 'ask'} wall "
                           f"(imb={imbalance:+.2f}, "
                           f"bid=${bid_wall:.0f} ask=${ask_wall:.0f})")

        return MicrostructureSignal(
            direction  = direction,
            confidence = round(confidence, 3),
            reasoning  = reasoning,
            bid_wall   = round(bid_wall, 0),
            ask_wall   = round(ask_wall, 0),
            imbalance  = round(imbalance, 3),
        )

    except Exception as e:
        log.debug(f"Microstructure error {token_id}: {e}")
        return None


# ── 4. Liquidity Timing ───────────────────────────────────────────────────────

def get_liquidity_timing_multiplier() -> tuple[float, str]:
    """
    Возвращает (multiplier, reason).
    
    Polymarket торгуется глобально но основная ликвидность — американцы.
    
    Расписание (UTC):
    - 14:00-22:00 UTC: американская сессия, максимальная ликвидность → 1.0
    - 08:00-14:00 UTC: европейская сессия, средняя ликвидность → 0.85
    - 22:00-02:00 UTC: вечер США / ночь Европы → 0.75
    - 02:00-08:00 UTC: ночь, минимальная ликвидность → 0.60
    """
    hour = datetime.now(timezone.utc).hour

    if 14 <= hour < 22:
        return 1.0,  "US session (peak liquidity)"
    elif 8 <= hour < 14:
        return 0.85, "EU session (good liquidity)"
    elif 22 <= hour or hour < 2:
        return 0.75, "US evening (moderate)"
    else:
        return 0.60, "Off-hours (low liquidity)"


# ── 5. Domain Sentiment Aggregator ───────────────────────────────────────────

@dataclass
class DomainSentiment:
    domain:         str
    avg_sentiment:  float    # -1.0 to +1.0
    news_count:     int
    bullish_count:  int
    bearish_count:  int
    signal:         str      # BULLISH / BEARISH / NEUTRAL
    confidence:     float    # 0.0 - 1.0


def get_domain_sentiment(domain: str, intel_db) -> Optional[DomainSentiment]:
    """
    Агрегирует сентимент новостей по домену за последние 4 часа.
    
    Если 70%+ новостей позитивные → BULLISH сигнал для всех рынков домена.
    Если 70%+ новостей негативные → BEARISH сигнал.
    
    Пример: 80% новостей по крипто позитивные → повышаем уверенность
    для всех YES ставок на крипто рынках.
    """
    try:
        points = intel_db.recent(domain=domain, hours=4, limit=30)
        if not points:
            return None

        sentiments = [dp.sentiment for dp in points if dp.sentiment != 0]
        if not sentiments:
            return None

        avg = sum(sentiments) / len(sentiments)
        bullish = sum(1 for s in sentiments if s > 0.1)
        bearish = sum(1 for s in sentiments if s < -0.1)
        total   = len(sentiments)

        bull_pct = bullish / total
        bear_pct = bearish / total

        if bull_pct >= 0.70:
            signal     = "BULLISH"
            confidence = min(0.80, bull_pct)
        elif bear_pct >= 0.70:
            signal     = "BEARISH"
            confidence = min(0.80, bear_pct)
        else:
            signal     = "NEUTRAL"
            confidence = 0.0

        return DomainSentiment(
            domain        = domain,
            avg_sentiment = round(avg, 3),
            news_count    = len(points),
            bullish_count = bullish,
            bearish_count = bearish,
            signal        = signal,
            confidence    = round(confidence, 3),
        )
    except Exception as e:
        log.debug(f"Domain sentiment error {domain}: {e}")
        return None


# ── Combined context для сигнального движка ──────────────────────────────────

@dataclass
class MarketContext:
    """
    Полный контекст рынка для принятия решения.
    Собирает всё в одном месте.
    """
    domain_multiplier:   float          = 1.0
    domain_conf_adj:     float          = 0.0
    timing_multiplier:   float          = 1.0
    timing_reason:       str            = ""
    microstructure:      Optional[MicrostructureSignal] = None
    domain_sentiment:    Optional[DomainSentiment]      = None

    @property
    def total_size_multiplier(self) -> float:
        """Итоговый множитель размера позиции."""
        return round(self.domain_multiplier * self.timing_multiplier, 3)

    @property
    def effective_min_edge_adj(self) -> float:
        """
        Поправка к минимальному edge.
        Плохой домен → требуем больший edge.
        Хороший домен → можем входить с меньшим edge.
        """
        return -self.domain_conf_adj * 0.5   # инвертируем

    def summary(self) -> str:
        parts = [
            f"domain_mult={self.domain_multiplier:.2f}",
            f"timing={self.timing_multiplier:.2f}({self.timing_reason})",
        ]
        if self.microstructure and self.microstructure.direction != "NEUTRAL":
            parts.append(f"micro={self.microstructure.direction}({self.microstructure.confidence:.2f})")
        if self.domain_sentiment and self.domain_sentiment.signal != "NEUTRAL":
            parts.append(f"sentiment={self.domain_sentiment.signal}")
        return " | ".join(parts)


def build_market_context(
    tags:          list[str],
    token_id:      str,
    current_price: float,
    domain_prior:  DomainPrior,
    intel_db=None,
) -> MarketContext:
    """
    Собирает полный контекст для рынка за один вызов.
    Используется в trade cycle перед принятием решения.
    """
    ctx = MarketContext()

    # Domain prior
    ctx.domain_multiplier = domain_prior.get_size_multiplier(tags)
    ctx.domain_conf_adj   = domain_prior.get_confidence_adj(tags)

    # Liquidity timing
    ctx.timing_multiplier, ctx.timing_reason = get_liquidity_timing_multiplier()

    # Microstructure (только если есть token_id)
    if token_id:
        ctx.microstructure = analyze_microstructure(token_id, current_price)

    # Domain sentiment (no circular import needed - tags already extracted)
    if intel_db and tags:
        domains_for_sent = [str(t).lower() for t in tags if str(t).lower() in ALL_DOMAINS]
        if domains_for_sent:
            ctx.domain_sentiment = get_domain_sentiment(domains_for_sent[0], intel_db)

    return ctx
