"""
Prometheus v3.3 — Position Resolver

FIXES v3.3:
- check_market_outcome теперь определяет исход через Gamma API (outcomePrices[0])
  а не по цене токена позиции — устраняет путаницу YES/NO сторон.
- get_yes_price() — отдельная функция для получения YES-цены рынка через Gamma.
- Добавлена redeem_winning_position() — автоматический клейм выигрыша через CLOB.
- run() вызывает redeem автоматически после каждой победной позиции.
- При неудачном redeem — Telegram уведомление с ссылкой для ручного клейма.
"""
from __future__ import annotations
import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional
import requests

from data import fetch_current_price


def sell_position_on_polymarket(token_id: str, shares: float,
                                poly_key: str = None) -> tuple[bool, float]:
    """
    Продать токены на Polymarket по рыночной цене (SELL FAK ордер).
    Используется для stop-loss, take-profit и закрытия позиций.
    
    Returns:
        (success, actual_price)
    """
    import os as _os
    _key = poly_key or _os.environ.get("POLYMARKET_PRIVATE_KEY", "")
    if not _key or not token_id or shares <= 0:
        log.warning(f"sell: невозможно продать — key={bool(_key)} token={bool(token_id)} shares={shares}")
        return False, 0.0
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import MarketOrderArgs, OrderType, ApiCreds
        from py_clob_client.order_builder.constants import SELL
        import time as _time

        sig_type = int(_os.environ.get("POLYMARKET_SIGNATURE_TYPE", "0"))
        clob_kwargs = dict(key=_key, chain_id=137, signature_type=sig_type)
        funder = _os.environ.get("POLYMARKET_FUNDER", "")
        if funder and funder.startswith("0x"):
            clob_kwargs["funder"] = funder

        client = ClobClient("https://clob.polymarket.com", **clob_kwargs)
        raw_creds = client.create_or_derive_api_creds()
        if isinstance(raw_creds, dict):
            raw_creds = ApiCreds(
                api_key        = raw_creds["api_key"],
                api_secret     = raw_creds["api_secret"],
                api_passphrase = raw_creds["api_passphrase"],
            )
        client.set_api_creds(raw_creds)
        _time.sleep(1)

        # Текущая цена
        current_price = 0.0
        try:
            r = requests.get(f"{CLOB}/midpoint", params={"token_id": token_id}, timeout=5)
            if r.status_code == 200:
                current_price = float(r.json().get("mid", 0))
        except Exception:
            pass

        sell_order = MarketOrderArgs(
            token_id   = token_id,
            amount     = shares,
            side       = SELL,
            order_type = OrderType.FAK,
        )
        signed = client.create_market_order(sell_order)
        result = client.post_order(signed, OrderType.FAK)
        log.info(f"✅ SELL OK: token={token_id[:12]} shares≈{shares:.1f} @ {current_price:.3f} | {result}")
        return True, current_price

    except Exception as e:
        log.error(f"SELL failed token={token_id[:12]}: {e}")
        return False, 0.0

log = logging.getLogger("prometheus.resolver")

GAMMA = "https://gamma-api.polymarket.com"
CLOB  = "https://clob.polymarket.com"


def check_market_outcome(market_id: str, current_price: float = None) -> Optional[str]:
    """
    Определяет итог рынка (YES/NO) через Gamma API.

    current_price здесь — YES-токен цена (не наш токен).
    Используется только как fallback если Gamma недоступна.
    """
    try:
        r = requests.get(f"{GAMMA}/markets/{market_id}", timeout=8)
        if r.status_code != 200:
            if current_price is not None:
                if current_price >= 0.99:  return "YES"
                if current_price <= 0.01:  return "NO"
            return None

        m = r.json()

        # 1. Явный winner из API
        winner = m.get("winner") or m.get("resolvedOutcome") or m.get("resolution")
        if winner:
            w = str(winner).upper()
            if w in ("YES", "1", "TRUE"):  return "YES"
            if w in ("NO",  "0", "FALSE"): return "NO"

        # 2. Закрытый рынок — outcomePrices[0] = YES цена после резолюции
        if m.get("closed") or m.get("active") == False:
            prices = m.get("outcomePrices")
            if isinstance(prices, str):
                try:
                    prices = json.loads(prices)
                except Exception:
                    prices = None
            if prices and len(prices) >= 2:
                yes_final = float(prices[0])
                no_final  = float(prices[1])
                if yes_final >= 0.97: return "YES"
                if no_final  >= 0.97: return "NO"
                if yes_final > no_final and yes_final >= 0.90: return "YES"
                if no_final > yes_final and no_final  >= 0.90: return "NO"

        return None

    except Exception as e:
        log.debug(f"check_market_outcome error {market_id}: {e}")
        if current_price is not None:
            if current_price >= 0.99:  return "YES"
            if current_price <= 0.01:  return "NO"
        return None


