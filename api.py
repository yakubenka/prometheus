"""
Prometheus — API v3
PostgreSQL хранилище. Данные не теряются при деплое.
"""
import json
import os
import secrets
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, Depends, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="Prometheus", version="3.2")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

API_KEY      = os.environ.get("DASHBOARD_API_KEY", "")
# Пробуем все возможные названия переменной
DATABASE_URL = (
    os.environ.get("DATABASE_URL") or
    os.environ.get("database_url") or
    os.environ.get("DATABASE_PUBLIC_URL") or
    ""
)
# psycopg2 требует postgresql:// а не postgres://
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
        conn.commit(); cur.close(); conn.close()
    except Exception as e:
        print(f"DB init: {e}")

def db_set(key: str, value: dict):
    conn = _conn()
    if not conn: return
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO bot_state (key, value, updated_at) VALUES (%s, %s, NOW())
            ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()
        """, (key, json.dumps(value)))
        conn.commit(); cur.close(); conn.close()
    except Exception as e:
        print(f"DB set: {e}")

def db_get(key: str):
    conn = _conn()
    if not conn: return None
    try:
        cur = conn.cursor()
        cur.execute("SELECT value FROM bot_state WHERE key=%s", (key,))
        row = cur.fetchone()
        cur.close(); conn.close()
        return json.loads(row[0]) if row else None
    except Exception as e:
        print(f"DB get: {e}")
        return None

def db_last_push():
    conn = _conn()
    if not conn: return None
    try:
        cur = conn.cursor()
        cur.execute("SELECT updated_at FROM bot_state WHERE key='overview'")
        row = cur.fetchone()
        cur.close(); conn.close()
        return row[0] if row else None
    except:
        return None

init_db()

# ── In-memory fallback ─────────────────────────────────────────────────────────
_mem: dict = {}

def store_set(key: str, value):
    _mem[key] = value
    db_set(key, value if isinstance(value, dict) else {"_list": value})

def store_get(key: str):
    if DATABASE_URL:
        val = db_get(key)
        if val is not None:
            if "_list" in val: return val["_list"]
            return val
    return _mem.get(key)

# ── Auth ───────────────────────────────────────────────────────────────────────
def _bot_auth(x_bot_key: str = Header(default="")):
    if API_KEY and not secrets.compare_digest(x_bot_key, API_KEY):
        raise HTTPException(401, "Invalid bot key")

def _alive() -> bool:
    t = db_last_push() if DATABASE_URL else _mem.get("last_push")
    if not t: return False
    try:
        if isinstance(t, str):
            t = datetime.fromisoformat(t.replace("Z","+00:00"))
        if not t.tzinfo:
            t = t.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - t).total_seconds() < 1800
    except:
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
    if p.overview:    store_set("overview", p.overview)
    if p.signals is not None: store_set("signals", {"_list": p.signals})
    if p.positions:   store_set("positions", p.positions)
    if p.smart_money: store_set("smart_money", p.smart_money)
    if p.learning:    store_set("learning", p.learning)
    _mem["last_push"] = datetime.now(timezone.utc).isoformat()
    return {"ok": True}

# ── Read ───────────────────────────────────────────────────────────────────────
@app.get("/api/health")
def health():
    t = db_last_push() if DATABASE_URL else _mem.get("last_push")
    return {"ok": True, "bot_alive": _alive(),
            "last_push": t.isoformat() if hasattr(t,"isoformat") else t,
            "db": "postgres" if DATABASE_URL else "memory",
            "ts": datetime.utcnow().isoformat()}

@app.get("/api/overview")
def overview():
    d = store_get("overview")
    if d:
        d = dict(d); d["bot_running"] = _alive(); return d
    return {"bot_running":_alive(),"dry_run":True,"pnl_today":0,"pnl_total":0,
            "win_rate":0,"total_trades":0,"open_positions":0,"open_exposure":0,
            "bankroll":float(os.environ.get("BANKROLL","100")),"daily_loss_used":0,
            "signals_today":0,"smart_money_today":0}

@app.get("/api/signals")
def signals(limit: int = 30):
    d = store_get("signals")
    if isinstance(d, list): return {"signals": d[:limit]}
    if isinstance(d, dict) and "_list" in d: return {"signals": d["_list"][:limit]}
    return {"signals": []}

@app.get("/api/positions")
def positions():
    return store_get("positions") or {"open":[],"closed_today":[]}

@app.post("/api/close_position")
async def close_position(request: Request):
    """Закрыть позицию вручную из дашборда."""
    body = await request.json()
    market_id = body.get("market_id","")
    if not market_id:
        return {"ok": False, "error": "no market_id"}

    pos_data = store_get("positions") or {"open":[], "closed_today":[], "history":[]}
    open_pos  = pos_data.get("open", [])
    found     = None

    for p in open_pos:
        if p.get("id") == market_id or p.get("market_id") == market_id:
            found = p
            break

    if not found:
        return {"ok": False, "error": "position not found"}

    # Закрываем по текущей цене
    cur_price = found.get("current_price") or found.get("price", 0)
    entry     = found.get("price", 0)
    size      = found.get("size", 0)
    direction = found.get("direction","YES")

    # P&L
    if direction == "YES":
        pnl = round((float(cur_price) - float(entry)) / max(float(entry), 0.01) * float(size), 2)
    else:
        pnl = round((float(entry) - float(cur_price)) / max(1 - float(entry), 0.01) * float(size), 2)

    found["status"]    = "closed"
    found["closed_at"] = datetime.now(timezone.utc).isoformat()
    found["exit_price"]= cur_price
    found["pnl"]       = pnl

    # Обновляем данные
    new_open    = [p for p in open_pos if p.get("id") != market_id and p.get("market_id") != market_id]
    history     = pos_data.get("history", [])
    history.append(found)

    pos_data["open"]         = new_open
    pos_data["history"]      = history
    pos_data["closed_today"] = pos_data.get("closed_today", []) + [found]
    store_set("positions", pos_data)

    return {"ok": True, "pnl": pnl}

@app.get("/api/smart_money")
def smart_money():
    return store_get("smart_money") or {"traders":[]}

@app.get("/api/learning")
def learning():
    return store_get("learning") or {"signal_stats":{}}

@app.get("/api/audit")
def audit():
    return {"entries":[]}

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

    # Сброс in-memory P&L
    _mem["last_push"] = datetime.now(timezone.utc).isoformat()

    return {
        "ok":      True,
        "message": "Paper trading reset: balance=$100.00, all P&L and positions cleared",
        "bankroll": 100.0,
        "pnl_today": 0.0,
        "pnl_total": 0.0,
    }
