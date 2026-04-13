
"""
Prometheus v3 — Risk Manager
Защита капитала. Никакой сделки без прохождения всех проверок.
Audit log каждого решения. PostgreSQL persistence.
"""

from __future__ import annotations
import json
import logging
import hashlib
import os
from dataclasses import dataclass, asdict
from datetime import datetime, date, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger("prometheus.risk")

REASON_OK              = ""
REASON_DAILY_LOSS      = "daily_loss_limit_reached"
REASON_MAX_POSITIONS   = "max_open_positions"
REASON_DUPLICATE       = "duplicate_market"
REASON_CORRELATED      = "correlated_exposure_limit"
REASON_SIZE_TOO_SMALL  = "size_below_minimum"
REASON_SIZE_TOO_LARGE  = "size_above_maximum"
REASON_BOT_PAUSED      = "bot_paused"


@dataclass
class Position:
    market_id:    str
    question:     str
    direction:    str
    entry_price:  float
    size_usd:     float
    opened_at:    str
    tags:         list
    status:       str = "open"
    closed_at:    Optional[str] = None
    exit_price:   Optional[float] = None
    pnl:          Optional[float] = None
    signal_type:  str = "ai"
    token_id:     Optional[str] = None
    slug:         Optional[str] = None
    market_slug:  Optional[str] = None
    event_slug:   Optional[str] = None
    market_url:   Optional[str] = None
    event_url:    Optional[str] = None
    condition_id: Optional[str] = None
    last_verified_at: Optional[str] = None
    reconcile_state: str = "unverified"
    closed_reason: Optional[str] = None


@dataclass
class PendingAction:
    action_type:   str
    market_id:     str
    question:      str
    direction:     str
    size_usd:      float
    token_id:      Optional[str]
    slug:          Optional[str]
    entry_price:   float
    attempts:      int
    max_attempts:  int
    interval_sec:  int
    next_retry_at: float
    created_at:    str
    tags:          list
    market_slug:   Optional[str] = None
    event_slug:    Optional[str] = None
    market_url:    Optional[str] = None
    event_url:     Optional[str] = None
    condition_id:  Optional[str] = None
    signal_type:   str = "ai"
    reason:        str = ""
    target_price:  Optional[float] = None
    last_error:    str = ""

    def due(self, now_ts: float) -> bool:
        return now_ts >= self.next_retry_at


@dataclass
class RiskDecision:
    allowed:    bool
    reason:     str
    size_usd:   float
    audit_hash: str = ""

    @classmethod
    def allow(cls, size: float) -> "RiskDecision":
        return cls(True, REASON_OK, size)

    @classmethod
    def deny(cls, reason: str) -> "RiskDecision":
        return cls(False, reason, 0.0)


