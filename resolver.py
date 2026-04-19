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
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
import requests

from data import fetch_current_price
from risk import Position
from config import cfg


@dataclass
class PendingRedeem:
    market_id:    str
    token_id:     str
    shares:       float
    question:     str
    pnl:          float
    slug:         str
    attempts:     int   = 0
    max_attempts: int   = 5          # всего 5 попыток (~25 минут)
    interval_sec: int   = 300        # повтор каждые 5 минут
    next_retry_at: float = field(default_factory=lambda: time.time() + 300)


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


def get_market_info(market_id: str) -> dict:
    """
    Получить полные данные рынка из Gamma API одним запросом.
    Возвращает сырой dict (или пустой при ошибке).

    Содержит: winner, outcomePrices, closed, active, endDate, endDateIso.
    Используется внутри resolver.run() чтобы сделать ОДИН запрос вместо двух
    (раньше вызывались get_yes_price + check_market_outcome раздельно).
    """
    try:
        r = requests.get(f"{GAMMA}/markets/{market_id}", timeout=8)
        if r.status_code == 200:
            return r.json()
        log.debug(f"get_market_info HTTP {r.status_code} for {market_id}")
    except Exception as e:
        log.debug(f"get_market_info error {market_id}: {e}")
    return {}


def check_market_outcome(
    market_id: str,
    current_price: float = None,
    market_data: dict = None,
) -> Optional[str]:
    """
    Определяет итог рынка (YES/NO).

    market_data — предзагруженный ответ Gamma API (из get_market_info).
    Если не передан — выполняется HTTP запрос.
    current_price — YES-цена как fallback при недоступности API.
    """
    if market_data is None:
        try:
            r = requests.get(f"{GAMMA}/markets/{market_id}", timeout=8)
            if r.status_code != 200:
                if current_price is not None:
                    if current_price >= 0.99:  return "YES"
                    if current_price <= 0.01:  return "NO"
                return None
            market_data = r.json()
        except Exception as e:
            log.debug(f"check_market_outcome error {market_id}: {e}")
            if current_price is not None:
                if current_price >= 0.99:  return "YES"
                if current_price <= 0.01:  return "NO"
            return None

    m = market_data

    # 1. Явный winner из API
    winner = m.get("winner") or m.get("resolvedOutcome") or m.get("resolution")
    if winner:
        w = str(winner).upper()
        if w in ("YES", "1", "TRUE"):  return "YES"
        if w in ("NO",  "0", "FALSE"): return "NO"

    # 2. Закрытый рынок — outcomePrices[0] = YES цена после резолюции
    if m.get("closed") or m.get("active") is False:
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


def get_yes_price(market_id: str) -> Optional[float]:
    """
    YES-цена рынка через Gamma API.
    Для разовых запросов. Внутри resolver.run() используй get_market_info().
    """
    info = get_market_info(market_id)
    return _yes_price_from_info(info)


def _yes_price_from_info(info: dict) -> Optional[float]:
    """Извлечь YES-цену из уже загруженного dict Gamma API."""
    try:
        prices = info.get("outcomePrices")
        if isinstance(prices, str):
            prices = json.loads(prices)
        if prices:
            return float(prices[0])
    except Exception:
        pass
    return None


def _end_date_from_info(info: dict) -> Optional[str]:
    """Извлечь дату закрытия рынка из dict Gamma API."""
    return info.get("endDate") or info.get("endDateIso")


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




def calc_position_token_entry_price(direction: str, entry_price: float) -> float:
    return entry_price if direction == "YES" else (1.0 - entry_price)


