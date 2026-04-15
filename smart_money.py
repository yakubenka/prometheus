"""
Prometheus — Smart Money Tracker v3.5
Стратегия: найти инсайдеров → следить за ними → копировать их сделки
как можно быстрее после того как они входят.

АРХИТЕКТУРА:
1. fetch_large_trades() → видим кто ставит $3k+ прямо сейчас
2. enrich_trades_with_outcomes() → обогащаем историю кошелька реальными исходами
   через Gamma API (рынок закрыт → берём финальную цену как outcome)
3. WalletAnalyzer → строим профиль: WR, ROI, специализации, insider_score
4. SmartMoneyMonitor.scan() → если известный инсайдер только что вошёл →
   сигнал "копируй немедленно"

КЛЮЧЕВАЯ ИДЕЯ:
Инсайдер знает результат заранее → ставит на низкую вероятность (5-20%)
→ рынок ещё не знает → у нас есть окно 5-30 мин чтобы войти рядом
→ новость выходит → цена летит вверх → профит

БАГ-ФИКСЫ v3.5:
- enrich_trades_with_outcomes(): обогащаем сделки outcome через Gamma API
  (решает проблему пустых resolved_trades)
- Снижен MIN_RESOLVED с 10 до 5 (быстрее строим профили)
- Добавлен SPEED_BONUS: чем раньше вошёл до движения цены — тем выше score
- Insider detection улучшен: big_upset_score учитывает размер ставки
- scan() теперь сортирует по времени (свежие сделки важнее)
"""
from __future__ import annotations
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Optional
from collections import defaultdict

import requests

from data import Trade, fetch_wallet_trades, fetch_large_trades

log = logging.getLogger("prometheus.smart_money")

GAMMA = "https://gamma-api.polymarket.com"
_S    = requests.Session()
_S.headers["User-Agent"] = "Prometheus/3.0"

# Кэш outcomes по market_id (protected by _OUTCOME_CACHE_LOCK for thread safety)
_OUTCOME_CACHE: dict[str, Optional[str]] = {}
_OUTCOME_CACHE_TS: dict[str, float] = {}
_OUTCOME_CACHE_LOCK = threading.Lock()
_OUTCOME_TTL = 3600              # 1 час — TTL записи
_OUTCOME_CACHE_CLEANUP_INTERVAL = 300   # запускать чистку раз в 5 минут
_OUTCOME_CACHE_LAST_CLEANUP: float = 0.0


class TraderClass(Enum):
    INSIDER     = "insider"
    SHARP       = "sharp"
    CONTRARIAN  = "contrarian"
    NOISE       = "noise"

    @property
    def emoji(self) -> str:
        return {"insider":"🔴","sharp":"🟡","contrarian":"🔵","noise":"⚪"}[self.value]


@dataclass
class WalletProfile:
    address:          str
    trader_class:     TraderClass = TraderClass.NOISE
    win_rate:         float = 0.0
    roi:              float = 0.0
    total_trades:     int   = 0
    resolved_trades:  int   = 0
    total_volume:     float = 0.0
    avg_edge:         float = 0.0
    calibration:      float = 0.0
    insider_score:    float = 0.0
    sharp_score:      float = 0.0
    specializations:  list  = field(default_factory=list)
    big_upsets:       int   = 0       # выиграл когда рынок давал <20%
    speed_score:      float = 0.0     # насколько рано входит до движения
    sample_size_ok:   bool  = False
    last_updated:     datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class SmartSignal:
    wallet:      WalletProfile
    market_id:   str
    question:    str
    direction:   str
    entry_price: float
    their_size:  float
    our_size:    float
    strength:    float
    signal_type: str
    reasoning:   str
    minutes_ago: float = 0.0
    timestamp:   datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    token_id:    str = ""    # token_id нашей стороны (YES или NO)
    slug:        str = ""    # slug для прямой ссылки на Polymarket


# ── Outcome enrichment ────────────────────────────────────────────────────────

