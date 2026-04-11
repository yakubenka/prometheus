"""
Prometheus — Position Review Engine v1.0
Динамическое управление открытыми позициями.

ЛОГИКА:
Каждые N минут для каждой открытой позиции проверяем:

1. ТРИГГЕРЫ (без AI — быстро):
   - Цена выросла на 50%+ от входа → кандидат на фиксацию
   - Осталось < 48 часов до закрытия → кандидат на проверку
   - Новости по теме за последние 2 часа → немедленная переоценка

2. AI ПЕРЕОЦЕНКА (только если триггер сработал):
   Claude получает:
   - Текущую цену vs цену входа
   - Оригинальный тезис
   - Свежие новости
   - Математику: сколько EV осталось vs гарантированный выход

   Claude отвечает: HOLD / TAKE_PROFIT / CUT_LOSS + reasoning

3. РЕШЕНИЕ на основе Expected Value:
   EV_hold  = new_prob * remaining_payout - (1 - new_prob)
   EV_close = текущая цена как гарантированный P&L

   Закрываем только если:
   - TAKE_PROFIT: зафиксировали 65%+ потенциала И EV_close > EV_hold
   - CUT_LOSS: тезис сломан новостями (не просто цена упала)

ПРИНЦИП: не торговать против себя из-за шума.
Prediction markets часто ходят туда-сюда до резолюции.
Закрываем только при реальном изменении картины.
"""
from __future__ import annotations
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional, TYPE_CHECKING

import requests

if TYPE_CHECKING:
    from risk import RiskManager, Position
    from intel import IntelPipeline
    from anthropic import Anthropic

log = logging.getLogger("prometheus.position_review")

CLOB  = "https://clob.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"

_S = requests.Session()
_S.headers["User-Agent"] = "Prometheus/3.0"

_MODEL = "claude-sonnet-4-20250514"


# ── Dataclasses ────────────────────────────────────────────────────────────────

@dataclass
class ReviewDecision:
    action:      str       # HOLD / TAKE_PROFIT / CUT_LOSS
    reasoning:   str
    new_prob:    float     # AI оценка текущей вероятности
    ev_hold:     float     # Expected Value если держим
    ev_close:    float     # Гарантированный выход (текущая цена)
    trigger:     str       # что вызвало переоценку
    current_price: float
    unrealised_pnl: float


# ── Price fetcher ──────────────────────────────────────────────────────────────

def _get_current_price(token_id: str) -> Optional[float]:
    """Текущая mid-цена токена."""
    if not token_id:
        return None
    try:
        r = _S.get(f"{CLOB}/midpoint", params={"token_id": token_id}, timeout=5)
        if r.status_code == 200:
            val = float(r.json().get("mid", 0))
            return val if val > 0 else None
    except Exception:
        pass
    return None


# ── Expected Value ─────────────────────────────────────────────────────────────

def _calc_ev(prob: float, current_price: float, direction: str) -> float:
    """
    Expected Value позиции если держим.
    EV = prob * payout - (1 - prob)
    где payout = (1/price) - 1
    """
    price = current_price if direction == "YES" else 1.0 - current_price
    price = max(price, 0.01)
    payout = (1.0 / price) - 1.0
    return round(prob * payout - (1.0 - prob), 4)


def _calc_unrealised_pnl(pos, current_price: float) -> float:
    """Unrealised P&L по текущей цене."""
    entry  = pos.entry_price if pos.direction == "YES" else 1.0 - pos.entry_price
    cur    = current_price   if pos.direction == "YES" else 1.0 - current_price
    if entry <= 0:
        return 0.0
    return round((cur - entry) / entry * pos.size_usd, 2)


def _profit_captured_pct(pos, current_price: float) -> float:
    """
    Сколько процентов от максимального потенциала уже зафиксировано.
    Максимальный потенциал = вход @ entry_price, выход @ 1.0
    Текущий потенциал = вход @ entry_price, выход @ current_price
    """
    entry = pos.entry_price if pos.direction == "YES" else 1.0 - pos.entry_price
    cur   = current_price   if pos.direction == "YES" else 1.0 - current_price
    max_payout = (1.0 / max(entry, 0.01)) - 1.0
    cur_payout = (cur - entry) / max(entry, 0.01)
    if max_payout <= 0:
        return 0.0
    return max(0.0, cur_payout / max_payout)


# ── Trigger detection ──────────────────────────────────────────────────────────

