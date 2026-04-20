"""
Prometheus — API v3.1
PostgreSQL хранилище. Данные не теряются при деплое.

FIXES v3.1:
- /api/close_position теперь обновляет overview P&L корректно
- Добавлен /api/overview/recalc для пересчёта P&L из позиций
- db_get/db_set используют connection pooling (один conn на запрос, корректный close)
- Signals endpoint возвращает корректный список без вложенного _list
"""
import json
import os
import re
import secrets
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, Depends, Header, Request
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="Prometheus", version="3.2")
_ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "*").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS or ["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# ── Rate Limiter ───────────────────────────────────────────────────────────────
# Sliding-window in-memory rate limiter (no external dependencies).
# Периодически очищает старые записи чтобы не расти в памяти.

_rl_store: dict[str, list[float]] = defaultdict(list)
_rl_lock  = threading.Lock()
_rl_last_gc: float = 0.0
_RL_GC_INTERVAL = 120  # очищать словарь раз в 2 минуты


def _rate_limited(key: str, limit: int, window: int) -> bool:
    """
    Sliding window rate limiter.
    Возвращает True если лимит превышен (запрос нужно отклонить).
    key    — обычно IP адрес или имя эндпоинта
    limit  — макс. кол-во запросов
    window — окно в секундах
    """
    global _rl_last_gc
    now = time.time()
    with _rl_lock:
        # Периодическая сборка мусора: удаляем ключи без активности
        if now - _rl_last_gc > _RL_GC_INTERVAL:
            stale = [k for k, ts in _rl_store.items()
                     if not ts or now - ts[-1] > window * 2]
            for k in stale:
                del _rl_store[k]
            _rl_last_gc = now

        calls = _rl_store[key]
        # Вытесняем старые метки вне окна
        _rl_store[key] = [t for t in calls if now - t < window]
        if len(_rl_store[key]) >= limit:
            return True
        _rl_store[key].append(now)
        return False


def _client_ip(request: Request) -> str:
    """Извлекает реальный IP из заголовков прокси или напрямую."""
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


_MARKET_ID_RE = re.compile(r'^[0-9a-zA-Z_\-]{1,150}$')

POLY_FUNDER  = os.environ.get("POLYMARKET_FUNDER", "")
API_KEY      = os.environ.get("DASHBOARD_API_KEY", "")
MANUAL_CLOSE_KEY = os.environ.get("MANUAL_CLOSE_KEY", "")
DATABASE_URL = (
    os.environ.get("DATABASE_URL") or
    os.environ.get("database_url") or
    os.environ.get("DATABASE_PUBLIC_URL") or
    ""
)
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

print(f"[STARTUP] DB: {'postgres' if DATABASE_URL else 'memory'}", flush=True)
print(f"[STARTUP] URL prefix: {DATABASE_URL[:30] if DATABASE_URL else 'none'}", flush=True)

# ── PostgreSQL ─────────────────────────────────────────────────────────────────

def _conn():
    if not DATABASE_URL:
        return None
    try:
        import psycopg2
        return psycopg2.connect(DATABASE_URL)
    except Exception as e:
        print(f"DB error: {e}")
        return None

def init_db():
    conn = _conn()
    if not conn: return
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS bot_state (
                key        TEXT PRIMARY KEY,
                value      TEXT NOT NULL,
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        conn.commit()
        cur.close()
    except Exception as e:
        print(f"DB init: {e}")
    finally:
        conn.close()

def db_set(key: str, value: dict):
    conn = _conn()
    if not conn: return
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO bot_state (key, value, updated_at) VALUES (%s, %s, NOW())
            ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()
        """, (key, json.dumps(value)))
        conn.commit()
        cur.close()
    except Exception as e:
        print(f"DB set: {e}")
    finally:
        conn.close()

def db_get(key: str):
    conn = _conn()
    if not conn: return None
    try:
        cur = conn.cursor()
        cur.execute("SELECT value FROM bot_state WHERE key=%s", (key,))
        row = cur.fetchone()
        cur.close()
        return json.loads(row[0]) if row else None
    except Exception as e:
        print(f"DB get: {e}")
        return None
    finally:
        conn.close()

def db_last_push():
    conn = _conn()
    if not conn: return None
    try:
        cur = conn.cursor()
        # Prefer dedicated last_push key (string ISO timestamp, written on every push)
        cur.execute("SELECT value FROM bot_state WHERE key='last_push'")
        row = cur.fetchone()
        if row:
            data = json.loads(row[0])
            ts = data.get("ts")
            cur.close()
            return ts
        # Fallback: use updated_at of overview row
        cur.execute("SELECT updated_at FROM bot_state WHERE key='overview'")
        row = cur.fetchone()
        cur.close()
        return row[0] if row else None
    except Exception as e:
        print(f"DB last_push error: {e}")
        return None
    finally:
        conn.close()

init_db()

INDEX_PATH = os.path.join(os.path.dirname(__file__), "index.html")

# ── In-memory fallback ─────────────────────────────────────────────────────────
_mem: dict = {}
_MEM_MAX_KEYS = 100   # защита от неограниченного роста

def store_set(key: str, value):
    if key not in _mem and len(_mem) >= _MEM_MAX_KEYS:
        oldest = next(iter(_mem))
        del _mem[oldest]
    _mem[key] = value
    db_set(key, value if isinstance(value, dict) else {"_list": value})

def store_get(key: str):
    if DATABASE_URL:
        val = db_get(key)
        if val is not None:
            if isinstance(val, dict) and "_list" in val:
                return val["_list"]
            return val
    return _mem.get(key)

# ── Auth ───────────────────────────────────────────────────────────────────────
def _bot_auth(x_bot_key: str = Header(default="")):
    if not API_KEY:
        raise HTTPException(503, "DASHBOARD_API_KEY is not configured")
    if not secrets.compare_digest(x_bot_key, API_KEY):
        raise HTTPException(401, "Invalid bot key")

def _manual_close_auth(x_manual_close_key: str = Header(default="")):
    if not MANUAL_CLOSE_KEY:
        raise HTTPException(503, "MANUAL_CLOSE_KEY is not configured")
    if not secrets.compare_digest(x_manual_close_key, MANUAL_CLOSE_KEY):
        raise HTTPException(401, "Invalid manual close key")

def _alive() -> bool:
    t = None
    if DATABASE_URL:
        t = db_last_push()
    if t is None:
        t = _mem.get("last_push")
    if not t: return False
    try:
        if isinstance(t, str):
            t = datetime.fromisoformat(t.replace("Z","+00:00"))
        if not t.tzinfo:
            t = t.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - t).total_seconds() < 1800
    except Exception as e:
        print(f"_alive() error: {e}")
        return False

# ── Push ───────────────────────────────────────────────────────────────────────
class Push(BaseModel):
    overview:    Optional[dict] = None
    signals:     Optional[list] = None
    positions:   Optional[dict] = None
    smart_money: Optional[dict] = None
    learning:    Optional[dict] = None

@app.post("/internal/push")
def push(p: Push, _=Depends(_bot_auth)):
    if p.overview:
        store_set("overview", p.overview)
    if p.signals is not None:
        store_set("signals", {"_list": p.signals})
    if p.positions:
        store_set("positions", p.positions)
    if p.smart_money:
        store_set("smart_money", p.smart_money)
    if p.learning:
        store_set("learning", p.learning)
    now_str = datetime.now(timezone.utc).isoformat()
    _mem["last_push"] = now_str
    store_set("last_push", {"ts": now_str})
    return {"ok": True}

# ── Read ───────────────────────────────────────────────────────────────────────
@app.get("/api/health")
def health():
    t = db_last_push() if DATABASE_URL else _mem.get("last_push")
    return {
        "ok": True,
        "bot_alive": _alive(),
        "last_push": t.isoformat() if hasattr(t, "isoformat") else t,
        "db": "postgres" if DATABASE_URL else "memory",
        "ts": datetime.now(timezone.utc).isoformat(),
    }

@app.get("/api/debug/telegram")
def debug_telegram(_=Depends(_bot_auth)):
    """
    Проверить работу Telegram прямо из браузера.
    Открой: https://<твой-api>.railway.app/api/debug/telegram
    """
    token   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

    if not token:
        return {"ok": False, "error": "TELEGRAM_BOT_TOKEN не задан в Railway Variables"}
    if not chat_id:
        return {"ok": False, "error": "TELEGRAM_CHAT_ID не задан в Railway Variables"}

    # Проверяем токен
    try:
        import requests as _req
        r = _req.get(f"https://api.telegram.org/bot{token}/getMe", timeout=5)
        if r.status_code != 200:
            return {"ok": False, "error": f"Невалидный токен: HTTP {r.status_code}"}
        bot_name = r.json().get("result", {}).get("username", "?")
    except Exception as e:
        return {"ok": False, "error": f"Не могу достучаться до Telegram API: {e}"}

    # Отправляем тестовое сообщение
    try:
        r2 = _req.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id":    chat_id,
                "text":       "✅ *Prometheus — тест связи*\n\nTelegram работает!",
                "parse_mode": "Markdown",
            },
            timeout=8,
        )
        if r2.status_code == 200:
            return {
                "ok":       True,
                "bot":      f"@{bot_name}",
                "chat_id":  chat_id,
                "message":  "Тестовое сообщение отправлено — проверь Telegram",
            }
        else:
            err = r2.json().get("description", r2.text[:100])
            return {
                "ok":      False,
                "bot":     f"@{bot_name}",
                "error":   f"sendMessage failed: {err}",
                "hint":    "Скорее всего неверный TELEGRAM_CHAT_ID",
            }
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/api/overview")
def overview(request: Request):
    if _rate_limited(_client_ip(request), limit=60, window=60):
        raise HTTPException(429, "Too many requests")
    d = store_get("overview")
    if d:
        d = dict(d)
        d["bot_running"] = _alive()
        return d
    return {
        "bot_running": _alive(), "dry_run": True,
        "pnl_today": 0, "pnl_total": 0, "unrealised_pnl": 0,
        "win_rate": 0, "total_trades": 0, "open_positions": 0,
        "open_exposure": 0, "bankroll": float(os.environ.get("BANKROLL", "100")),
        "portfolio_value": 0, "total_invested": 0, "poly_sync_at": None,
        "daily_loss_used": 0, "signals_today": 0, "smart_money_today": 0,
        "poly_configured": bool(POLY_FUNDER),
    }

@app.get("/api/signals")
def signals(request: Request, limit: int = 30):
    if _rate_limited(_client_ip(request), limit=60, window=60):
        raise HTTPException(429, "Too many requests")
    d = store_get("signals")
    if isinstance(d, list):
        return {"signals": d[:limit]}
    if isinstance(d, dict) and "_list" in d:
        return {"signals": d["_list"][:limit]}
    return {"signals": []}

@app.get("/api/positions")
def positions(request: Request):
    if _rate_limited(_client_ip(request), limit=60, window=60):
        raise HTTPException(429, "Too many requests")
    return store_get("positions") or {"open": [], "closed_today": [], "history": []}

@app.post("/api/close_position")
async def close_position(request: Request, _=Depends(_manual_close_auth)):
    """Закрыть позицию вручную из дашборда. Корректно обновляет overview P&L."""
    ip = _client_ip(request)
    if _rate_limited(f"close:{ip}", limit=10, window=60):
        raise HTTPException(429, "Too many requests — подождите минуту")

    body = await request.json()
    market_id = body.get("market_id", "").strip()
    if not market_id:
        return {"ok": False, "error": "no market_id"}
    if not _MARKET_ID_RE.match(market_id):
        return {"ok": False, "error": "invalid market_id format"}

    pos_data = store_get("positions") or {"open": [], "closed_today": [], "history": []}
    open_pos = pos_data.get("open", [])
    found = None

    for p in open_pos:
        if p.get("id") == market_id or p.get("market_id") == market_id:
            found = p
            break

    if not found:
        return {"ok": False, "error": "position not found"}

    # P&L расчёт с поддержкой YES и NO
    cur_price = float(found.get("current_price") or found.get("price", 0))
    entry     = float(found.get("price", 0))
    size      = float(found.get("size", 0))
    direction = found.get("direction", "YES")

    if direction == "YES":
        entry_token   = entry
        current_token = cur_price
    else:
        entry_token   = 1.0 - entry
        current_token = 1.0 - cur_price

    if entry_token > 0:
        pnl = round((current_token - entry_token) / entry_token * size, 2)
    else:
        pnl = 0.0

    now_iso = datetime.now(timezone.utc).isoformat()
    found["status"]    = "closed"
    found["closed_at"] = now_iso
    found["exit_price"]= cur_price
    found["pnl"]       = pnl

    new_open = [p for p in open_pos
                if p.get("id") != market_id and p.get("market_id") != market_id]
    history  = pos_data.get("history", [])
    history.append(found)

    pos_data["open"]         = new_open
    pos_data["history"]      = history
    pos_data["closed_today"] = pos_data.get("closed_today", []) + [found]
    store_set("positions", pos_data)

    # FIX: обновляем overview P&L чтобы отразить ручное закрытие
    ov = store_get("overview") or {}
    if ov:
        ov = dict(ov)
        ov["pnl_today"]      = round(float(ov.get("pnl_today", 0)) + pnl, 2)
        ov["pnl_total"]      = round(float(ov.get("pnl_total", 0)) + pnl, 2)
        ov["open_positions"] = max(0, int(ov.get("open_positions", 1)) - 1)
        ov["open_exposure"]  = round(max(0.0, float(ov.get("open_exposure", size)) - size), 2)
        ov["total_trades"]   = int(ov.get("total_trades", 0)) + 1
        # Пересчёт win_rate из реальной истории — надёжнее чем обратная реконструкция
        all_history = pos_data.get("history", [])
        if all_history:
            wins = sum(1 for p in all_history if float(p.get("pnl") or 0) > 0)
            ov["win_rate"] = round(wins / len(all_history), 3)
        store_set("overview", ov)

    return {"ok": True, "pnl": pnl}

@app.get("/api/overview/recalc")
def overview_recalc():
    """
    Пересчитать pnl_today и pnl_total из истории позиций.
    Вызывать если P&L на дашборде рассинхронизировался.
    """
    pos_data = store_get("positions") or {}
    history  = pos_data.get("history", [])
    today    = datetime.now(timezone.utc).date().isoformat()

    pnl_total = sum(float(p.get("pnl") or 0) for p in history)
    pnl_today = sum(
        float(p.get("pnl") or 0) for p in history
        if (p.get("closed_at") or "")[:10] == today
    )
    wins      = sum(1 for p in history if float(p.get("pnl") or 0) > 0)
    win_rate  = round(wins / len(history), 3) if history else 0.0

    ov = store_get("overview") or {}
    if ov:
        ov = dict(ov)
        ov["pnl_today"]   = round(pnl_today, 2)
        ov["pnl_total"]   = round(pnl_total, 2)
        ov["total_trades"]= len(history)
        ov["win_rate"]    = win_rate
        store_set("overview", ov)

    return {
        "ok": True,
        "pnl_today":    round(pnl_today, 2),
        "pnl_total":    round(pnl_total, 2),
        "total_trades": len(history),
        "win_rate":     win_rate,
    }

_poly_live_cache: dict = {"data": None, "ts": 0.0}
_POLY_LIVE_TTL = 15  # seconds — server-side cache to avoid hammering Polymarket

@app.get("/api/polymarket_live")
def polymarket_live(request: Request):
    """
    Live position data fetched directly from Polymarket Data API.
    Server-side cached for 15 s. Requires POLYMARKET_FUNDER env var.
    Returns positions with real-time cur_price, cashPnl, percentPnl.
    """
    if _rate_limited(_client_ip(request), limit=30, window=60):
        raise HTTPException(429, "Too many requests")

    if not POLY_FUNDER:
        return {
            "configured": False, "positions": [], "portfolio_value": 0,
            "total_invested": 0, "unrealised_pnl": 0, "position_count": 0,
        }

    now = time.time()
    if _poly_live_cache["data"] and now - _poly_live_cache["ts"] < _POLY_LIVE_TTL:
        return _poly_live_cache["data"]

    try:
        import requests as _req
        r = _req.get(
            "https://data-api.polymarket.com/positions",
            params={"user": POLY_FUNDER, "sizeThreshold": "0.01"},
            timeout=8,
        )
        if r.status_code != 200:
            cached = _poly_live_cache["data"]
            return cached or {"configured": True, "error": f"HTTP {r.status_code}", "positions": []}

        positions = []
        for item in r.json():
            try:
                size = float(item.get("size", 0))
                if size < 0.01:
                    continue
                positions.append({
                    "token_id":      str(item.get("asset") or item.get("token_id", "")),
                    "market_id":     str(item.get("conditionId", "")),
                    "question":      str(item.get("title", "")),
                    "outcome":       str(item.get("outcome", "")).upper(),
                    "size":          round(size, 4),
                    "avg_price":     round(float(item.get("avgPrice", 0)), 4),
                    "cur_price":     round(float(item.get("curPrice", 0)), 4),
                    "initial_value": round(float(item.get("initialValue", 0)), 2),
                    "current_value": round(float(item.get("currentValue", 0)), 2),
                    "pnl":           round(float(item.get("cashPnl", 0)), 2),
                    "pnl_pct":       round(float(item.get("percentPnl", 0)), 4),
                })
            except (ValueError, TypeError, KeyError):
                continue

        result = {
            "configured":      True,
            "positions":       positions,
            "portfolio_value": round(sum(p["current_value"] for p in positions), 2),
            "total_invested":  round(sum(p["initial_value"] for p in positions), 2),
            "unrealised_pnl":  round(sum(p["pnl"] for p in positions), 2),
            "position_count":  len(positions),
            "synced_at":       datetime.now(timezone.utc).strftime("%H:%M:%S UTC"),
        }
        _poly_live_cache["data"] = result
        _poly_live_cache["ts"] = now
        return result
    except Exception as e:
        print(f"polymarket_live error: {e}")
        cached = _poly_live_cache["data"]
        return cached or {"configured": True, "error": str(e)[:120], "positions": []}

@app.get("/api/smart_money")
def smart_money():
    return store_get("smart_money") or {"traders": []}

@app.get("/api/learning")
def learning():
    return store_get("learning") or {"signal_stats": {}}

@app.get("/api/audit")
def audit():
    return {"entries": []}



@app.get("/")
def dashboard_index():
    if os.path.exists(INDEX_PATH):
        return FileResponse(INDEX_PATH)
    raise HTTPException(404, "Dashboard not found")

@app.get("/api/settings")
def settings():
    return {
        "risk": {
            "bankroll": float(os.environ.get("BANKROLL", "100") or 100),
            "max_position_usd": float(os.environ.get("MAX_POSITION_USD", os.environ.get("MAX_POS_USD", "10")) or 10),
            "min_position_usd": float(os.environ.get("MIN_POSITION_USD", os.environ.get("MIN_POS_USD", "1")) or 1),
            "max_open_positions": int(os.environ.get("MAX_OPEN_POSITIONS", "10") or 10),
            "max_daily_loss_usd": float(os.environ.get("MAX_DAILY_LOSS_USD", "50") or 50),
        },
        "engine": {
            "scan_interval_sec": int(os.environ.get("SCAN_INTERVAL_SEC", "60") or 60),
            "ai_mode": os.environ.get("AI_MODE", "minimal"),
            "min_trade_quality": int(os.environ.get("MIN_TRADE_QUALITY", "60") or 60),
            "enable_near_resolution": str(os.environ.get("ENABLE_NEAR_RESOLUTION", "false")).lower() == "true",
        },
        "strategy_control": {
            "strategy_min_trades": int(os.environ.get("STRATEGY_MIN_TRADES", "10") or 10),
            "strategy_weak_win_rate": float(os.environ.get("STRATEGY_WEAK_WIN_RATE", "0.45") or 0.45),
            "strategy_reenable_days": int(os.environ.get("STRATEGY_REENABLE_DAYS", "7") or 7),
            "strategy_weakened_size_mult": float(os.environ.get("STRATEGY_WEAKENED_SIZE_MULT", "0.5") or 0.5),
            "all_strategies_weak_min_usd": float(os.environ.get("ALL_STRATEGIES_WEAK_MIN_USD", "1") or 1),
        },
        "security": {
            "dashboard_key_set": bool(API_KEY),
            "manual_close_key_set": bool(MANUAL_CLOSE_KEY),
        },
    }

@app.get("/api/strategies")
def strategies():
    learning_data = store_get("learning") or {}
    return {"strategies": (learning_data.get("strategy_stats") or {})}

# ── Admin ──────────────────────────────────────────────────────────────────────

@app.post("/api/admin/reset")
def admin_reset(_=Depends(_bot_auth)):
    """
    Сброс paper trading состояния:
    - bankroll → $100
    - все P&L → 0
    - все позиции и история → пусто
    """
    default_overview = {
        "bot_running":       _alive(),
        "dry_run":           True,
        "pnl_today":         0.0,
        "pnl_total":         0.0,
        "unrealised_pnl":    0.0,
        "win_rate":          0.0,
        "total_trades":      0,
        "open_positions":    0,
        "open_exposure":     0.0,
        "bankroll":          100.0,
        "daily_loss_used":   0.0,
        "signals_today":     0,
        "smart_money_today": 0,
    }
    empty_positions = {
        "open":         [],
        "closed_today": [],
        "history":      [],
    }

    store_set("overview",  default_overview)
    store_set("positions", empty_positions)
    store_set("signals",   {"_list": []})

    _mem["last_push"] = datetime.now(timezone.utc).isoformat()

    return {
        "ok":      True,
        "message": "Paper trading reset: balance=$100.00, all P&L and positions cleared",
        "bankroll":  100.0,
        "pnl_today": 0.0,
        "pnl_total": 0.0,
    }