def _maybe_cleanup_outcome_cache() -> None:
    """Удаляет просроченные записи из кэша. Запускается не чаще раза в 5 минут."""
    global _OUTCOME_CACHE_LAST_CLEANUP
    now = time.time()
    if now - _OUTCOME_CACHE_LAST_CLEANUP < _OUTCOME_CACHE_CLEANUP_INTERVAL:
        return
    with _OUTCOME_CACHE_LOCK:
        # Обновляем timestamp внутри lock чтобы избежать гонки
        _OUTCOME_CACHE_LAST_CLEANUP = now
        expired = [mid for mid, ts in list(_OUTCOME_CACHE_TS.items()) if now - ts > _OUTCOME_TTL]
        for mid in expired:
            _OUTCOME_CACHE.pop(mid, None)
            _OUTCOME_CACHE_TS.pop(mid, None)
    if expired:
        log.debug(f"Outcome cache cleanup: удалено {len(expired)} просроченных записей")


def _fetch_market_outcome(market_id: str) -> Optional[str]:
    """
    Получить outcome закрытого рынка через Gamma API.
    YES если финальная цена >= 0.97, NO если <= 0.03.
    Кэшируем на 1 час.
    """
    _maybe_cleanup_outcome_cache()
    now = time.time()
    with _OUTCOME_CACHE_LOCK:
        if market_id in _OUTCOME_CACHE:
            if now - _OUTCOME_CACHE_TS.get(market_id, 0) < _OUTCOME_TTL:
                return _OUTCOME_CACHE[market_id]

    try:
        r = _S.get(f"{GAMMA}/markets/{market_id}", timeout=6)
        if r.status_code != 200:
            return None
        m = r.json()

        # Прямые поля резолюции
        winner = m.get("winner") or m.get("resolvedOutcome") or m.get("resolution")
        if winner:
            w = str(winner).upper()
            if w in ("YES", "1", "TRUE"):
                with _OUTCOME_CACHE_LOCK:
                    _OUTCOME_CACHE[market_id] = "YES"
                    _OUTCOME_CACHE_TS[market_id] = now
                return "YES"
            if w in ("NO", "0", "FALSE"):
                with _OUTCOME_CACHE_LOCK:
                    _OUTCOME_CACHE[market_id] = "NO"
                    _OUTCOME_CACHE_TS[market_id] = now
                return "NO"

        # По финальной цене
        if m.get("closed") or not m.get("active", True):
            prices = m.get("outcomePrices")
            if isinstance(prices, str):
                import json
                prices = json.loads(prices)
            if prices:
                yes_price = float(prices[0])
                if yes_price >= 0.97:
                    result = "YES"
                elif yes_price <= 0.03:
                    result = "NO"
                else:
                    result = None
                with _OUTCOME_CACHE_LOCK:
                    _OUTCOME_CACHE[market_id] = result
                    _OUTCOME_CACHE_TS[market_id] = now
                return result

        # Рынок ещё открыт
        with _OUTCOME_CACHE_LOCK:
            _OUTCOME_CACHE[market_id] = None
            _OUTCOME_CACHE_TS[market_id] = now
        return None

    except Exception as e:
        log.debug(f"fetch_market_outcome {market_id}: {e}")
        return None


def enrich_trades_with_outcomes(trades: list[Trade]) -> list[Trade]:
    """
    Обогащаем сделки реальными outcomes через Gamma API.
    Это решает главную проблему: /activity API не возвращает outcome.
    Батчим запросы — один запрос на уникальный market_id.
    """
    # Уникальные market_ids без outcome
    missing = {t.market_id for t in trades
               if t.outcome is None and t.market_id}

    log.debug(f"Enriching outcomes for {len(missing)} markets...")

    from config import cfg as _cfg

    enriched_map: dict[str, Optional[str]] = {}
    for mid in list(missing)[:50]:   # максимум 50 запросов
        outcome = _fetch_market_outcome(mid)
        if outcome:
            enriched_map[mid] = outcome
        time.sleep(_cfg.enrich_request_interval)

    # Применяем outcomes
    result = []
    for t in trades:
        if t.outcome is None and t.market_id in enriched_map:
            # Trade frozen=True — создаём новый объект с outcome
            from dataclasses import replace
            t = replace(t, outcome=enriched_map[t.market_id])
        result.append(t)

    resolved = sum(1 for t in result if t.outcome is not None)
    log.debug(f"Enrichment: {resolved}/{len(result)} trades have outcome")
    return result