def _check_triggers(pos, current_price: float,
                    intel=None) -> Optional[str]:
    """
    Проверяем нужна ли переоценка.
    Возвращает описание триггера или None.
    """
    if current_price is None or current_price <= 0:
        return None

    entry = pos.entry_price if pos.direction == "YES" else 1.0 - pos.entry_price
    cur   = current_price   if pos.direction == "YES" else 1.0 - current_price

    # Триггер 1: большое движение цены
    if entry > 0:
        price_change = (cur - entry) / entry
        if price_change >= 0.50:
            return f"price_up_{price_change:.0%}"
        if price_change <= -0.30:
            return f"price_down_{abs(price_change):.0%}"

    # Триггер 2: скоро закрытие
    if pos.opened_at:
        try:
            # Проверяем через Gamma API время закрытия
            r = _S.get(f"{GAMMA}/markets/{pos.market_id}", timeout=5)
            if r.status_code == 200:
                m = r.json()
                end_date = m.get("endDate") or m.get("endDateIso")
                if end_date:
                    end = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                    hours_left = (end - datetime.now(timezone.utc)).total_seconds() / 3600
                    if 0 < hours_left < 48:
                        return f"closing_soon_{hours_left:.0f}h"
        except Exception:
            pass

    # Триггер 3: релевантные новости за последние 2 часа
    if intel:
        try:
            recent = intel.db.recent(hours=2, limit=20)
            q_words = set(pos.question.lower().split())
            for dp in recent:
                text_words = set((dp.title + " " + dp.content).lower().split())
                overlap = len(q_words & text_words) / max(len(q_words), 1)
                if overlap > 0.25 and dp.relevance > 0.6:
                    return f"news_{dp.source}"
        except Exception:
            pass

    return None


# ── AI Review ─────────────────────────────────────────────────────────────────

def _ai_review(pos, current_price: float, trigger: str,
               intel=None, ai=None) -> ReviewDecision:
    """
    Спрашиваем Claude: держать или закрыть?
    Возвращает ReviewDecision с action и reasoning.
    """
    if ai is None:
        return ReviewDecision("HOLD", "no AI client", 0.5, 0.0, 0.0, trigger,
                              current_price, 0.0)

    # Собираем контекст
    entry         = pos.entry_price if pos.direction == "YES" else 1.0 - pos.entry_price
    cur           = current_price   if pos.direction == "YES" else 1.0 - current_price
    upnl          = _calc_unrealised_pnl(pos, current_price)
    captured_pct  = _profit_captured_pct(pos, current_price)

    # Время в позиции
    try:
        opened = datetime.fromisoformat(pos.opened_at.replace("Z", "+00:00"))
        hours_held = (datetime.now(timezone.utc) - opened).total_seconds() / 3600
    except Exception:
        hours_held = 0

    # Свежие новости по теме
    news_context = ""
    if intel:
        try:
            news = intel.context_for(pos.question, hours=6)
            if news and "No recent" not in news:
                news_context = f"\nRecent news:\n{news[:600]}"
        except Exception:
            pass

    prompt = f"""You are reviewing an open prediction market position.

POSITION:
Market: {pos.question}
Direction: {pos.direction}
Entry price: {pos.entry_price:.3f} ({pos.entry_price:.1%})
Current price: {current_price:.3f} ({current_price:.1%})
Size: ${pos.size_usd:.2f}
Unrealised P&L: ${upnl:+.2f}
Profit captured: {captured_pct:.0%} of maximum potential
Hours held: {hours_held:.1f}h
Review trigger: {trigger}
{news_context}

MATH:
- If we close NOW: lock in ${upnl:+.2f} (guaranteed)
- If we hold to resolution: we get full payout if correct, lose full size if wrong

TASK: Should we HOLD, TAKE_PROFIT, or CUT_LOSS?

Rules:
- TAKE_PROFIT only if: captured 65%+ of potential AND your new probability estimate is LOWER than current price (market overshot)
- CUT_LOSS only if: new information FUNDAMENTALLY changed the thesis (not just price movement)  
- HOLD in all other cases - prediction markets are noisy, don't overtrade

Return ONLY JSON:
{{"action":"HOLD|TAKE_PROFIT|CUT_LOSS","new_probability":0.0,"reasoning":"one sentence max 100 chars"}}"""

    try:
        from anthropic import Anthropic as _Anthropic
        resp = ai.messages.create(
            model=_MODEL,
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}],
        )
        raw  = resp.content[0].text.strip().replace("```json","").replace("```","").strip()
        data = json.loads(raw)

        action   = data.get("action", "HOLD").upper()
        new_prob = float(data.get("new_probability", 0.5))
        reason   = data.get("reasoning", "")

        if action not in ("HOLD", "TAKE_PROFIT", "CUT_LOSS"):
            action = "HOLD"

        # Математическая проверка решения
        ev_hold  = _calc_ev(new_prob, current_price, pos.direction)
        ev_close = upnl / max(pos.size_usd, 1.0)  # нормализованный guaranteed exit

        # Переопределяем если математика не согласна с AI
        if action == "TAKE_PROFIT":
            if captured_pct < 0.50:
                # Слишком рано фиксировать — меньше 50% потенциала
                action  = "HOLD"
                reason  = f"Overridden: only {captured_pct:.0%} captured, too early"
            elif ev_hold > ev_close + 0.05:
                # EV держать значительно выше EV закрыть
                action = "HOLD"
                reason = f"Overridden: EV_hold={ev_hold:.2f} > EV_close={ev_close:.2f}"

        if action == "CUT_LOSS":
            if "news" not in trigger and "price_down" not in trigger:
                # Не закрываем просто так
                action = "HOLD"
                reason = "Overridden: no news trigger, holding"

        return ReviewDecision(
            action        = action,
            reasoning     = reason,
            new_prob      = new_prob,
            ev_hold       = ev_hold,
            ev_close      = ev_close,
            trigger       = trigger,
            current_price = current_price,
            unrealised_pnl = upnl,
        )

    except Exception as e:
        log.debug(f"AI review error: {e}")
        return ReviewDecision("HOLD", f"AI error: {e}", 0.5, 0.0, 0.0,
                              trigger, current_price, upnl)


