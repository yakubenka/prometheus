"""
Prometheus v3 — API
Отдаёт реальные данные дашборду. Быстро. Надёжно.
"""
import json
import os
import re
import secrets
from datetime import datetime, date, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Prometheus", version="3.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

LOGS    = Path(os.environ.get("LOGS_DIR", "/app/logs"))
API_KEY = os.environ.get("DASHBOARD_API_KEY", "")


def _auth(x_api_key: str = Header(default="")):
    """Простая API-key авторизация. Если ключ не задан — открытый доступ (dev)."""
    if API_KEY and not secrets.compare_digest(x_api_key, API_KEY):
        raise HTTPException(status_code=401, detail="Invalid API key")


def _read(path: Path, fallback):
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception:
        pass
    return fallback


def _time_ago(iso: Optional[str]) -> str:
    if not iso:
        return "—"
    try:
        dt    = datetime.fromisoformat(iso.replace("Z","+00:00"))
        delta = int((datetime.now(timezone.utc) - dt).total_seconds())
        if delta < 3600:  return f"{delta//60}m"
        if delta < 86400: return f"{delta//3600}h"
        return f"{delta//86400}d"
    except Exception:
        return "—"


def _bot_alive() -> bool:
    if not (LOGS/"prometheus.log").exists():
        return False
    try:
        lines = (LOGS/"prometheus.log").read_text(errors="ignore").splitlines()
        for line in reversed(lines[-30:]):
            m = re.search(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
            if m:
                dt    = datetime.fromisoformat(m.group(1))
                delta = (datetime.utcnow() - dt).total_seconds()
                return delta < 600
    except Exception:
        pass
    return False


@app.get("/api/overview")
def overview(_=Depends(_auth)):
    positions = _read(LOGS/"positions.json", [])
    today = date.today().isoformat()

    closed       = [p for p in positions if p.get("status") == "closed"]
    open_        = [p for p in positions if p.get("status") == "open"]
    today_closed = [p for p in closed if (p.get("closed_at") or "")[:10] == today]

    pnl_today = sum(p.get("pnl") or 0 for p in today_closed)
    pnl_total = sum(p.get("pnl") or 0 for p in closed)
    wins      = [p for p in closed if (p.get("pnl") or 0) > 0]

    # Из trades.json — дополнительная статистика
    trades = _read(LOGS/"trades.json", [])
    signals_today    = sum(1 for t in trades if (t.get("time",""))[:10] == today)
    sm_today         = sum(1 for t in trades if (t.get("time",""))[:10] == today
                           and "smart_money" in (t.get("tags") or []))

    return {
        "bot_running":        _bot_alive(),
        "dry_run":            os.environ.get("DRY_RUN","true").lower() == "true",
        "pnl_today":          round(pnl_today, 2),
        "pnl_total":          round(pnl_total, 2),
        "win_rate":           round(len(wins)/len(closed), 3) if closed else 0,
        "total_trades":       len(closed),
        "open_positions":     len(open_),
        "open_exposure":      round(sum(p.get("size_usd",0) for p in open_), 2),
        "bankroll":           float(os.environ.get("BANKROLL","100")),
        "daily_loss_used":    round(abs(min(0, pnl_today)) / 50, 3),
        "signals_today":      signals_today,
        "smart_money_today":  sm_today,
    }


@app.get("/api/positions")
def positions(_=Depends(_auth)):
    all_pos = _read(LOGS/"positions.json", [])
    today   = date.today().isoformat()
    open_   = [p for p in all_pos if p.get("status") == "open"]
    closed  = [p for p in all_pos if p.get("status") == "closed"
               and (p.get("closed_at",""))[:10] == today]

    def fmt(p):
        return {
            "id":        p.get("market_id",""),
            "question":  p.get("question",""),
            "direction": p.get("direction",""),
            "price":     p.get("entry_price",0),
            "size":      p.get("size_usd",0),
            "pnl":       p.get("pnl",0) or 0,
            "age":       _time_ago(p.get("opened_at") or p.get("closed_at")),
            "tags":      p.get("tags",[]),
            "status":    p.get("status","open"),
            "type":      p.get("signal_type","ai"),
        }

    return {
        "open":         [fmt(p) for p in open_],
        "closed_today": [fmt(p) for p in closed],
    }


@app.get("/api/signals")
def signals(limit: int = 30, _=Depends(_auth)):
    trades = _read(LOGS/"trades.json", [])
    result = []
    for t in reversed(trades[-100:]):
        result.append({
            "id":        t.get("time",""),
            "time":      (t.get("time",""))[-8:-3] or "—",
            "type":      "smart_money" if "smart_money" in (t.get("tags") or []) else "ensemble",
            "question":  t.get("market",""),
            "direction": t.get("direction","—"),
            "price":     t.get("price",0),
            "size":      t.get("size_usd",0),
            "edge":      t.get("edge",0),
            "confidence":t.get("confidence",""),
            "detail":    t.get("reasoning",""),
        })
    return {"signals": result[:limit]}


@app.get("/api/smart_money")
def smart_money(_=Depends(_auth)):
    data = _read(LOGS/"smart_money.json", {"traders":[]})
    return data


@app.get("/api/backtest")
def backtest(_=Depends(_auth)):
    data = _read(LOGS/"backtest_result.json", None)
    if not data:
        raise HTTPException(404, "Backtest not run yet")
    return data


@app.get("/api/audit")
def audit(limit: int = 50, _=Depends(_auth)):
    path = LOGS / "audit.jsonl"
    if not path.exists():
        return {"entries": []}
    lines = path.read_text(errors="ignore").splitlines()[-limit:]
    entries = []
    for line in reversed(lines):
        try:
            entries.append(json.loads(line))
        except Exception:
            pass
    return {"entries": entries}


@app.get("/api/learning")
def learning(_=Depends(_auth)):
    """Точность каждого сигнала — для дашборда."""
    from learning import LearningEngine
    eng = LearningEngine(str(LOGS))
    return {"signal_stats": eng.stats(), "weights_path": str(LOGS/"weights_history.jsonl")}


@app.get("/api/health")
def health():
    return {"ok": True, "bot_alive": _bot_alive(), "ts": datetime.utcnow().isoformat()}