# ── Wallet Analyzer ───────────────────────────────────────────────────────────

class WalletAnalyzer:
    MIN_RESOLVED = 5    # снижено с 10 — быстрее строим профили
    CACHE_TTL    = 3600

    def __init__(self) -> None:
        self._cache: dict[str, WalletProfile] = {}

    def analyze(self, address: str, force: bool = False) -> WalletProfile:
        addr = address.lower()
        if not force and addr in self._cache:
            cached = self._cache[addr]
            age = (datetime.now(timezone.utc) - cached.last_updated).total_seconds()
            if age < self.CACHE_TTL:
                return cached

        trades  = fetch_wallet_trades(addr, limit=300)
        # КЛЮЧЕВОЙ ФИКс: обогащаем outcomes через API
        trades  = enrich_trades_with_outcomes(trades)
        profile = self._build(addr, trades)
        self._cache[addr] = profile
        return profile

    def _build(self, address: str, trades: list[Trade]) -> WalletProfile:
        p = WalletProfile(address=address)
        if not trades:
            return p

        resolved = [t for t in trades if t.outcome is not None]
        p.total_trades    = len(trades)
        p.resolved_trades = len(resolved)
        p.total_volume    = sum(t.size for t in trades)
        p.sample_size_ok  = len(resolved) >= self.MIN_RESOLVED

        if not resolved:
            log.debug(f"  {address[:10]}… — 0 resolved trades (API missing outcomes)")
            return p

        # Win rate: BUY+YES или SELL+NO
        wins = [
            t for t in resolved
            if (t.side == "BUY"  and t.outcome == "YES") or
               (t.side == "SELL" and t.outcome == "NO")
        ]
        p.win_rate = len(wins) / len(resolved)

        total_pnl = sum(self._pnl(t) for t in resolved)
        vol_res   = sum(t.size for t in resolved)
        p.roi     = total_pnl / vol_res if vol_res > 0 else 0.0

        edges = []
        for t in resolved:
            actual      = 1.0 if t.outcome == "YES" else 0.0
            trade_price = t.price if t.side == "BUY" else 1.0 - t.price
            edges.append(abs(actual - trade_price))
        p.avg_edge = sum(edges) / len(edges) if edges else 0

        p.calibration     = self._calibration(resolved)
        p.specializations = self._specializations(resolved)

        # Big upsets: выиграл когда рынок давал <= 20% — признак инсайдера
        p.big_upsets = sum(
            1 for t in resolved
            if t.price <= 0.20
            and t.size >= 1000
            and t.side == "BUY"
            and t.outcome == "YES"
        )

        # Speed score: как рано входит до движения рынка
        p.speed_score     = self._speed_score(resolved)
        p.insider_score   = self._insider_score(p, resolved)
        p.sharp_score     = self._sharp_score(p)
        p.trader_class    = self._classify(p)
        p.last_updated    = datetime.now(timezone.utc)

        log.info(
            f"  Wallet {address[:10]}… | {p.trader_class.value} | "
            f"WR={p.win_rate:.1%} ROI={p.roi:.1%} "
            f"n={p.resolved_trades}/{p.total_trades} "
            f"upsets={p.big_upsets}"
        )
        return p

    def _pnl(self, t: Trade) -> float:
        if t.side == "BUY":
            if t.outcome == "YES":
                return t.size * ((1.0 / max(t.price, 0.01)) - 1)
            return -t.size
        else:
            no_price = 1.0 - t.price
            if t.outcome == "NO":
                return t.size * ((1.0 / max(no_price, 0.01)) - 1)
            return -t.size

    def _calibration(self, trades: list[Trade]) -> float:
        buckets: dict[float, list[bool]] = defaultdict(list)
        for t in trades:
            price = t.price if t.side == "BUY" else 1.0 - t.price
            won   = (t.side == "BUY" and t.outcome == "YES") or \
                    (t.side == "SELL" and t.outcome == "NO")
            buckets[round(price * 10) / 10].append(won)
        errors = [
            (prob - sum(outcomes)/len(outcomes)) ** 2
            for prob, outcomes in buckets.items()
            if len(outcomes) >= 3
        ]
        return max(0.0, 1.0 - (sum(errors)/len(errors))*4) if errors else 0.5

    def _specializations(self, trades: list[Trade]) -> list[str]:
        DOMAINS = {
            "geopolitics": ["iran","israel","russia","ukraine","china","war","attack","military","nato"],
            "politics":    ["election","president","congress","vote","trump","senate","democrat"],
            "macro":       ["fed","rate","inflation","cpi","gdp","fomc","recession","treasury"],
            "crypto":      ["bitcoin","btc","eth","crypto","solana","defi","binance"],
        }
        results: dict[str, list[bool]] = defaultdict(list)
        for t in trades:
            q   = t.question.lower()
            won = (t.side == "BUY"  and t.outcome == "YES") or \
                  (t.side == "SELL" and t.outcome == "NO")
            for domain, kws in DOMAINS.items():
                if any(kw in q for kw in kws):
                    results[domain].append(won)
        return [
            d for d, r in results.items()
            if len(r) >= 3 and sum(r)/len(r) > 0.60
        ]

    def _speed_score(self, trades: list[Trade]) -> float:
        """
        Насколько рано трейдер входит до движения цены.
        Высокий score = входит когда цена ещё низкая → инсайдер.
        """
        early_wins = sum(
            1 for t in trades
            if t.price <= 0.25
            and t.side == "BUY"
            and t.outcome == "YES"
        )
        if not trades:
            return 0.0
        return min(1.0, early_wins / max(len(trades) * 0.1, 1))

    def _insider_score(self, p: WalletProfile, trades: list[Trade]) -> float:
        if not p.sample_size_ok:
            return 0.0
        score = 0.0
        # Win rate
        if p.win_rate > 0.80:   score += 0.25
        elif p.win_rate > 0.70: score += 0.12
        # Специализация в geo/politics — классические инсайдерские домены
        if {"geopolitics", "politics"} & set(p.specializations):
            score += 0.20
        # Big upsets — ключевой признак инсайдера (цена <= 20%, выиграл)
        if p.big_upsets >= 5:   score += 0.30
        elif p.big_upsets >= 3: score += 0.20
        elif p.big_upsets >= 1: score += 0.10
        # Speed score — входит рано когда цена ещё низкая
        score += p.speed_score * 0.15
        # Объём — инсайдеры ставят серьёзные деньги
        if p.total_volume > 200_000: score += 0.15
        elif p.total_volume > 50_000: score += 0.08
        # НОВОЕ: бонус если трейдер стабильно выигрывает на ценах <= 10¢
        ultra_low_wins = sum(
            1 for t in trades
            if t.price <= 0.10
            and t.side == "BUY"
            and t.outcome == "YES"
            and t.size >= 500
        )
        if ultra_low_wins >= 3: score += 0.20
        elif ultra_low_wins >= 1: score += 0.10
        return min(1.0, score)

    def _sharp_score(self, p: WalletProfile) -> float:
        if not p.sample_size_ok:
            return 0.0
        score = 0.0
        if p.roi > 0.20:    score += 0.30
        elif p.roi > 0.08:  score += 0.15
        if p.calibration > 0.75:  score += 0.25
        elif p.calibration > 0.55: score += 0.12
        if p.win_rate > 0.62 and p.resolved_trades >= 15: score += 0.25
        elif p.win_rate > 0.58 and p.resolved_trades >= 5: score += 0.12
        if p.total_volume > 50_000: score += 0.12
        elif p.total_volume > 15_000: score += 0.06
        return min(1.0, score)

    def _classify(self, p: WalletProfile) -> TraderClass:
        if not p.sample_size_ok:
            return TraderClass.NOISE
        if p.insider_score >= 0.45:
            return TraderClass.INSIDER
        if p.sharp_score >= 0.50 and p.roi > 0.05:
            return TraderClass.SHARP
        if p.win_rate > 0.65 and p.avg_edge > 0.35 and p.resolved_trades >= 10:
            return TraderClass.CONTRARIAN
        return TraderClass.NOISE