# ── Main reviewer ──────────────────────────────────────────────────────────────

class PositionReviewer:
    """
    Запускается из main loop каждые REVIEW_INTERVAL секунд.
    Проверяет открытые позиции и принимает решение держать/закрыть.
    """
    REVIEW_INTERVAL = 1800   # каждые 30 минут
    MIN_HOLD_TIME   = 3600   # минимум 1 час держим без трогания

    def __init__(self, risk_manager, ai=None, intel=None,
                 telegram=None) -> None:
        self.risk  = risk_manager
        self.ai    = ai
        self.intel = intel
        self.tg    = telegram
        self._last_review: dict[str, float] = {}   # market_id → timestamp

    def should_run(self) -> bool:
        """Проверяем нужно ли запускать review в этом цикле."""
        now = time.time()
        for pos in self.risk.open_positions:
            last = self._last_review.get(pos.market_id, 0)
            if now - last >= self.REVIEW_INTERVAL:
                return True
        return False

    def run(self) -> list[dict]:
        """
        Проверить все открытые позиции.
        Вернуть список действий которые были предприняты.
        """
        open_pos = self.risk.open_positions
        if not open_pos:
            return []

        actions = []
        now     = time.time()

        for pos in open_pos:
            # Пропускаем если слишком рано
            last = self._last_review.get(pos.market_id, 0)
            if now - last < self.REVIEW_INTERVAL:
                continue

            # Пропускаем если позиция слишком молодая
            try:
                opened     = datetime.fromisoformat(pos.opened_at.replace("Z", "+00:00"))
                hours_held = (datetime.now(timezone.utc) - opened).total_seconds() / 3600
                if hours_held < self.MIN_HOLD_TIME / 3600:
                    log.debug(f"  Skip review — too young ({hours_held:.1f}h): {pos.question[:40]}")
                    continue
            except Exception:
                pass

            self._last_review[pos.market_id] = now

            # Текущая цена
            current_price = _get_current_price(pos.token_id) if pos.token_id else None
            if current_price is None:
                log.debug(f"  No price for {pos.market_id[:16]}")
                continue

            upnl = _calc_unrealised_pnl(pos, current_price)
            captured = _profit_captured_pct(pos, current_price)

            log.info(
                f"📋 Review: {pos.question[:50]}\n"
                f"   {pos.direction} @ {pos.entry_price:.3f} → {current_price:.3f} "
                f"| uPnL ${upnl:+.2f} | captured {captured:.0%}"
            )

            # Проверяем триггеры
            trigger = _check_triggers(pos, current_price, self.intel)
            if not trigger:
                log.info(f"   → No trigger, HOLD")
                continue

            log.info(f"   → Trigger: {trigger} — running AI review...")

            # AI переоценка
            decision = _ai_review(pos, current_price, trigger, self.intel, self.ai)

            log.info(
                f"   → AI: {decision.action} | prob={decision.new_prob:.2f} "
                f"EV_hold={decision.ev_hold:.3f} EV_close={decision.ev_close:.3f}\n"
                f"   → Reason: {decision.reasoning}"
            )

            if decision.action == "HOLD":
                self._notify_hold(pos, decision)
                continue

            # Исполняем решение
            if decision.action == "TAKE_PROFIT":
                result = self._close_position(pos, current_price, "TAKE_PROFIT")
                if result:
                    actions.append({
                        "action":   "TAKE_PROFIT",
                        "market_id": pos.market_id,
                        "pnl":      upnl,
                        "reasoning": decision.reasoning,
                    })
                    self._notify_action(pos, decision, upnl)

            elif decision.action == "CUT_LOSS":
                result = self._close_position(pos, current_price, "CUT_LOSS")
                if result:
                    actions.append({
                        "action":   "CUT_LOSS",
                        "market_id": pos.market_id,
                        "pnl":      upnl,
                        "reasoning": decision.reasoning,
                    })
                    self._notify_action(pos, decision, upnl)

        if actions:
            log.info(f"📋 Review complete: {len(actions)} actions taken")

        return actions

    def _close_position(self, pos, current_price: float,
                        reason: str) -> bool:
        """
        Закрыть позицию:
        1. Реально продаём токены на Polymarket
        2. Только если продажа прошла — закрываем в базе
        """
        import os as _os
        upnl = _calc_unrealised_pnl(pos, current_price)
        
        # Пытаемся реально продать на Polymarket
        poly_key = _os.environ.get("POLYMARKET_PRIVATE_KEY", "")
        if pos.token_id and poly_key:
            try:
                from resolver import sell_position_on_polymarket
                shares = pos.size_usd / max(pos.entry_price, 0.001)
                sold, actual_price = sell_position_on_polymarket(
                    token_id = pos.token_id,
                    shares   = shares,
                    poly_key = poly_key,
                )
                if not sold:
                    log.warning(
                        f"⚠️ {reason}: продажа на Polymarket не прошла — "
                        f"позиция НЕ закрыта в базе: {pos.question[:40]}"
                    )
                    return False
                
                # Проверяем что позиция реально закрылась на Polymarket
                funder = _os.environ.get("POLYMARKET_FUNDER", "")
                try:
                    from resolver import verify_position_closed
                    confirmed = verify_position_closed(
                        token_id  = pos.token_id,
                        funder    = funder,
                        retries   = 3,
                        wait_sec  = 10,
                    )
                    if not confirmed:
                        log.warning(
                            f"⚠️ {reason}: продажа отправлена но позиция "
                            f"ещё видна на Polymarket — попробуем позже: {pos.question[:40]}"
                        )
                        return False
                except Exception as ve:
                    log.debug(f"verify error: {ve}")
                    # Если не можем проверить — доверяем результату продажи
                
                # Используем реальную цену продажи
                current_price = actual_price if actual_price > 0 else current_price
                upnl = _calc_unrealised_pnl(pos, current_price)
            except Exception as e:
                log.error(f"sell_position error: {e}")
                return False
        else:
            log.warning(f"⚠️ {reason}: нет token_id или private_key — закрываем только в базе")

        closed = self.risk.close_with_pnl(
            pos.market_id,
            exit_price = current_price,
            pnl        = upnl,
        )
        if closed:
            log.info(f"  ✅ Closed ({reason}): {pos.question[:50]} | P&L ${upnl:+.2f}")
            return True
        return False

    def _notify_hold(self, pos, decision: ReviewDecision) -> None:
        """Тихое уведомление что проверили и решили держать."""
        if not self.tg:
            return
        captured = _profit_captured_pct(pos, decision.current_price)
        if hasattr(self.tg, 'position_review_hold'):
            self.tg.position_review_hold(
                question  = pos.question,
                direction = pos.direction,
                upnl      = decision.unrealised_pnl,
                captured  = captured,
                trigger   = decision.trigger,
                reasoning = decision.reasoning,
                new_prob  = decision.new_prob,
            )

    def _notify_action(self, pos, decision: ReviewDecision,
                       pnl: float) -> None:
        """Уведомление о закрытии позиции через review."""
        if not self.tg:
            return
        import urllib.parse as _up
        slug = getattr(pos, 'slug', None)
        url  = (f"https://polymarket.com/event/{slug}" if slug
                else f"https://polymarket.com/?s={_up.quote((pos.question or '')[:100])}")
        if hasattr(self.tg, 'position_review_action'):
            self.tg.position_review_action(
                question    = pos.question,
                direction   = pos.direction,
                action      = decision.action,
                entry_price = pos.entry_price,
                exit_price  = decision.current_price,
                pnl         = pnl,
                reasoning   = decision.reasoning,
                url         = url,
            )