def calc_position_shares(pos_or_direction, entry_price: float | None = None, size_usd: float | None = None) -> float:
    if hasattr(pos_or_direction, "direction"):
        direction = pos_or_direction.direction
        entry = pos_or_direction.entry_price
        size = pos_or_direction.size_usd
    else:
        direction = str(pos_or_direction)
        entry = float(entry_price or 0)
        size = float(size_usd or 0)
    token_entry = calc_position_token_entry_price(direction, entry)
    return round(size / max(token_entry, 0.001), 6)

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

        import time as _time

        # Попытка 1: нативный redeem (если метод доступен в py_clob_client)
        if hasattr(client, "redeem_positions"):
            try:
                result = client.redeem_positions(token_id=token_id)
                log.info(f"Redeem OK (native): market={market_id[:12]} result={result}")
                return True
            except Exception as e1:
                log.debug(f"native redeem failed: {e1}")

        # Попытка 2: HTTP /redeem endpoint — повторяем 3 раза (API иногда запаздывает)
        headers = client.get_headers("POST", "/redeem")
        for attempt in range(3):
            try:
                resp = requests.post(
                    f"{CLOB}/redeem",
                    json={"token_id": token_id},
                    headers=headers,
                    timeout=15,
                )
                if resp.status_code in (200, 201):
                    log.info(f"Redeem OK (HTTP): market={market_id[:12]} | {resp.text[:80]}")
                    return True
                log.warning(f"Redeem HTTP {resp.status_code} attempt {attempt+1}: {resp.text[:100]}")
                if resp.status_code == 400:
                    break  # bad request — не повторяем
                _time.sleep(5 * (attempt + 1))
            except Exception as e2:
                log.debug(f"HTTP redeem attempt {attempt+1} failed: {e2}")
                _time.sleep(5)

        # Попытка 3: /redeem-positions endpoint (альтернативный путь)
        try:
            resp2 = requests.post(
                f"{CLOB}/redeem-positions",
                json={"tokenId": token_id, "amount": shares},
                headers=headers,
                timeout=15,
            )
            if resp2.status_code in (200, 201):
                log.info(f"Redeem OK (/redeem-positions): market={market_id[:12]}")
                return True
            log.debug(f"/redeem-positions {resp2.status_code}: {resp2.text[:80]}")
        except Exception as e3:
            log.debug(f"/redeem-positions failed: {e3}")

        # Попытка 4: SELL winning shares (токены ценой ~$1 всё ещё торгуемы)
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
    True только если токен точно не найден или его size <= 0.01.
    """
    if not token_id or not funder:
        return False

    import time as _time

    for attempt in range(retries):
        try:
            positions = fetch_polymarket_positions(funder)
            item = positions.get(token_id)
            if item is None or float(item.get("size", 0) or 0) <= 0.01:
                log.info("✅ verify: позиция подтверждена закрытой на Polymarket")
                return True

            size = float(item.get("size", 0) or 0)
            log.debug(
                f"verify: позиция ещё открыта size={size:.4f} "
                f"(попытка {attempt+1}/{retries})"
            )
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
        self._pending_redeems: list[PendingRedeem] = []

    def retry_pending_redeems(self) -> None:
        """Повторные попытки клейма выигрышей. Вызывать каждый цикл."""
        if not self._pending_redeems:
            return
        poly_key = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
        now = time.time()
        still_pending = []
        for pr in self._pending_redeems:
            if now < pr.next_retry_at:
                still_pending.append(pr)
                continue
            pr.attempts += 1
            log.info(
                f"♻️ Redeem retry {pr.attempts}/{pr.max_attempts}: "
                f"market={pr.market_id[:12]} pnl=+${pr.pnl:.2f}"
            )
            ok = redeem_winning_position(
                market_id = pr.market_id,
                token_id  = pr.token_id,
                shares    = pr.shares,
                poly_key  = poly_key,
            )
            if ok:
                log.info(f"✅ Redeem succeeded on retry {pr.attempts}: {pr.question[:60]}")
                if self.tg:
                    try:
                        self.tg.send(
                            f"✅ *Выигрыш успешно получен* (попытка {pr.attempts})\n\n"
                            f"*{pr.question[:70]}*\n"
                            f"Выигрыш: *+${pr.pnl:.2f}*"
                        )
                    except Exception:
                        pass
            elif pr.attempts >= pr.max_attempts:
                log.warning(f"⚠️ Redeem окончательно не удался после {pr.attempts} попыток: {pr.question[:60]}")
                self._send_manual_redeem_notice(pr)
            else:
                pr.next_retry_at = time.time() + pr.interval_sec
                still_pending.append(pr)
        self._pending_redeems = still_pending

    def _send_manual_redeem_notice(self, pr: PendingRedeem) -> None:
        """Уведомление о необходимости ручного клейма (только после исчерпания всех попыток)."""
        if not self.tg:
            return
        try:
            import urllib.parse as _up
            url = (f"https://polymarket.com/event/{pr.slug}"
                   if pr.slug
                   else f"https://polymarket.com/?s={_up.quote(pr.question[:80])}")
            msg = (
                f"⚠️ *Redeem не удался*\n\n"
                f"*{pr.question[:70]}*\n\n"
                f"Выигрыш: *+${pr.pnl:.2f}*\n"
                f"Зайди и клейм вручную:\n"
                f"🔗 [Открыть на Polymarket]({url})"
            )
            if hasattr(self.tg, "send"):
                self.tg.send(msg)
            else:
                self.tg(msg)
        except Exception:
            pass

    def _notify(self, pos, outcome: str, won: bool, pnl: float,
                exit_price: float, loss_pct: float = None) -> None:
        """Отправить уведомление о закрытии позиции."""
        if not self.tg:
            return
        try:
            import urllib.parse as _up
            pm_url = getattr(pos, 'url', '') or ''
            if not pm_url:
                q      = _up.quote((pos.question or '')[:120])
                pm_url = f"https://polymarket.com/?s={q}"

            self.tg.position_closed(
                question    = pos.question,
                direction   = pos.direction,
                entry_price = pos.entry_price,
                exit_price  = exit_price,
                size        = pos.size_usd,
                pnl         = pnl,
                outcome     = outcome,
                signal_type = pos.signal_type,
                opened_at   = getattr(pos, 'opened_at', None),
                url         = pm_url,
            )
        except Exception as e:
            log.debug(f"Telegram notify error: {e}")


    def retry_pending_closures(self) -> list[dict]:
        poly_key = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
        funder = os.environ.get("POLYMARKET_FUNDER", "")
        closed_now: list[dict] = []
        if not poly_key or not funder:
            return closed_now

        for action in list(self.risk.pending_close_actions):
            if not action.due(datetime.now(timezone.utc).timestamp()):
                continue

            pos = next((p for p in self.risk.open_positions if p.market_id == action.market_id), None)
            if not pos:
                self.risk.remove_pending_action(action.market_id, "close_retry")
                continue

            shares = calc_position_shares(pos)
            sold, actual_price = sell_position_on_polymarket(
                token_id=action.token_id or pos.token_id or "",
                shares=shares,
                poly_key=poly_key,
            )
            if sold:
                confirmed = verify_position_closed(
                    token_id=action.token_id or pos.token_id or "",
                    funder=funder,
                    retries=3,
                    wait_sec=10,
                )
                if confirmed:
                    current_price = actual_price or action.target_price or 0.0
                    entry_token = calc_position_token_entry_price(pos.direction, pos.entry_price)
                    exit_token = max(current_price, 0.0)
                    pnl = round((exit_token - entry_token) / max(entry_token, 0.01) * pos.size_usd, 2)
                    closed = self.risk.close_with_pnl(pos.market_id, exit_price=current_price, pnl=pnl)
                    if closed:
                        closed_now.append(self._make_closed_dict(pos, action.reason or "RETRY_CLOSE", pnl >= 0, pnl))
                        self._notify(pos, action.reason or "RETRY_CLOSE", pnl >= 0, pnl, current_price)
                    self.risk.remove_pending_action(action.market_id, "close_retry")
                    continue

            self.risk.mark_action_attempt(action, error="retry_close_not_confirmed", due_in=action.interval_sec)
            if action.attempts >= action.max_attempts:
                self.risk.remove_pending_action(action.market_id, "close_retry")
                self.risk.pause()
                if self.tg:
                    self.tg.error(
                        f"Не удалось закрыть позицию после {action.max_attempts} попыток: {action.question[:100]}"
                    )
        self._signal_outcomes.extend(closed_now)
        return closed_now

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
            # Цена НАШЕГО токена — для stop-loss и time-based exit
            cur_token_price = get_current_price(pos.token_id) if pos.token_id else None

            # ОДИН запрос к Gamma — содержит всё: outcome, yes_price, end_date
            market_info = get_market_info(pos.market_id)
            yes_price   = _yes_price_from_info(market_info)
            end_date    = _end_date_from_info(market_info)

            # 1. Проверка резолюции рынка (без повторного HTTP)
            outcome = check_market_outcome(pos.market_id, current_price=yes_price,
                                           market_data=market_info)

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
                        shares = calc_position_shares(pos)
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
                                f"⚠️ Redeem не удался — ставим в очередь повторных попыток: "
                                f"{pos.market_id[:16]}"
                            )
                            # Не уведомляем сразу — добавляем в очередь повторов
                            self._pending_redeems.append(PendingRedeem(
                                market_id    = pos.market_id,
                                token_id     = pos.token_id,
                                shares       = shares,
                                question     = pos.question or "",
                                pnl          = closed.pnl,
                                slug         = getattr(pos, "slug", "") or "",
                                next_retry_at = time.time() + 300,  # первый повтор через 5 мин
                            ))
                continue

            # 2. Time-based exit: рынок закрывается менее чем через N часов
            #    и мы уже в минусе на cfg.time_exit_min_loss_pct+
            if cur_token_price is not None and end_date:
                try:
                    end_dt     = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                    hours_left = (end_dt - datetime.now(timezone.utc)).total_seconds() / 3600
                    entry_tok  = pos.entry_price if pos.direction == "YES" else 1.0 - pos.entry_price
                    cur_tok    = cur_token_price if pos.direction == "YES" else 1.0 - cur_token_price
                    time_loss  = (entry_tok - cur_tok) / entry_tok if entry_tok > 0 else 0

                    if 0 < hours_left < cfg.time_exit_hours and time_loss >= cfg.time_exit_min_loss_pct:
                        pnl = round((cur_tok - entry_tok) / max(entry_tok, 0.01) * pos.size_usd, 2)
                        log.info(
                            f"⏰ TIME EXIT: {hours_left:.1f}h left, "
                            f"down {time_loss:.0%} | {pos.question[:50]}"
                        )
                        shares = calc_position_shares(pos)
                        sold, actual_price = False, cur_token_price
                        if pos.token_id and poly_key:
                            sold, actual_price = sell_position_on_polymarket(
                                token_id=pos.token_id, shares=shares, poly_key=poly_key,
                            )
                            if not sold:
                                log.warning(f"⚠️ TIME EXIT sell failed: {pos.question[:40]}")
                                continue
                            confirmed = verify_position_closed(
                                token_id=pos.token_id,
                                funder=os.environ.get("POLYMARKET_FUNDER", ""),
                                retries=3, wait_sec=10,
                            )
                            if not confirmed:
                                log.warning(f"⚠️ TIME EXIT: sell sent but position still visible")
                                continue
                        closed = self.risk.close_with_pnl(
                            pos.market_id, exit_price=actual_price, pnl=pnl,
                        )
                        if closed:
                            closed_now.append(self._make_closed_dict(pos, "TIME_EXIT", False, pnl))
                            self._notify(pos, "TIME_EXIT", False, pnl, actual_price, time_loss)
                        continue
                except Exception as e:
                    log.debug(f"time_exit parse error: {e}")

            # 3. Stop-loss — потеряли cfg.stop_loss_pct+ от входной цены нашего токена
            if cur_token_price is not None and cur_token_price > 0:
                entry_token   = pos.entry_price if pos.direction == "YES" else 1.0 - pos.entry_price
                current_token = cur_token_price if pos.direction == "YES" else 1.0 - cur_token_price
                loss_pct = (entry_token - current_token) / entry_token if entry_token > 0 else 0

                if loss_pct >= cfg.stop_loss_pct:
                    pnl = round((current_token - entry_token) / max(entry_token, 0.01) * pos.size_usd, 2)
                    
                    # Реально продаём токены на Polymarket
                    shares = calc_position_shares(pos)
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
