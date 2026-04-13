"""
Prometheus — Telegram Notifier v3.7
Полный набор уведомлений. Чистый минималистичный дизайн.

Уведомления:
- Открытие/закрытие позиции
- Position review (HOLD / TAKE_PROFIT / CUT_LOSS)
- Инсайдер обнаружен
- Breaking news
- Domain prior изменился (домен стал горячим/холодным)
- Дневной и еженедельный отчёты
- Cycle summary (только если были сделки)
- Ошибки
"""
from __future__ import annotations
import logging
import requests

log = logging.getLogger("prometheus.telegram")

_SESSION = requests.Session()
_SESSION.headers["User-Agent"] = "Prometheus/3.0"


class Telegram:
    def __init__(self, token: str, chat_id: str) -> None:
        self.token   = token
        self.chat_id = chat_id
        self.enabled = bool(token and chat_id)
        self._base   = f"https://api.telegram.org/bot{token}"
        if not self.enabled:
            log.warning("Telegram отключён — TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID не заданы")

    def send(self, text: str, silent: bool = False) -> bool:
        if not self.enabled:
            return False
        import time as _time
        for attempt in range(3):  # 3 попытки
            try:
                r = _SESSION.post(
                    f"{self._base}/sendMessage",
                    json={
                        "chat_id":              self.chat_id,
                        "text":                 text[:4096],
                        "parse_mode":           "Markdown",
                        "disable_notification": silent,
                    },
                    timeout=8,
                )
                if r.status_code == 200:
                    return True
                if r.status_code == 429:
                    # Flood limit — ждём сколько Telegram говорит
                    retry_after = 5
                    try:
                        retry_after = r.json().get("parameters", {}).get("retry_after", 5)
                    except Exception:
                        pass
                    log.warning(f"Telegram flood limit — ждём {retry_after}с (попытка {attempt+1}/3)")
                    _time.sleep(retry_after)
                    continue
                log.warning(f"Telegram HTTP {r.status_code}: {r.text[:200]}")
                return False
            except Exception as e:
                log.warning(f"Telegram error (попытка {attempt+1}/3): {e}")
                if attempt < 2:
                    _time.sleep(3)
        return False

    # ── Система ───────────────────────────────────────────────────────────────

    def started(self, dry_run: bool) -> None:
        mode = "Paper Trading" if dry_run else "🔴 LIVE"
        self.send(f"🤖 *Prometheus* запущен — {mode}")

    def stopped(self) -> None:
        self.send("⛔ Prometheus остановлен")

    def error(self, msg: str) -> None:
        self.send(f"🚨 *Ошибка*\n`{msg[:200]}`")

    # ── Позиции ───────────────────────────────────────────────────────────────

    def position_opened(
        self,
        question:    str,
        direction:   str,
        price:       float,
        size:        float,
        edge:        float,
        confidence:  str,
        reasoning:   str,
        dry_run:     bool,
        signals:     list = None,
        signal_type: str  = "ai",
        url:         str  = "",
        domain_mult: float = 1.0,
        timing:      str  = "",
    ) -> None:
        dir_icon  = "🟢" if direction == "YES" else "🔴"
        conf_icon = {"high": "🔥", "medium": "✅", "low": "⚡"}.get(confidence, "")
        src       = "AI" if signal_type == "ai" else "Smart Money"
        mode      = "PAPER" if dry_run else "LIVE"

        token_price  = price if direction == "YES" else 1.0 - price
        expected_win = round(size * ((1.0 / max(token_price, 0.01)) - 1), 2)

        lines = [
            f"{dir_icon} *{mode} · {src}*",
            f"",
            f"*{question[:80]}*",
            f"",
            f"Направление:  *{direction}*  @  `{price:.3f}`",
            f"Размер:       *${size:.2f}*  →  потенциал *+${expected_win:.2f}*",
            f"Edge:         *{edge:.1%}*  {conf_icon} {confidence}",
        ]

        # Domain multiplier если отличается от нормы
        if domain_mult > 1.15:
            lines.append(f"Domain:       🔥 *{domain_mult:.1f}x* (домен работает хорошо)")
        elif domain_mult < 0.75:
            lines.append(f"Domain:       ⚠️ *{domain_mult:.1f}x* (домен слабый)")

        # Timing если не пик
        if timing and "peak" not in timing.lower():
            lines.append(f"Timing:       ⏰ {timing}")

        # Главный сигнал
        if signals:
            active = [s for s in signals
                      if s.direction != "NEUTRAL"
                      and s.reasoning not in ("error", "insufficient history",
                                              "no Kalshi match", "no PredictIt match")]
            if active:
                top = active[0]
                lines += ["", f"💡 _{top.reasoning[:120]}_"]

        if url:
            lines += ["", f"[Открыть на Polymarket]({url})"]

        self.send("\n".join(lines))

    def position_closed(
        self,
        question:    str,
        direction:   str,
        entry_price: float,
        exit_price:  float,
        size:        float,
        pnl:         float,
        outcome:     str,
        signal_type: str = "ai",
        url:         str = "",
    ) -> None:
        won     = pnl > 0
        icon    = "✅" if won else ("🛑" if outcome == "STOP_LOSS" else "❌")
        result  = "ПОБЕДА" if won else ("СТОП-ЛОСС" if outcome == "STOP_LOSS" else "ПОТЕРЯ")
        pnl_str = f"+${pnl:.2f}" if won else f"−${abs(pnl):.2f}"
        src     = "AI" if signal_type == "ai" else "Smart Money"

        lines = [
            f"{icon} *{src} — {result}*",
            f"",
            f"*{question[:80]}*",
            f"",
            f"*{pnl_str}*  |  {direction}  @  `{entry_price:.3f}` → `{exit_price:.3f}`",
        ]

        if url:
            lines += ["", f"[Polymarket]({url})"]

        self.send("\n".join(lines))

    # ── Position Review ───────────────────────────────────────────────────────

    def position_review_hold(
        self,
        question:    str,
        direction:   str,
        upnl:        float,
        captured:    float,
        trigger:     str,
        reasoning:   str,
        new_prob:    float,
    ) -> None:
        """Тихое уведомление — проверили позицию, решили держать."""
        icon    = "📈" if upnl >= 0 else "📉"
        upnl_s  = f"+${upnl:.2f}" if upnl >= 0 else f"−${abs(upnl):.2f}"

        self.send(
            f"🔍 *Обзор — ДЕРЖИМ*\n\n"
            f"*{question[:70]}*\n\n"
            f"{icon} uP&L: *{upnl_s}*  |  Захвачено: *{captured:.0%}*\n"
            f"Триггер: `{trigger}`  |  Prob: *{new_prob:.0%}*\n"
            f"_{reasoning[:100]}_",
            silent=True,
        )

    def position_review_action(
        self,
        question:    str,
        direction:   str,
        action:      str,
        entry_price: float,
        exit_price:  float,
        pnl:         float,
        reasoning:   str,
        url:         str = "",
    ) -> None:
        """Активное закрытие через review — TAKE_PROFIT или CUT_LOSS."""
        won     = pnl >= 0
        icon    = "💰" if action == "TAKE_PROFIT" else "✂️"
        label   = "ФИКСАЦИЯ ПРИБЫЛИ" if action == "TAKE_PROFIT" else "СТОП ПО ТЕЗИСУ"
        pnl_str = f"+${pnl:.2f}" if won else f"−${abs(pnl):.2f}"

        lines = [
            f"{icon} *{label}*",
            f"",
            f"*{question[:70]}*",
            f"",
            f"*{pnl_str}*  |  {direction}  @  `{entry_price:.3f}` → `{exit_price:.3f}`",
            f"",
            f"💡 _{reasoning[:120]}_",
        ]
        if url:
            lines += ["", f"[Polymarket]({url})"]
        self.send("\n".join(lines))

    # ── Smart Money ───────────────────────────────────────────────────────────

    def insider_detected(
        self,
        question:    str,
        direction:   str,
        price:       float,
        their_size:  float,
        our_size:    float,
        trader_class: str,
        win_rate:    float,
        roi:         float,
        reasoning:   str,
        url:         str = "",
    ) -> None:
        """Обнаружен инсайдер или sharp трейдер — следуем за ним."""
        dir_icon = "🟢" if direction == "YES" else "🔴"
        cls_icon = {"insider": "🔴", "sharp": "🟡", "contrarian": "🔵"}.get(
            trader_class.lower(), "🐋")

        their_str = (f"${their_size/1000:.0f}k"
                     if their_size >= 1000 else f"${their_size:.0f}")

        lines = [
            f"{cls_icon} *{trader_class.upper()} DETECTED*",
            f"",
            f"*{question[:80]}*",
            f"",
            f"{dir_icon} *{direction}*  @  `{price:.3f}`",
            f"Их ставка: *{their_str}*  |  Наша: *${our_size:.2f}*",
            f"",
            f"WR *{win_rate:.0%}*  ROI *{roi:+.0%}*",
            f"_{reasoning[:120]}_",
        ]
        if url:
            lines += ["", f"[Polymarket]({url})"]
        self.send("\n".join(lines))

    def smart_money(self, question: str, direction: str,
                    price: float, size: float, reasoning: str) -> None:
        """Обратная совместимость."""
        dir_icon = "🟢" if direction == "YES" else "🔴"
        self.send(
            f"🔎 *Smart Money*\n\n"
            f"*{question[:80]}*\n\n"
            f"{dir_icon} {direction}  @  `{price:.3f}`  →  *${size:.2f}*\n\n"
            f"_{reasoning[:150]}_"
        )

    # ── Domain Intelligence ───────────────────────────────────────────────────

    def domain_shift(
        self,
        domain:      str,
        old_mult:    float,
        new_mult:    float,
        win_rate:    float,
        total:       int,
    ) -> None:
        """
        Домен значительно изменил свой множитель.
        Отправляем когда переходим через порог 1.2x или 0.7x.
        """
        if new_mult > old_mult:
            icon  = "🔥"
            label = "усиливаем"
        else:
            icon  = "📉"
            label = "снижаем"

        self.send(
            f"{icon} *Domain Update — {domain.upper()}*\n\n"
            f"Множитель: *{old_mult:.1f}x* → *{new_mult:.1f}x* ({label})\n"
            f"Win rate: *{win_rate:.0%}*  |  Сделок: *{total}*\n\n"
            f"_Размер позиций в этом домене {'увеличен' if new_mult > old_mult else 'снижен'}_",
            silent=True,
        )

    def microstructure_signal(
        self,
        question:   str,
        direction:  str,
        confidence: float,
        reasoning:  str,
        action:     str,   # "confirmed" / "contra" / "reduced"
    ) -> None:
        """Order book сигнал по открытой или новой позиции."""
        icon = "📊"
        if action == "confirmed":
            label = "ORDER BOOK ПОДТВЕРЖДАЕТ"
        elif action == "contra":
            label = "ORDER BOOK ПРОТИВ — размер снижен"
        else:
            label = "ORDER BOOK СИГНАЛ"

        self.send(
            f"{icon} *{label}*\n\n"
            f"*{question[:70]}*\n\n"
            f"Направление: *{direction}*  conf: *{confidence:.0%}*\n"
            f"_{reasoning}_",
            silent=True,
        )

    # ── Алерты ────────────────────────────────────────────────────────────────

    def arbitrage(self, title: str, content: str) -> None:
        self.send(f"🎯 *Арбитраж*\n\n{title}\n_{content}_")

    def breaking(self, items: list[str]) -> None:
        # Disabled on purpose: these alerts were too noisy and not actionable.
        return

    def limit_warning(self, pct: float) -> None:
        self.send(f"⚠️ Лимит потерь использован на *{pct:.0%}*")

    def weights_updated(self, weights: dict) -> None:
        lines = ["🧠 *Веса сигналов обновлены*", ""]
        for name, w in sorted(weights.items(), key=lambda x: -x[1]):
            bar = "▓" * int(w * 15) + "░" * (15 - int(w * 15))
            lines.append(f"`{name:<12}` {bar} {w:.0%}")
        self.send("\n".join(lines), silent=True)

    # ── Отчёты ────────────────────────────────────────────────────────────────

    def daily_report(self, snap: dict, by_tag: dict,
                     signals: int, sm_alerts: int,
                     domain_stats: dict = None) -> None:
        from datetime import date
        pnl_today = snap["daily_pnl"]
        pnl_total = snap["total_pnl"]
        icon      = "📈" if pnl_today >= 0 else "📉"

        lines = [
            f"📊 *Отчёт* — {date.today()}",
            f"",
            f"{icon} Сегодня:  *{'+' if pnl_today>=0 else ''}${pnl_today:.2f}*",
            f"   Всего:   *{'+' if pnl_total>=0 else ''}${pnl_total:.2f}*",
            f"",
            f"Win rate:  *{snap['win_rate']:.0%}*  ({snap['total_trades']} сделок)",
            f"Открыто:  {snap['open_positions']}  |  Сигналов: {signals}",
        ]

        # По доменам из domain prior
        if domain_stats:
            active = [(d, s) for d, s in domain_stats.items()
                      if s.get("wins", 0) + s.get("losses", 0) >= 3]
            active.sort(key=lambda x: x[1].get("pnl", 0), reverse=True)
            if active:
                lines += ["", "*Домены:*"]
                for domain, s in active[:5]:
                    mult = s.get("multiplier", 1.0)
                    mult_str = f" *{mult:.1f}x*" if abs(mult - 1.0) > 0.1 else ""
                    pnl_d = s.get("pnl", 0)
                    lines.append(
                        f"  {'✅' if pnl_d>=0 else '❌'} "
                        f"`{domain:<12}` "
                        f"WR {s.get('win_rate',0):.0%}  "
                        f"${pnl_d:+.1f}"
                        f"{mult_str}"
                    )
        elif by_tag:
            top = sorted(
                [(t, s) for t, s in by_tag.items() if s["trades"] >= 2],
                key=lambda x: x[1]["pnl"], reverse=True
            )[:4]
            if top:
                lines += ["", "*Домены:*"]
                for tag, s in top:
                    lines.append(
                        f"  {'✅' if s['pnl']>=0 else '❌'} "
                        f"{tag}: WR {s['win_rate']:.0%}  ${s['pnl']:+.1f}"
                    )

        self.send("\n".join(lines))

    def weekly_report(self, snap: dict, domain_stats: dict = None) -> None:
        """Еженедельный итог — каждое воскресенье."""
        pnl_total = snap["total_pnl"]
        icon      = "📈" if pnl_total >= 0 else "📉"

        lines = [
            f"📆 *Недельный итог*",
            f"",
            f"{icon} P&L всего:  *{'+' if pnl_total>=0 else ''}${pnl_total:.2f}*",
            f"Win rate:   *{snap['win_rate']:.0%}*  ({snap['total_trades']} сделок)",
            f"Открыто:   {snap['open_positions']}",
        ]

        if domain_stats:
            hot = [(d, s) for d, s in domain_stats.items()
                   if s.get("multiplier", 1.0) > 1.2]
            cold = [(d, s) for d, s in domain_stats.items()
                    if s.get("multiplier", 1.0) < 0.7]
            if hot:
                lines += ["", "🔥 *Горячие домены:*"]
                for d, s in sorted(hot, key=lambda x: -x[1].get("multiplier",1))[:3]:
                    lines.append(f"  {d}: *{s.get('multiplier',1):.1f}x*  WR {s.get('win_rate',0):.0%}")
            if cold:
                lines += ["", "❄️ *Слабые домены:*"]
                for d, s in sorted(cold, key=lambda x: x[1].get("multiplier",1))[:3]:
                    lines.append(f"  {d}: *{s.get('multiplier',1):.1f}x*  WR {s.get('win_rate',0):.0%}")

        self.send("\n".join(lines))

    def cycle_summary(self, ai: int, sm: int, pnl: float) -> None:
        """Только если были сделки — не спамим."""
        if ai == 0 and sm == 0:
            return
        icon = "📈" if pnl >= 0 else "📉"
        self.send(
            f"🔄 Цикл: AI={ai} SM={sm}  {icon} ${pnl:+.2f}",
            silent=True,
        )

    def trade(self, question: str, direction: str, price: float,
              size: float, edge: float, confidence: str,
              reasoning: str, dry_run: bool,
              signals: list = None) -> None:
        """Обратная совместимость."""
        self.position_opened(
            question=question, direction=direction, price=price,
            size=size, edge=edge, confidence=confidence,
            reasoning=reasoning, dry_run=dry_run,
            signals=signals, signal_type="ai",
        )