class RiskManager:
    def __init__(
        self,
        max_position_usd:   float = 20.0,
        max_daily_loss_usd: float = 50.0,
        max_open_positions: int   = 5,
        max_correlated_usd: float = 35.0,
        kelly_fraction:     float = 0.20,
        data_dir:           str   = "/app/logs",
    ):
        self.max_position_usd   = max_position_usd
        self.max_daily_loss_usd = max_daily_loss_usd
        self.max_open_positions = max_open_positions
        self.max_correlated_usd = max_correlated_usd
        self.kelly_fraction     = kelly_fraction
        self._dir               = Path(data_dir)
        self._paused            = False

        self._positions: list[Position] = []
        self._pending_actions: list[PendingAction] = []
        self._audit_log: list[dict] = []
        self._db_init()
        self._load()

        if os.environ.get("CLEAR_POSITIONS_ON_START", "").lower() in ("1", "true", "yes"):
            open_count = len([p for p in self._positions if p.status == "open"])
            if open_count > 0:
                self._positions = [p for p in self._positions if p.status != "open"]
                self._save()
                log.info(f"🧹 CLEAR_POSITIONS_ON_START: удалено {open_count} открытых позиций")

    def kelly_size(
        self,
        ai_probability: float,
        market_price:   float,
        direction:      str,
        bankroll:       float = 100.0,
    ) -> float:
        p     = ai_probability if direction == "YES" else 1 - ai_probability
        price = market_price   if direction == "YES" else 1 - market_price
        price = max(price, 0.01)
        payout = (1 / price) - 1
        kelly  = (p * payout - (1 - p)) / payout
        kelly  = max(0.0, kelly)
        raw = bankroll * kelly * self.kelly_fraction
        return round(min(raw, self.max_position_usd), 2)

    def check(self, market_id: str, size_usd: float, tags: list = None) -> RiskDecision:
        tags = tags or []

        if self._paused:
            return self._log_deny(market_id, REASON_BOT_PAUSED, size_usd)
        if self._daily_pnl() <= -self.max_daily_loss_usd:
            return self._log_deny(market_id, REASON_DAILY_LOSS, size_usd)
        if len(self.open_positions) >= self.max_open_positions:
            return self._log_deny(market_id, REASON_MAX_POSITIONS, size_usd)
        if any(p.market_id == market_id for p in self.open_positions):
            return self._log_deny(market_id, REASON_DUPLICATE, size_usd)
        if any(a.market_id == market_id and a.action_type == "open_verify" for a in self._pending_actions):
            return self._log_deny(market_id, REASON_DUPLICATE, size_usd)

        if tags:
            correlated = sum(
                p.size_usd for p in self.open_positions
                if any(t in p.tags for t in tags)
            )
            if correlated + size_usd > self.max_correlated_usd:
                return self._log_deny(market_id, REASON_CORRELATED, size_usd)

        if size_usd < 1.0:
            return self._log_deny(market_id, REASON_SIZE_TOO_SMALL, size_usd)

        capped = min(size_usd, self.max_position_usd)
        self._audit("ALLOW", market_id, capped, REASON_OK)
        return RiskDecision.allow(capped)

    def open(
        self,
        market_id:   str,
        question:    str,
        direction:   str,
        entry_price: float,
        size_usd:    float,
        tags:        list = None,
        signal_type: str  = "ai",
        token_id:    str  = None,
        slug:        str  = None,
        market_slug: str  = None,
        event_slug:  str  = None,
        market_url:  str  = None,
        event_url:   str  = None,
        condition_id:str  = None,
    ) -> Position:
        pos = Position(
            market_id   = market_id,
            question    = question,
            direction   = direction,
            entry_price = entry_price,
            size_usd    = size_usd,
            opened_at   = datetime.now(timezone.utc).isoformat(),
            tags        = tags or [],
            signal_type = signal_type,
            token_id    = token_id,
            slug        = slug,
            market_slug = market_slug,
            event_slug  = event_slug,
            market_url  = market_url,
            event_url   = event_url,
            condition_id= condition_id or market_id,
            last_verified_at = datetime.now(timezone.utc).isoformat() if not os.environ.get("DRY_RUN", "true").lower() in ("1","true","yes","on") else None,
            reconcile_state = "open_verified" if not os.environ.get("DRY_RUN", "true").lower() in ("1","true","yes","on") else "paper_open",
        )
        self._positions.append(pos)
        self._save()
        log.info(f"✅ Позиция открыта | {direction} @ {entry_price:.3f} ${size_usd:.2f} | {question[:60]}")
        return pos

    def close(self, market_id: str, exit_price: float, won: bool) -> Optional[Position]:
        for pos in self._positions:
            if pos.market_id == market_id and pos.status == "open":
                if pos.direction == "YES":
                    pnl = pos.size_usd * (((1.0 / max(pos.entry_price, 0.01)) - 1) if won else -1)
                else:
                    no_entry = 1.0 - pos.entry_price
                    pnl = pos.size_usd * (((1.0 / max(no_entry, 0.01)) - 1) if won else -1)
                pos.status = "closed"
                pos.closed_at = datetime.now(timezone.utc).isoformat()
                pos.exit_price = exit_price
                pos.pnl = round(pnl, 2)
                self._save()
                log.info(f"{'✅' if won else '❌'} {'WIN' if won else 'LOSS'} | P&L ${pnl:+.2f} | {pos.question[:60]}")
                return pos
        return None

    def close_with_pnl(self, market_id: str, exit_price: float, pnl: float) -> Optional[Position]:
        for pos in self._positions:
            if pos.market_id == market_id and pos.status == "open":
                pos.status = "closed"
                pos.closed_at = datetime.now(timezone.utc).isoformat()
                pos.exit_price = exit_price
                pos.pnl = round(pnl, 2)
                self._save()
                return pos
        return None

    def register_pending_open(
        self,
        *,
        market_id: str,
        question: str,
        direction: str,
        entry_price: float,
        size_usd: float,
        tags: list,
        signal_type: str,
        token_id: str | None,
        slug: str | None,
        market_slug: str | None = None,
        event_slug: str | None = None,
        market_url: str | None = None,
        event_url: str | None = None,
        condition_id: str | None = None,
        max_attempts: int = 10,
        interval_sec: int,
    ) -> PendingAction:
        existing = next((a for a in self._pending_actions if a.market_id == market_id and a.action_type == "open_verify"), None)
        if existing:
            return existing
        action = PendingAction(
            action_type="open_verify",
            market_id=market_id,
            question=question,
            direction=direction,
            size_usd=size_usd,
            token_id=token_id,
            slug=slug,
            signal_type=signal_type,
            market_slug=market_slug,
            event_slug=event_slug,
            market_url=market_url,
            event_url=event_url,
            condition_id=condition_id or market_id,
            entry_price=entry_price,
            attempts=0,
            max_attempts=max_attempts,
            interval_sec=interval_sec,
            next_retry_at=0.0,
            created_at=datetime.now(timezone.utc).isoformat(),
            tags=tags or [],
        )
        self._pending_actions.append(action)
        self._save()
        log.info(f"🕐 Позиция ожидает подтверждения открытия на Polymarket: {question[:60]}")
        return action

    def register_pending_close(
        self,
        *,
        pos: Position,
        reason: str,
        max_attempts: int,
        interval_sec: int,
        target_price: float | None = None,
        last_error: str = "",
    ) -> PendingAction:
        existing = next((a for a in self._pending_actions if a.market_id == pos.market_id and a.action_type == "close_retry"), None)
        if existing:
            existing.reason = reason or existing.reason
            existing.target_price = target_price if target_price is not None else existing.target_price
            existing.last_error = last_error or existing.last_error
            self._save()
            return existing
        action = PendingAction(
            action_type="close_retry",
            market_id=pos.market_id,
            question=pos.question,
            direction=pos.direction,
            size_usd=pos.size_usd,
            token_id=pos.token_id,
            slug=pos.slug,
            signal_type=pos.signal_type,
            market_slug=pos.market_slug,
            event_slug=pos.event_slug,
            market_url=pos.market_url,
            event_url=pos.event_url,
            condition_id=pos.condition_id or pos.market_id,
            entry_price=pos.entry_price,
            attempts=0,
            max_attempts=max_attempts,
            interval_sec=interval_sec,
            next_retry_at=0.0,
            created_at=datetime.now(timezone.utc).isoformat(),
            tags=pos.tags or [],
            reason=reason,
            target_price=target_price,
            last_error=last_error,
        )
        self._pending_actions.append(action)
        self._save()
        log.warning(f"🕐 Позиция поставлена в очередь повторного закрытия: {pos.question[:60]}")
        return action

    def mark_action_attempt(self, action: PendingAction, *, error: str = "", due_in: int | None = None) -> PendingAction:
        action.attempts += 1
        action.last_error = error[:250]
        action.next_retry_at = self._utc_ts() + float(due_in if due_in is not None else action.interval_sec)
        self._save()
        return action

    def remove_pending_action(self, market_id: str, action_type: str) -> None:
        before = len(self._pending_actions)
        self._pending_actions = [
            a for a in self._pending_actions
            if not (a.market_id == market_id and a.action_type == action_type)
        ]
        if len(self._pending_actions) != before:
            self._save()

    def sync_closed_from_api(self, closed_positions_data: list[dict]) -> None:
        existing_ids = {p.market_id for p in self._positions}
        for item in closed_positions_data:
            mid = item.get("id") or item.get("market_id", "")
            if not mid or mid in existing_ids:
                continue
            try:
                pos = Position(
                    market_id=mid,
                    question=item.get("question", ""),
                    direction=item.get("direction", "YES"),
                    entry_price=float(item.get("price", 0)),
                    size_usd=float(item.get("size", 0)),
                    opened_at=item.get("opened_at", datetime.now(timezone.utc).isoformat()),
                    tags=item.get("tags", []),
                    status="closed",
                    closed_at=item.get("closed_at", datetime.now(timezone.utc).isoformat()),
                    exit_price=float(item.get("exit_price") or item.get("current_price") or 0),
                    pnl=float(item.get("pnl") or 0),
                    signal_type=item.get("type", "ai"),
                    token_id=item.get("token_id"),
                    slug=item.get("slug"),
                    market_slug=item.get("market_slug"),
                    event_slug=item.get("event_slug"),
                    market_url=item.get("market_url") or item.get("url"),
                    event_url=item.get("event_url"),
                    condition_id=item.get("condition_id") or mid,
                    last_verified_at=item.get("last_verified_at"),
                    reconcile_state=item.get("reconcile_state", "closed_verified"),
                    closed_reason=item.get("closed_reason"),
                )
                self._positions.append(pos)
                existing_ids.add(mid)
            except Exception as e:
                log.debug(f"sync_closed_from_api: {e}")

    def pause(self):
        self._paused = True
        log.warning("⏸ Бот приостановлен")
        self._save()

    def resume(self):
        self._paused = False
        log.info("▶ Бот возобновлён")
        self._save()

    @property
    def open_positions(self) -> list[Position]:
        return [p for p in self._positions if p.status == "open"]

    @property
    def closed_positions(self) -> list[Position]:
        return [p for p in self._positions if p.status == "closed"]

    @property
    def pending_actions(self) -> list[PendingAction]:
        return list(self._pending_actions)

    @property
    def pending_open_actions(self) -> list[PendingAction]:
        return [a for a in self._pending_actions if a.action_type == "open_verify"]

    @property
    def pending_close_actions(self) -> list[PendingAction]:
        return [a for a in self._pending_actions if a.action_type == "close_retry"]

    @property
    def is_paused(self) -> bool:
        return self._paused

    def reconcile_open_positions(self, real_positions: dict[str, dict]) -> dict:
        """
        Сверить локальные open-позиции с Polymarket.
        Источник правды — Polymarket.
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        externally_closed: list[dict] = []
        discovered: list[dict] = []

        pending_close_ids = {a.market_id for a in self.pending_close_actions}
        local_by_token = {p.token_id: p for p in self.open_positions if p.token_id}

        for pos in self.open_positions:
            if not pos.token_id:
                continue
            real = real_positions.get(pos.token_id)
            if real and float(real.get("size", 0) or 0) > 0.0001:
                pos.last_verified_at = now_iso
                pos.reconcile_state = "open_verified"
                continue
            if pos.market_id in pending_close_ids:
                continue

            pos.status = "closed"
            pos.closed_at = now_iso
            pos.closed_reason = "external_manual_close"
            pos.reconcile_state = "externally_closed"
            pos.last_verified_at = now_iso
            if pos.pnl is None:
                pos.pnl = 0.0
            externally_closed.append({
                "market_id": pos.market_id,
                "question": pos.question,
                "token_id": pos.token_id,
                "reason": pos.closed_reason,
            })

        for token_id, real in real_positions.items():
            if token_id in local_by_token:
                continue
            discovered.append({
                "token_id": token_id,
                "market_id": real.get("market_id", ""),
                "size": float(real.get("size", 0) or 0),
                "avg_price": float(real.get("avg_price", 0) or 0),
                "cur_price": float(real.get("cur_price", 0) or 0),
            })

        if externally_closed or discovered:
            self._save()
        return {"externally_closed": externally_closed, "discovered": discovered}

    def snapshot(self) -> dict:
        daily = self._daily_pnl()
        total = sum(p.pnl or 0 for p in self.closed_positions)
        closed = self.closed_positions
        wins = [p for p in closed if (p.pnl or 0) > 0]
        return {
            "open_positions": len(self.open_positions),
            "open_exposure": round(sum(p.size_usd for p in self.open_positions), 2),
            "daily_pnl": round(daily, 2),
            "total_pnl": round(total, 2),
            "total_trades": len(closed),
            "win_rate": round(len(wins) / len(closed), 3) if closed else 0,
            "daily_loss_pct": round(abs(min(0, daily)) / self.max_daily_loss_usd, 3),
            "can_trade": not self._paused and daily > -self.max_daily_loss_usd,
            "paused": self._paused,
            "pending_open": len(self.pending_open_actions),
            "pending_close": len(self.pending_close_actions),
        }

    def performance_by_tag(self) -> dict:
        result: dict = {}
        for p in self.closed_positions:
            for tag in (p.tags or []):
                if tag not in result:
                    result[tag] = {"trades": 0, "wins": 0, "pnl": 0.0, "vol": 0.0}
                result[tag]["trades"] += 1
                if (p.pnl or 0) > 0:
                    result[tag]["wins"] += 1
                result[tag]["pnl"] += p.pnl or 0
                result[tag]["vol"] += p.size_usd
        for tag in result:
            s = result[tag]
            s["win_rate"] = round(s["wins"] / s["trades"], 3)
            s["roi"] = round(s["pnl"] / s["vol"], 3) if s["vol"] else 0
        return result

    def _daily_pnl(self) -> float:
        today = date.today().isoformat()
        return sum(
            p.pnl or 0
            for p in self.closed_positions
            if (p.closed_at or "")[:10] == today
        )

    def _utc_ts(self) -> float:
        return datetime.now(timezone.utc).timestamp()

    def _log_deny(self, market_id: str, reason: str, size: float) -> RiskDecision:
        self._audit("DENY", market_id, size, reason)
        log.info(f"🚫 Сделка отклонена: {reason} | ${size:.2f} | {market_id[:16]}")
        return RiskDecision.deny(reason)

    def _audit(self, action: str, market_id: str, size: float, reason: str):
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "market_id": market_id,
            "size": size,
            "reason": reason,
        }
        entry["hash"] = hashlib.sha256(json.dumps(entry).encode()).hexdigest()[:16]
        self._audit_log.append(entry)
        try:
            audit_path = self._dir / "audit.jsonl"
            audit_path.parent.mkdir(parents=True, exist_ok=True)
            with audit_path.open("a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as e:
            log.error(f"Audit log write failed: {e}")

    def _db_conn(self):
        db_url = os.environ.get("DATABASE_URL", "")
        if not db_url:
            return None
        try:
            import psycopg2
            if db_url.startswith("postgres://"):
                db_url = db_url.replace("postgres://", "postgresql://", 1)
            return psycopg2.connect(db_url)
        except Exception:
            return None

    def _db_init(self):
        conn = self._db_conn()
        if not conn:
            return
        try:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS positions (
                    market_id   TEXT PRIMARY KEY,
                    data        TEXT NOT NULL,
                    updated_at  TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_runtime_state (
                    state_key   TEXT PRIMARY KEY,
                    data        TEXT NOT NULL,
                    updated_at  TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            conn.commit()
            cur.close()
            conn.close()
        except Exception as e:
            log.debug(f"DB init positions/runtime: {e}")

    def _save_runtime_state(self):
        state = {
            "paused": self._paused,
            "pending_actions": [asdict(a) for a in self._pending_actions],
        }
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            (self._dir / "runtime_state.json").write_text(json.dumps(state, indent=2))
        except Exception as e:
            log.error(f"Runtime state file save failed: {e}")

        conn = self._db_conn()
        if not conn:
            return
        try:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO bot_runtime_state (state_key, data, updated_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (state_key) DO UPDATE
                SET data = EXCLUDED.data, updated_at = NOW()
            """, ("runtime", json.dumps(state)))
            conn.commit()
            cur.close()
            conn.close()
        except Exception as e:
            log.debug(f"DB save runtime state: {e}")

    def _save(self):
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            data = [asdict(p) for p in self._positions]
            (self._dir / "positions.json").write_text(json.dumps(data, indent=2))
        except Exception as e:
            log.error(f"Positions file save failed: {e}")

        conn = self._db_conn()
        if conn:
            try:
                cur = conn.cursor()
                for p in self._positions:
                    cur.execute("""
                        INSERT INTO positions (market_id, data, updated_at)
                        VALUES (%s, %s, NOW())
                        ON CONFLICT (market_id) DO UPDATE
                        SET data = EXCLUDED.data, updated_at = NOW()
                    """, (p.market_id, json.dumps(asdict(p))))
                conn.commit()
                cur.close()
                conn.close()
            except Exception as e:
                log.debug(f"DB save positions: {e}")
        self._save_runtime_state()

    def _load_runtime_state(self):
        data = None
        conn = self._db_conn()
        if conn:
            try:
                cur = conn.cursor()
                cur.execute("SELECT data FROM bot_runtime_state WHERE state_key=%s", ("runtime",))
                row = cur.fetchone()
                cur.close()
                conn.close()
                if row:
                    data = json.loads(row[0])
            except Exception as e:
                log.debug(f"DB load runtime state: {e}")
        if data is None:
            try:
                path = self._dir / "runtime_state.json"
                if path.exists():
                    data = json.loads(path.read_text())
            except Exception as e:
                log.error(f"Runtime state load failed: {e}")
        if not data:
            return
        self._paused = bool(data.get("paused", False))
        self._pending_actions = []
        for item in data.get("pending_actions", []):
            try:
                self._pending_actions.append(PendingAction(**item))
            except Exception as e:
                log.debug(f"Pending action load failed: {e}")

    def _load(self):
        conn = self._db_conn()
        if conn:
            try:
                cur = conn.cursor()
                cur.execute("SELECT data FROM positions ORDER BY updated_at DESC")
                rows = cur.fetchall()
                cur.close()
                conn.close()
                if rows:
                    seen = set()
                    for row in rows:
                        d = json.loads(row[0])
                        mid = d.get("market_id", "")
                        if mid and mid not in seen:
                            seen.add(mid)
                            self._positions.append(Position(**d))
                    log.info(f"Загружено {len(self._positions)} позиций из PostgreSQL")
            except Exception as e:
                log.debug(f"DB load positions: {e}")

        if not self._positions:
            try:
                path = self._dir / "positions.json"
                if path.exists():
                    for d in json.loads(path.read_text()):
                        self._positions.append(Position(**d))
                    log.info(f"Загружено {len(self._positions)} позиций из файла")
            except Exception as e:
                log.error(f"Positions load failed: {e}")

        self._load_runtime_state()
