"""
Polymarket Data Client v1.0

Единственный источник правды о реальном состоянии на Polymarket:
позиции, текущие цены, unrealised P&L.

КЛЮЧЕВЫЕ РЕШЕНИЯ:
- Один Session на весь процесс (без пересоздания при каждом запросе)
- TTL-кэш: позиции кэшируются 30 сек, сброс после каждой сделки
- Thread-safe через Lock
- Нет зависимости от приватного ключа — только public Data API
"""
from __future__ import annotations
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger("prometheus.polymarket")

_DATA_API = "https://data-api.polymarket.com"
_CLOB_API = "https://clob.polymarket.com"
_GAMMA    = "https://gamma-api.polymarket.com"

_POSITIONS_TTL = 30    # кэш позиций: 30 сек
_PRICES_TTL    = 10    # кэш цен токенов: 10 сек


# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class PolyPosition:
    """Открытая позиция с реальными данными от Polymarket."""
    token_id:        str
    market_id:       str
    question:        str
    outcome:         str          # "Yes" / "No"
    size:            float        # кол-во shares
    avg_price:       float        # средняя цена входа
    cur_price:       float        # текущая рыночная цена
    initial_value:   float        # вложено USD (size * avg_price)
    current_value:   float        # текущая стоимость USD (size * cur_price)
    unrealised_pnl:  float        # P&L в USD
    pnl_pct:         float        # P&L в %


@dataclass
class PortfolioSnapshot:
    """Срез состояния портфеля по данным Polymarket."""
    positions:        list[PolyPosition]
    positions_value:  float        # суммарная стоимость позиций
    total_invested:   float        # суммарно вложено
    unrealised_pnl:   float        # суммарный unrealised P&L
    fetched_at:       float = field(default_factory=time.time)

    @property
    def position_count(self) -> int:
        return len(self.positions)

    def by_token(self, token_id: str) -> Optional[PolyPosition]:
        return next((p for p in self.positions if p.token_id == token_id), None)

    def by_market(self, market_id: str) -> Optional[PolyPosition]:
        return next((p for p in self.positions if p.market_id == market_id), None)


# ── Client ────────────────────────────────────────────────────────────────────

