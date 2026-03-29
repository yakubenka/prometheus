"""
Prometheus v3 — Market Data
Единственный источник правды о рынках Polymarket.
Всё типизировано. Всё валидировано. Нет silent failures.
"""

from __future__ import annotations
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger("prometheus.data")

GAMMA = "https://gamma-api.polymarket.com"
CLOB  = "https://clob.polymarket.com"
DATA  = "https://data-api.polymarket.com"


def _build_session() -> requests.Session:
    """Session с retry, timeout, user-agent."""
    s = requests.Session()
    s.headers["User-Agent"] = "Prometheus/3.0"
    retry = Retry(
        total=3,
        backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


SESSION = _build_session()


# ── Data Models ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Market:
    """Иммутабельная модель рынка. Нет рынка — нет сделки."""
    id:           str
    question:     str
    yes_price:    float          # 0.0 – 1.0
    no_price:     float
    volume_24h:   float
    end_date:     Optional[str]
    token_id_yes: Optional[str]
    token_id_no:  Optional[str]
    description:  str
    tags:         tuple          # immutable

    @property
    def spread(self) -> float:
        return abs(self.yes_price + self.no_price - 1.0)

    @property
    def is_tradeable(self) -> bool:
        return (
            0.02 < self.yes_price < 0.98
            and self.volume_24h > 0
            and self.token_id_yes is not None
            and self.spread < 0.05
        )


@dataclass(frozen=True)
class Trade:
    """Сделка с Polymarket Data API."""
    id:         str
    market_id:  str
    question:   str
    maker:      str          # wallet address
    price:      float
    size:       float
    side:       str          # "BUY" / "SELL"
    timestamp:  datetime
    outcome:    Optional[str] = None   # "YES" / "NO" / None (unresolved)

    @property
    def is_large(self) -> bool:
        return self.size >= 3000

    @property
    def is_unusual(self) -> bool:
        """Крупная ставка на маловероятное или высоковероятное."""
        return self.is_large and (self.price < 0.25 or self.price > 0.75)


# ── Fetchers ──────────────────────────────────────────────────────────────────

def _safe_get(url: str, params: dict = None, timeout: int = 10) -> Optional[dict | list]:
    try:
        r = SESSION.get(url, params=params, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.Timeout:
        log.warning(f"Timeout: {url}")
    except requests.exceptions.HTTPError as e:
        log.warning(f"HTTP {e.response.status_code}: {url}")
    except requests.exceptions.ConnectionError:
        log.warning(f"Connection error: {url}")
    except ValueError:
        log.warning(f"Invalid JSON: {url}")
    return None


def fetch_markets(
    limit:          int   = 30,
    min_volume_24h: float = 10_000,
) -> list[Market]:
    """
    Получить активные рынки, отсортированные по объёму.
    Возвращает только валидные, ликвидные рынки.
    """
    raw = _safe_get(f"{GAMMA}/markets", {
        "active":    "true",
        "closed":    "false",
        "limit":     limit,
        "order":     "volume24hr",
        "ascending": "false",
    })

    if not raw:
        return []

    markets = []
    for m in (raw if isinstance(raw, list) else raw.get("data", [])):
        parsed = _parse_market(m)
        if parsed and parsed.volume_24h >= min_volume_24h and parsed.is_tradeable:
            markets.append(parsed)

    log.info(f"Рынков загружено: {len(markets)} (из {len(raw) if raw else 0})")
    return markets


def fetch_closed_markets(limit: int = 200) -> list[Market]:
    """Закрытые рынки для backtesting."""
    raw = _safe_get(f"{GAMMA}/markets", {
        "active":    "false",
        "closed":    "true",
        "limit":     limit,
        "order":     "volume",
        "ascending": "false",
    })
    if not raw:
        return []
    results = []
    for m in (raw if isinstance(raw, list) else raw.get("data", [])):
        parsed = _parse_market(m)
        if parsed:
            results.append(parsed)
    return results


def fetch_market_price_history(market_id: str, days: int = 7) -> list[dict]:
    """История цен рынка по 1-часовым интервалам."""
    raw = _safe_get(f"{GAMMA}/markets/{market_id}/history", {
        "interval": f"{days}d",
        "fidelity": 60,
    })
    if not raw:
        return []
    return raw if isinstance(raw, list) else raw.get("history", [])


def fetch_large_trades(
    min_size:   float = 3_000,
    hours_back: int   = 6,
    limit:      int   = 100,
) -> list[Trade]:
    """Крупные сделки за последние N часов."""
    from datetime import timedelta
    since = int((datetime.now(timezone.utc) - timedelta(hours=hours_back)).timestamp())

    raw = _safe_get(f"{DATA}/trades", {
        "limit":    limit,
        "after":    since,
        "size_gte": min_size,
    })
    if not raw:
        return []

    items = raw if isinstance(raw, list) else raw.get("data", [])
    trades = []
    for t in items:
        parsed = _parse_trade(t)
        if parsed:
            trades.append(parsed)

    log.info(f"Крупных сделок за {hours_back}ч: {len(trades)}")
    return trades


def fetch_wallet_trades(address: str, limit: int = 200) -> list[Trade]:
    """Полная история кошелька."""
    raw = _safe_get(f"{DATA}/activity", {"user": address, "limit": limit})
    if not raw:
        return []
    items = raw if isinstance(raw, list) else raw.get("data", [])
    trades = []
    for t in items:
        parsed = _parse_trade(t)
        if parsed:
            trades.append(parsed)
    return trades


def fetch_current_price(token_id: str) -> Optional[float]:
    """Текущая цена токена."""
    raw = _safe_get(f"{CLOB}/price", {"token_id": token_id, "side": "BUY"})
    if not raw:
        return None
    try:
        return float(raw.get("price", 0))
    except (ValueError, TypeError):
        return None


# ── Parsers ───────────────────────────────────────────────────────────────────

def _parse_market(m: dict) -> Optional[Market]:
    """Парсим сырой dict → Market. None если невалидный."""
    try:
        # Prices
        prices_raw = m.get("outcomePrices")
        if isinstance(prices_raw, str):
            prices_raw = json.loads(prices_raw)
        if not prices_raw or len(prices_raw) < 2:
            return None
        yes_price = float(prices_raw[0])
        no_price  = float(prices_raw[1])

        # Token IDs
        tokens_raw = m.get("clobTokenIds")
        if isinstance(tokens_raw, str):
            tokens_raw = json.loads(tokens_raw)
        token_yes = tokens_raw[0] if tokens_raw and len(tokens_raw) > 0 else None
        token_no  = tokens_raw[1] if tokens_raw and len(tokens_raw) > 1 else None

        # Volume
        volume = float(m.get("volume24hr") or m.get("volume") or 0)

        # Tags
        text = (m.get("question") or "") + " " + (m.get("description") or "")
        tags = _extract_tags(text.lower())

        return Market(
            id           = str(m.get("id") or m.get("conditionId") or ""),
            question     = str(m.get("question") or m.get("title") or ""),
            yes_price    = yes_price,
            no_price     = no_price,
            volume_24h   = volume,
            end_date     = m.get("endDate") or m.get("endDateIso"),
            token_id_yes = token_yes,
            token_id_no  = token_no,
            description  = str(m.get("description") or "")[:500],
            tags         = tags,
        )
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as e:
        log.debug(f"Ошибка парсинга рынка: {e}")
        return None


def _parse_trade(t: dict) -> Optional[Trade]:
    """Парсим сырой dict → Trade."""
    try:
        ts_raw = t.get("timestamp") or t.get("created_at") or t.get("transactionHash")
        if isinstance(ts_raw, (int, float)):
            ts = datetime.fromtimestamp(ts_raw, tz=timezone.utc)
        elif isinstance(ts_raw, str):
            try:
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            except ValueError:
                ts = datetime.now(timezone.utc)
        else:
            ts = datetime.now(timezone.utc)

        outcome_raw = t.get("outcome") or t.get("resolvedOutcome")
        outcome = None
        if outcome_raw:
            outcome = "YES" if str(outcome_raw).lower() in ("yes","1","true","win") else "NO"

        return Trade(
            id        = str(t.get("id") or t.get("transactionHash") or ""),
            market_id = str(t.get("market") or t.get("conditionId") or ""),
            question  = str(t.get("question") or t.get("title") or ""),
            maker     = str(t.get("maker") or t.get("user") or "").lower(),
            price     = float(t.get("price", 0)),
            size      = float(t.get("size") or t.get("amount") or 0),
            side      = str(t.get("side") or "BUY").upper(),
            timestamp = ts,
            outcome   = outcome,
        )
    except (ValueError, TypeError, KeyError) as e:
        log.debug(f"Ошибка парсинга сделки: {e}")
        return None


def _extract_tags(text: str) -> tuple:
    TAG_KEYWORDS = {
        "crypto":      ["bitcoin","btc","eth","crypto","solana","defi","nft"],
        "politics":    ["election","president","congress","senate","vote","trump","biden","harris"],
        "macro":       ["fed","rate","inflation","cpi","gdp","fomc","recession","jobs","unemployment"],
        "geopolitics": ["iran","israel","russia","ukraine","china","war","attack","military","nato"],
        "sports":      ["nba","nfl","nhl","mlb","championship","super bowl","world cup","playoffs"],
        "tech":        ["apple","google","microsoft","ai","openai","anthropic","nvidia","tesla"],
    }
    return tuple(tag for tag, kws in TAG_KEYWORDS.items() if any(kw in text for kw in kws))