def get_yes_price(market_id: str) -> Optional[float]:
    """
    YES-цена рынка через Gamma API.
    Используется для определения исхода — НЕ для P&L нашей позиции.
    """
    try:
        r = requests.get(f"{GAMMA}/markets/{market_id}", timeout=5)
        if r.status_code == 200:
            m = r.json()
            prices = m.get("outcomePrices")
            if isinstance(prices, str):
                prices = json.loads(prices)
            if prices:
                return float(prices[0])
    except Exception:
        pass
    return None


def get_current_price(token_id: str) -> Optional[float]:
    """
    Midpoint цена конкретного токена с CLOB.
    Это цена НАШЕГО токена (YES или NO по direction позиции).
    """
    try:
        r = requests.get(f"{CLOB}/midpoint", params={"token_id": token_id}, timeout=5)
        if r.status_code == 200:
            return float(r.json().get("mid", 0))
    except Exception:
        pass
    return None


def redeem_winning_position(market_id: str, token_id: str,
                             shares: float, poly_key: str = None) -> bool:
    """
    Клейм выигрышных shares — USDC возвращается на баланс кошелька.

    Три попытки:
    1. Нативный client.redeem_positions() если есть в py_clob_client
    2. HTTP POST /redeem
    3. SELL winning shares по рыночной цене (fallback)
    """
    _key = poly_key or os.environ.get("POLYMARKET_PRIVATE_KEY", "")
    if not _key:
        log.warning("redeem: POLYMARKET_PRIVATE_KEY не задан — пропускаем")
        return False

    if not token_id:
        log.warning(f"redeem: нет token_id для market {market_id}")
        return False

    try:
        from py_clob_client.client import ClobClient

        sig_type = int(os.environ.get("POLYMARKET_SIGNATURE_TYPE", "0"))
        clob_kwargs = dict(key=_key, chain_id=137, signature_type=sig_type)

        funder = os.environ.get("POLYMARKET_FUNDER", "")
        if funder and funder.startswith("0x"):
            clob_kwargs["funder"] = funder

        client = ClobClient("https://clob.polymarket.com", **clob_kwargs)

        raw_creds = client.create_or_derive_api_creds()
        if isinstance(raw_creds, dict):
            from py_clob_client.clob_types import ApiCreds
            raw_creds = ApiCreds(
                api_key        = raw_creds["api_key"],
                api_secret     = raw_creds["api_secret"],
                api_passphrase = raw_creds["api_passphrase"],
            )
        client.set_api_creds(raw_creds)

        # Попытка 1: нативный redeem
        if hasattr(client, "redeem_positions"):
            result = client.redeem_positions(token_id=token_id)
            log.info(f"Redeem OK (native): market={market_id[:12]} result={result}")
            return True

        # Попытка 2: HTTP redeem endpoint
        headers = client.get_headers("POST", "/redeem")
        resp = requests.post(
            f"{CLOB}/redeem",
            json={"token_id": token_id},
            headers=headers,
            timeout=10,
        )
        if resp.status_code in (200, 201):
            log.info(f"Redeem OK (HTTP): market={market_id[:12]} | {resp.text[:80]}")
            return True
        else:
            log.warning(f"Redeem HTTP {resp.status_code}: {resp.text[:120]}")

        # Попытка 3: SELL winning shares по рыночной цене
        try:
            from py_clob_client.clob_types import MarketOrderArgs, OrderType
            from py_clob_client.order_builder.constants import SELL
            sell_order = MarketOrderArgs(
                token_id   = token_id,
                amount     = shares,
                side       = SELL,
                order_type = OrderType.FAK,
            )
            signed = client.create_market_order(sell_order)
            result = client.post_order(signed, OrderType.FAK)
            log.info(f"Redeem via SELL OK: market={market_id[:12]} | {result}")
            return True
        except Exception as sell_err:
            log.debug(f"Redeem SELL fallback failed: {sell_err}")

        return False

    except ImportError:
        log.error("redeem: py-clob-client не установлен")
        return False
    except Exception as e:
        log.error(f"redeem error market={market_id}: {e}", exc_info=True)
        return False