# ── Smart Money Monitor ───────────────────────────────────────────────────────

class SmartMoneyMonitor:
    MAX_SIGNALS = 5

    def __init__(self, max_position_usd: float = 20.0) -> None:
        self.analyzer          = WalletAnalyzer()
        self.max_position_usd  = max_position_usd
        self.known_wallets:    dict[str, WalletProfile] = {}
        self._total_scanned:   int = 0

    def scan(self) -> list[SmartSignal]:
        log.info("🔍 Smart Money скан...")
        signals: list[SmartSignal] = []
        seen:    set[str]          = set()

        large   = fetch_large_trades(min_size=3000, hours_back=6)
        unusual = [t for t in large if t.is_unusual]
        self._total_scanned += len(unusual)
        log.info(f"Необычных сделок: {len(unusual)} из {len(large)}")

        # Сортируем по времени — свежие сделки важнее
        unusual.sort(key=lambda t: t.timestamp, reverse=True)

        for trade in unusual:
            if not trade.maker or trade.maker in seen:
                continue
            seen.add(trade.maker)

            profile = self.analyzer.analyze(trade.maker)
            self.known_wallets[trade.maker] = profile

            if profile.trader_class == TraderClass.NOISE:
                continue

            minutes_ago = (
                datetime.now(timezone.utc) - trade.timestamp
            ).total_seconds() / 60

            sig = self._make_signal(trade, profile, minutes_ago)
            if sig:
                signals.append(sig)
                log.info(
                    f"  🎯 {profile.trader_class.emoji} {profile.trader_class.value} "
                    f"| {trade.question[:50]} "
                    f"| {sig.direction} ${trade.size:,.0f} "
                    f"| {minutes_ago:.0f}min ago"
                )

        # Мониторим известных инсайдеров и sharp трейдеров
        cutoff = datetime.now(timezone.utc) - timedelta(hours=2)
        for addr, profile in list(self.known_wallets.items()):
            if profile.trader_class == TraderClass.NOISE or addr in seen:
                continue
            recent = fetch_wallet_trades(addr, limit=5)
            for trade in recent:
                if trade.timestamp < cutoff or trade.size < 500:
                    continue
                minutes_ago = (
                    datetime.now(timezone.utc) - trade.timestamp
                ).total_seconds() / 60
                sig = self._make_signal(trade, profile, minutes_ago)
                if sig:
                    signals.append(sig)

        # Дедупликация + сортировка по силе сигнала
        seen_mkts: set[str] = set()
        unique: list[SmartSignal] = []
        for s in sorted(signals, key=lambda x: x.strength, reverse=True):
            key = f"{s.market_id}_{s.direction}"
            if key not in seen_mkts:
                seen_mkts.add(key)
                unique.append(s)
            if len(unique) >= self.MAX_SIGNALS:
                break

        log.info(f"Smart Money сигналов: {len(unique)}")
        return unique

    def _make_signal(
        self,
        trade:       Trade,
        profile:     WalletProfile,
        minutes_ago: float = 0.0,
    ) -> Optional[SmartSignal]:

        strength = self._strength(trade, profile, minutes_ago)

        # Insiders — порог ниже, хотим следовать им быстрее
        min_str = 0.45 if profile.trader_class == TraderClass.INSIDER else 0.55

        if strength < min_str:
            return None

        direction = "YES" if trade.side == "BUY" else "NO"
        our_size  = round(min(self.max_position_usd * strength, self.max_position_usd), 2)

        # HIGH-UPSIDE BONUS: если инсайдер ставит на цену <= 10¢
        # потенциал x10+ → увеличиваем размер до 1.5x, но не более $30
        token_price = trade.price if direction == "YES" else 1.0 - trade.price
        is_high_upside = (
            token_price <= 0.10
            and profile.trader_class == TraderClass.INSIDER
            and profile.big_upsets >= 1
            and trade.size >= 2000  # инсайдер сам поставил серьёзно
        )
        if is_high_upside:
            our_size = round(min(our_size * 1.5, min(self.max_position_usd * 1.5, 30.0)), 2)
            log.info(
                f"  🎯 HIGH-UPSIDE: price={token_price:.1%} "
                f"upside=x{1/max(token_price,0.01):.0f} "
                f"size bumped to ${our_size:.2f}"
            )

        stype = (
            "insider_bet"  if profile.trader_class == TraderClass.INSIDER else
            "sharp_follow" if profile.trader_class == TraderClass.SHARP   else
            "contrarian"
        )

        # Формируем reasoning
        urgency = ""
        if minutes_ago < 5:
            urgency = " 🔥 ТОЛЬКО ЧТО"
        elif minutes_ago < 15:
            urgency = f" ⚡ {minutes_ago:.0f} мин назад"
        else:
            urgency = f" · {minutes_ago:.0f} мин назад"

        domains_str = (
            f" | {', '.join(profile.specializations)}"
            if profile.specializations else ""
        )

        reasoning = (
            f"{profile.trader_class.emoji} {profile.trader_class.value.upper()}{urgency}\n"
            f"WR {profile.win_rate:.0%}  ROI {profile.roi:+.0%}  "
            f"({profile.resolved_trades} resolved){domains_str}\n"
            f"${trade.size:,.0f} @ {trade.price:.2%}"
            + (f"  · {profile.big_upsets} big upsets" if profile.big_upsets else "")
        )

        # Получаем token_id и slug по market_id через Gamma API
        _token_id = ""
        _slug     = ""
        try:
            import requests as _req
            _r = _req.get(f"https://gamma-api.polymarket.com/markets/{trade.market_id}", timeout=5)
            if _r.status_code == 200:
                _m = _r.json()
                _slug = _m.get("groupSlug") or _m.get("marketSlug") or _m.get("slug") or ""
                _tokens = _m.get("clobTokenIds") or []
                if isinstance(_tokens, str):
                    import json as _json
                    _tokens = _json.loads(_tokens)
                # tokens[0] = YES, tokens[1] = NO
                if direction == "YES" and len(_tokens) >= 1:
                    _token_id = str(_tokens[0])
                elif direction == "NO" and len(_tokens) >= 2:
                    _token_id = str(_tokens[1])
        except Exception as _e:
            log.debug(f"SM token_id fetch failed: {_e}")

        return SmartSignal(
            wallet      = profile,
            market_id   = trade.market_id,
            question    = trade.question,
            direction   = direction,
            entry_price = trade.price,
            their_size  = trade.size,
            our_size    = our_size,
            strength    = strength,
            signal_type = stype,
            reasoning   = reasoning,
            minutes_ago = minutes_ago,
            token_id    = _token_id,
            slug        = _slug,
        )

    def _strength(
        self,
        trade:       Trade,
        profile:     WalletProfile,
        minutes_ago: float = 0.0,
    ) -> float:
        s = 0.0

        # База по классу
        if profile.trader_class == TraderClass.INSIDER:    s += 0.40
        elif profile.trader_class == TraderClass.SHARP:    s += 0.28
        elif profile.trader_class == TraderClass.CONTRARIAN: s += 0.18

        # Win rate
        if profile.win_rate > 0.80:   s += 0.20
        elif profile.win_rate > 0.70: s += 0.12
        elif profile.win_rate > 0.60: s += 0.06

        # Размер сделки — большая ставка = больше уверенность
        if trade.size > 100_000: s += 0.25
        elif trade.size > 50_000: s += 0.20
        elif trade.size > 20_000: s += 0.15
        elif trade.size > 5_000:  s += 0.08

        # Insider score
        s += profile.insider_score * 0.15

        # SPEED PENALTY: чем старше сделка тем меньше смысл копировать
        # Рынок уже мог переоцениться
        if minutes_ago < 5:    s += 0.10   # бонус за свежесть
        elif minutes_ago < 15: s += 0.05
        elif minutes_ago > 60: s -= 0.10   # штраф за опоздание
        elif minutes_ago > 120: s -= 0.20  # серьёзный штраф

        return min(1.0, max(0.0, s))