class PolymarketClient:
    """
    Кэшированный клиент для чтения данных с Polymarket Data API.

    Инициализация:
        client = PolymarketClient(funder_address="0x...")

    Использование:
        snapshot = client.portfolio()
        pos = snapshot.by_token(token_id)
        price = client.token_price(token_id)
    """

    def __init__(self, funder_address: str = "") -> None:
        self.funder    = funder_address
        self._session  = self._make_session()
        self._lock     = threading.Lock()

        self._portfolio_cache: Optional[PortfolioSnapshot] = None
        self._price_cache:     dict[str, tuple[float, float]] = {}   # token_id → (price, ts)

    # ── Public API ─────────────────────────────────────────────────────────────

    def portfolio(self, force: bool = False) -> PortfolioSnapshot:
        """
        Актуальный срез портфеля с Polymarket.
        Кэшируется на _POSITIONS_TTL секунд; force=True сбрасывает кэш.
        """
        now = time.time()
        with self._lock:
            if (not force
                    and self._portfolio_cache is not None
                    and now - self._portfolio_cache.fetched_at < _POSITIONS_TTL):
                return self._portfolio_cache

        snap = self._fetch_portfolio()
        with self._lock:
            self._portfolio_cache = snap
        return snap

    def token_price(self, token_id: str, force: bool = False) -> Optional[float]:
        """
        Midpoint цена токена с CLOB.
        Кэшируется на _PRICES_TTL секунд.
        """
        now = time.time()
        with self._lock:
            cached = self._price_cache.get(token_id)
            if not force and cached and now - cached[1] < _PRICES_TTL:
                return cached[0]

        price = self._fetch_token_price(token_id)
        if price is not None:
            with self._lock:
                self._price_cache[token_id] = (price, time.time())
        return price

    def invalidate(self) -> None:
        """
        Сбросить кэш позиций и цен.
        Вызывать после каждой сделки чтобы следующий запрос получил свежие данные.
        """
        with self._lock:
            self._portfolio_cache = None
            self._price_cache.clear()
        log.debug("PolymarketClient: кэш сброшен")

    def effective_bankroll(
        self,
        initial_bankroll: float,
        realized_pnl:     float,
    ) -> float:
        """
        Оценочный текущий капитал:
            bankroll = initial + realized_pnl + unrealised_pnl

        Использовать для Kelly sizing вместо статичного cfg.bankroll.
        При недоступности API возвращает initial + realized_pnl.
        """
        snap = self.portfolio()
        return max(1.0, initial_bankroll + realized_pnl + snap.unrealised_pnl)

    # ── Compatibility: drop-in замена fetch_polymarket_positions ──────────────

    def positions_dict(self) -> dict[str, dict]:
        """
        Словарь {token_id: {...}} — совместимость с fetch_polymarket_positions().
        Используется в resolver.verify_position_closed() и main._process_pending_opens().
        """
        snap = self.portfolio()
        return {
            p.token_id: {
                "size":            p.size,
                "avg_price":       p.avg_price,
                "market_id":       p.market_id,
                "cur_price":       p.cur_price,
                "unrealised_pnl":  p.unrealised_pnl,
            }
            for p in snap.positions
        }

    # ── Private ────────────────────────────────────────────────────────────────

    def _fetch_portfolio(self) -> PortfolioSnapshot:
        if not self.funder:
            return PortfolioSnapshot([], 0.0, 0.0, 0.0)

        try:
            r = self._session.get(
                f"{_DATA_API}/positions",
                params={"user": self.funder, "sizeThreshold": "0.01"},
                timeout=10,
            )
            if r.status_code != 200:
                log.warning(f"Polymarket positions HTTP {r.status_code}")
                cached = self._portfolio_cache
                return cached if cached else PortfolioSnapshot([], 0.0, 0.0, 0.0)

            positions: list[PolyPosition] = []
            for item in r.json():
                try:
                    pos = PolyPosition(
                        token_id       = str(item.get("asset") or item.get("token_id", "")),
                        market_id      = str(item.get("conditionId", "")),
                        question       = str(item.get("title", "")),
                        outcome        = str(item.get("outcome", "")),
                        size           = float(item.get("size", 0)),
                        avg_price      = float(item.get("avgPrice", 0)),
                        cur_price      = float(item.get("curPrice", 0)),
                        initial_value  = float(item.get("initialValue", 0)),
                        current_value  = float(item.get("currentValue", 0)),
                        unrealised_pnl = float(item.get("cashPnl", 0)),
                        pnl_pct        = float(item.get("percentPnl", 0)),
                    )
                    if pos.token_id and pos.size > 0.01:
                        positions.append(pos)
                except (ValueError, TypeError, KeyError):
                    continue

            snap = PortfolioSnapshot(
                positions       = positions,
                positions_value = sum(p.current_value for p in positions),
                total_invested  = sum(p.initial_value  for p in positions),
                unrealised_pnl  = sum(p.unrealised_pnl for p in positions),
            )
            log.debug(
                f"Polymarket portfolio: {len(positions)} позиций | "
                f"стоимость ${snap.positions_value:.2f} | "
                f"uPnL ${snap.unrealised_pnl:+.2f}"
            )
            return snap

        except Exception as e:
            log.warning(f"_fetch_portfolio error: {e}")
            cached = self._portfolio_cache
            return cached if cached else PortfolioSnapshot([], 0.0, 0.0, 0.0)

    def _fetch_token_price(self, token_id: str) -> Optional[float]:
        if not token_id:
            return None
        try:
            r = self._session.get(
                f"{_CLOB_API}/midpoint",
                params={"token_id": token_id},
                timeout=5,
            )
            if r.status_code == 200:
                val = float(r.json().get("mid", 0))
                return val if val > 0 else None
        except Exception as e:
            log.debug(f"token_price {token_id[:12]}: {e}")
        return None

    @staticmethod
    def _make_session() -> requests.Session:
        s = requests.Session()
        s.headers["User-Agent"] = "Prometheus/3.0"
        retry = Retry(
            total=2,
            backoff_factor=1.0,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
        )
        s.mount("https://", HTTPAdapter(max_retries=retry))
        return s