def fetch_polymarket_positions(funder_address: str) -> dict[str, dict]:
    """
    Получить реальные открытые позиции с Polymarket по адресу кошелька.
    Возвращает словарь {token_id: {size, price, market_id}}
    """
    if not funder_address:
        return {}
    try:
        r = requests.get(
            "https://data-api.polymarket.com/positions",
            params={"user": funder_address, "sizeThreshold": "0.01"},
            timeout=10,
        )
        if r.status_code != 200:
            return {}
        positions = {}
        for item in r.json():
            token_id = item.get("asset") or item.get("token_id", "")
            if token_id:
                positions[token_id] = {
                    "size":      float(item.get("size", 0)),
                    "avg_price": float(item.get("avgPrice", 0)),
                    "market_id": item.get("conditionId", ""),
                    "cur_price": float(item.get("curPrice", 0)),
                }
        return positions
    except Exception as e:
        log.debug(f"fetch_polymarket_positions error: {e}")
        return {}


def verify_position_closed(token_id: str, funder: str,
                            retries: int = 3, wait_sec: int = 10) -> bool:
    """
    Проверить что позиция реально закрылась на Polymarket.
    Опрашивает Data API несколько раз с паузой.
    
    Returns:
        True — позиция закрыта (balance = 0)
        False — позиция ещё открыта
    """
    if not token_id or not funder:
        return False
    
    import time as _time
    
    for attempt in range(retries):
        try:
            r = requests.get(
                "https://data-api.polymarket.com/positions",
                params={"user": funder, "sizeThreshold": "0.01"},
                timeout=8,
            )
            if r.status_code == 200:
                positions = r.json()
                # Ищем наш токен — если его нет или size=0 → закрыто
                for item in positions:
                    if item.get("asset") == token_id or item.get("token_id") == token_id:
                        size = float(item.get("size", 0))
                        if size > 0.01:
                            log.debug(f"verify: позиция ещё открыта size={size:.2f} (попытка {attempt+1}/{retries})")
                            if attempt < retries - 1:
                                _time.sleep(wait_sec)
                            continue
                # Токен не найден → закрыто
                log.info(f"✅ verify: позиция подтверждена закрытой на Polymarket")
                return True
        except Exception as e:
            log.debug(f"verify error: {e}")
        
        if attempt < retries - 1:
            _time.sleep(wait_sec)
    
    log.warning(f"⚠️ verify: позиция НЕ закрыта после {retries} проверок")
    return False


class PositionResolver:
    def __init__(self, risk_manager, telegram=None):
        self.risk = risk_manager
        self.tg   = telegram
        self._signal_outcomes: list[dict] = []

    def _notify(self, pos, outcome: str, won: bool, pnl: float,
                exit_price: float, loss_pct: float = None) -> None:
        """Отправить уведомление о закрытии позиции."""
        if not self.tg:
            return
        try:
            import urllib.parse as _up
            slug = getattr(pos, 'slug', None)
            if slug:
                pm_url = f"https://polymarket.com/event/{slug}"
            else:
                q      = _up.quote((pos.question or '')[:100])
                pm_url = f"https://polymarket.com/?s={q}"

            if hasattr(self.tg, "position_closed"):
                self.tg.position_closed(
                    question    = pos.question,
                    direction   = pos.direction,
                    entry_price = pos.entry_price,
                    exit_price  = exit_price,
                    size        = pos.size_usd,
                    pnl         = pnl,
                    outcome     = outcome,
                    signal_type = pos.signal_type,
                    url         = pm_url,
                )
            else:
                icon   = "✅" if won else ("🛑" if outcome == "STOP_LOSS" else "❌")
                result = "WIN" if won else ("STOP-LOSS" if outcome == "STOP_LOSS" else "LOSS")
                pnl_s  = f"+${pnl:.2f}" if won else f"−${abs(pnl):.2f}"
                msg = (
                    f"{icon} *{result}*\n\n"
                    f"*{pos.question[:70]}*\n\n"
                    f"Ставка: *{pos.direction}*  |  Исход: *{outcome}*\n"
                    f"P&L: *{pnl_s}*\n\n"
                    f"🔗 [Polymarket]({pm_url})"
                )
                if loss_pct is not None:
                    msg += f"\nПотери: *{loss_pct:.0%}*"
                self.tg(msg)
        except Exception as e:
            log.debug(f"Telegram notify error: {e}")

    def run(self) -> list[dict]:
        open_pos = self.risk.open_positions
        if not open_pos:
            return []

        poly_key   = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
        funder     = os.environ.get("POLYMARKET_FUNDER", "")
        closed_now = []

        # Синхронизация с реальными позициями на Polymarket
        real_positions = {}
        if funder:
            real_positions = fetch_polymarket_positions(funder)
            if real_positions:
                log.info(f"📡 Polymarket: {len(real_positions)} реальных позиций")

        for pos in open_pos:
            # Цена НАШЕГО токена — для stop-loss
            cur_token_price = get_current_price(pos.token_id) if pos.token_id else None

            # YES-цена рынка — для определения исхода
            yes_price = get_yes_price(pos.market_id)

            # 1. Проверка резолюции рынка
            outcome = check_market_outcome(pos.market_id, current_price=yes_price)

            if outcome is not None:
                won = (
                    (pos.direction == "YES" and outcome == "YES") or
                    (pos.direction == "NO"  and outcome == "NO")
                )
                exit_p = 1.0 if won else 0.0
                closed = self.risk.close(pos.market_id, exit_price=exit_p, won=won)

                if closed:
                    closed_now.append(self._make_closed_dict(pos, outcome, won, closed.pnl))
                    self._notify(pos, outcome, won, closed.pnl, exit_p)
                    log.info(
                        f"{'WIN ✅' if won else 'LOSS ❌'} | "
                        f"direction={pos.direction} outcome={outcome} | "
                        f"{pos.question[:50]} | P&L ${closed.pnl:+.2f}"
                    )

                    # ── REDEEM: забираем выигрыш на баланс ─────────────────
                    if won and poly_key and pos.token_id:
                        shares = pos.size_usd / max(pos.entry_price, 0.001)
                        log.info(
                            f"💰 Redeem: market={pos.market_id[:12]} "
                            f"token={pos.token_id[:12]} shares≈{shares:.1f}"
                        )
                        ok = redeem_winning_position(
                            market_id = pos.market_id,
                            token_id  = pos.token_id,
                            shares    = shares,
                            poly_key  = poly_key,
                        )
                        if not ok:
                            log.warning(
                                f"⚠️ Redeem не удался для {pos.market_id[:16]} — "
                                f"нужно клеймить вручную на Polymarket"
                            )
                            if self.tg:
                                try:
                                    import urllib.parse as _up
                                    slug = getattr(pos, 'slug', None)
                                    url  = (f"https://polymarket.com/event/{slug}"
                                            if slug
                                            else f"https://polymarket.com/?s={_up.quote((pos.question or '')[:80])}")
                                    msg = (
                                        f"⚠️ *Redeem не удался*\n\n"
                                        f"*{pos.question[:70]}*\n\n"
                                        f"Выигрыш: *+${closed.pnl:.2f}*\n"
                                        f"Зайди и клейм вручную:\n"
                                        f"🔗 [Открыть на Polymarket]({url})"
                                    )
                                    if hasattr(self.tg, "send"):
                                        self.tg.send(msg)
                                    else:
                                        self.tg(msg)
                                except Exception:
                                    pass
                continue

            # 2. Stop-loss — потеряли 40%+ от входной цены нашего токена
            if cur_token_price is not None and cur_token_price > 0:
                entry_token   = pos.entry_price if pos.direction == "YES" else 1.0 - pos.entry_price
                current_token = cur_token_price if pos.direction == "YES" else 1.0 - cur_token_price
                loss_pct = (entry_token - current_token) / entry_token if entry_token > 0 else 0

                if loss_pct >= 0.40:
                    pnl = round((current_token - entry_token) / max(entry_token, 0.01) * pos.size_usd, 2)
                    
                    # Реально продаём токены на Polymarket
                    shares = pos.size_usd / max(pos.entry_price, 0.001)
                    sold, actual_price = False, cur_token_price
                    if pos.token_id and poly_key:
                        sold, actual_price = sell_position_on_polymarket(
                            token_id = pos.token_id,
                            shares   = shares,
                            poly_key = poly_key,
                        )
                        if not sold:
                            log.warning(f"⚠️ STOP-LOSS sell failed — позиция НЕ закрыта на Polymarket: {pos.question[:40]}")
                            continue  # Не закрываем в базе если продажа не прошла
                        
                        # Проверяем что позиция реально закрылась
                        confirmed = verify_position_closed(
                            token_id = pos.token_id,
                            funder   = os.environ.get("POLYMARKET_FUNDER", ""),
                            retries  = 3,
                            wait_sec = 10,
                        )
                        if not confirmed:
                            log.warning(f"⚠️ STOP-LOSS: продажа отправлена но позиция ещё видна — пропускаем: {pos.question[:40]}")
                            continue
                    
                    closed = self.risk.close_with_pnl(pos.market_id, exit_price=actual_price, pnl=pnl)
                    if closed:
                        closed_now.append(self._make_closed_dict(pos, "STOP_LOSS", False, pnl))
                        self._notify(pos, "STOP_LOSS", False, pnl, actual_price, loss_pct)
                        log.info(f"🛑 STOP-LOSS | {pos.question[:50]} | -{loss_pct:.0%} | P&L ${pnl:+.2f}")

        self._signal_outcomes.extend(closed_now)
        return closed_now

    def _make_closed_dict(self, pos, outcome, won, pnl) -> dict:
        return {
            "market_id":   pos.market_id,
            "question":    pos.question,
            "direction":   pos.direction,
            "entry_price": pos.entry_price,
            "outcome":     outcome,
            "won":         won,
            "pnl":         pnl,
            "size_usd":    pos.size_usd,
            "signal_type": pos.signal_type,
            "tags":        pos.tags,
        }

    def signal_performance(self) -> dict:
        perf: dict = {}
        for rec in self._signal_outcomes:
            stype = rec.get("signal_type", "ai")
            if stype not in perf:
                perf[stype] = {"wins": 0, "total": 0, "pnl": 0.0}
            perf[stype]["total"] += 1
            if rec["won"]:
                perf[stype]["wins"] += 1
            perf[stype]["pnl"] += rec.get("pnl", 0)
        return {
            k: {**v,
                "win_rate": round(v["wins"] / v["total"], 3) if v["total"] else 0,
                "roi":      round(v["pnl"] / max(1, v["total"]) / 10, 3),
               }
            for k, v in perf.items()
        }

    def unrealised_pnl(self) -> dict:
        result = {}
        for pos in self.risk.open_positions:
            if not pos.token_id:
                result[pos.market_id] = 0.0
                continue
            current = get_current_price(pos.token_id)
            if current is None:
                result[pos.market_id] = 0.0
                continue
            entry = pos.entry_price if pos.direction == "YES" else 1 - pos.entry_price
            cur   = current         if pos.direction == "YES" else 1 - current
            pnl   = (cur - entry) * pos.size_usd / max(entry, 0.01)
            result[pos.market_id] = round(pnl, 2)
        return result
