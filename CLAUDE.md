# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

Prometheus is a Polymarket prediction-market trading bot. It runs as two services:

- **bot** (`python main.py`) — the trading loop: scan markets, run signals, place/close orders, notify Telegram, learn from outcomes.
- **api** (`uvicorn api:app`) — FastAPI backend + static `index.html` dashboard. Receives state pushes from the bot and serves read endpoints for the UI.

Deployment target is Railway (see `railway.json`, `Procfile`), with Docker images for local/self-hosted runs (`Dockerfile`, `Dockerfile.api`, `Dockerfile.bot`, `docker-compose.yml`).

## Running and testing

```bash
pip install -r requirements.txt           # Python 3.12

# Run bot locally (reads .env via env vars)
python main.py

# Run API locally
uvicorn api:app --host 0.0.0.0 --port 8000

# Docker (both services + healthchecks)
docker-compose up

# One-time on-chain setup: USDC approvals for Polymarket contracts
python set_allowances.py
```

Tests use `pytest` (plus some `unittest`). No `pytest.ini` / `pyproject.toml` — tests are discovered by the default `test_*.py` pattern at the repo root.

```bash
pytest                                    # all tests
pytest test_signals.py                    # single file
pytest test_signals.py::TestName::test_x  # single test
pytest -k "pattern"                       # filter by name
```

Most tests mock `requests`, `anthropic`, `psycopg2`, and `py_clob_client` at import time (see the top of `test_data.py`, `test_resolver.py`, `test_signals.py`). When adding a new test that imports project modules, keep the same mocking pattern — the code eagerly constructs HTTP sessions and expects env vars at import.

`test_balance.py` is **not** a unit test: it is a diagnostic script that talks to the real Polymarket CLOB using `POLYMARKET_PRIVATE_KEY` / `POLYMARKET_FUNDER`. Don't run it under `pytest` in CI.

## Architecture

Flat module layout — every file is at the repo root and imports siblings directly (`from config import cfg`, `from data import Market`). There are no packages. When adding a module, add it at the root and import by bare module name.

### Bot pipeline (main.py `Prometheus` class)

The `run()` loop repeats every `SCAN_INTERVAL_SEC` (default 300s, shortened to ~30s for a few cycles after each trade — "fast cycle" mode):

1. `_maybe_daily_report` — daily Telegram summary at `DAILY_REPORT_HOUR`.
2. `_process_pending_orders` — retries for orders that failed on open/close; dead orders → `dead_letter.jsonl`.
3. `_resolve_positions` — `PositionResolver` checks closed markets, computes P&L, redeems winnings via CLOB.
4. `_review_positions` — `PositionReviewer` runs per-position take-profit / cut-loss / time-exit logic (cheap triggers first, AI second).
5. `_run_intel` — `IntelPipeline` + `ExtendedIntelPipeline` fetch news / RSS / Google Trends / economic calendar, dedup via SQLite.
6. `_check_risk_alerts` — emits Telegram alerts when approaching daily-loss or max-position limits.
7. `_trade_cycle` — screener → signal ensemble → risk checks → size → execute → register position → notify.
8. Breaking-news branch triggers an extra `_trade_cycle` without re-spamming Telegram.

### Signal flow (signals.py)

`SignalEngine.evaluate()` runs an **ensemble of 8 signals** (`SIGNAL_NAMES` in `learning.py`):
`momentum, consensus, predictit, volume_spike, ai_guard, sentiment, calibration, base_rate`.

Non-AI signals compute first; Claude (`_MODEL_FAST = claude-haiku-4-5-20251001`) is called only as a sanity check, budgeted by `AI_REQUEST_BUDGET_PER_CYCLE` and gated by `AI_MIN_QUALITY_FOR_CALL` / `AI_HIGH_QUALITY_SKIP`. `AI_MODE=off|minimal|full` controls this globally.

A trade requires: ensemble `quality >= MIN_TRADE_QUALITY`, domain-specific minimum edge (`_DOMAIN_MIN_EDGE` per category), external confirmation, and no imminent macro release (`econ_calendar.imminent_release_for`).

### Risk and sizing

`risk.RiskManager` enforces: daily loss limit, max open positions, duplicate-market guard, **correlation** (tag overlap) and **semantic correlation** (tokenized question similarity — see `_STOPWORDS`) caps, size bounds, and a pause toggle.

Sizing goes through **two Kelly stages**:
1. Per-position fractional Kelly (`KELLY_FRACTION`, default 0.20), scaled by `get_confidence_kelly_mult` and `liquidity_size_mult`.
2. `domain_intel.portfolio_kelly_size` re-scales based on the rest of the portfolio and the Bayesian domain prior (`DomainPrior` with α=β=2 — weak 50% prior to avoid overfitting).

`strategy_control.StrategyControl` keeps per-strategy win/ROI stats and can **weaken** (reduce size via `STRATEGY_WEAKENED_SIZE_MULT`) or **disable** underperforming strategies after `STRATEGY_MIN_TRADES`. If `all_primary_strategies_weak()` is true, sizing falls to `ALL_STRATEGIES_WEAK_MIN_USD`.

### Execution (main.py `_execute`)

Polymarket orders go through `py_clob_client`:
- `signature_type=0` EOA (default) — no funder needed.
- `signature_type=1/2` — requires `POLYMARKET_FUNDER`.
- L2 API creds are derived from the private key on every run (`create_or_derive_api_creds` + `set_api_creds`), with a ~2s sleep for activation.
- Orders are `MarketOrderArgs` with `OrderType.FAK`.
- `HTTPS_PROXY` / `HTTP_PROXY` env vars are respected for geo-restricted regions.
- Failures → `_dead_letter` writes to `{LOGS_DIR}/dead_letter.jsonl` and logs a warning; they are **not** retried automatically.

`DRY_RUN=true` (default) skips on-chain calls entirely and just logs the intended order. Live mode additionally requires `POLYMARKET_PRIVATE_KEY` and triggers extra startup validation in `cfg.validate()`.

### API / dashboard (api.py)

The bot POSTs state to `/internal/push` (auth: `X-Bot-Key` = `DASHBOARD_API_KEY`). The UI reads `/api/overview`, `/api/signals`, `/api/positions`, `/api/smart_money`, `/api/learning`, `/api/audit`, `/api/polymarket_live`, `/api/strategies`.

Storage is **PostgreSQL if `DATABASE_URL` is set, otherwise in-memory dict** (`_mem`, capped at 100 keys). Any `postgres://` URL is rewritten to `postgresql://`. Each call to `db_set`/`db_get` opens and closes its own connection — there is no pool, keep handlers short.

Rate limiting is a sliding-window, in-memory dict (`_rl_store`) with periodic GC — no Redis. Client IP comes from `X-Forwarded-For` first, then `request.client.host`.

Mutating endpoints (`/api/close_position`, `/api/admin/reset`) require `X-Manual-Close-Key` (=`MANUAL_CLOSE_KEY`), separate from the bot key.

`_alive()` returns true if the last push is < 30 min old. The Docker healthcheck in `docker-compose.yml` hits `/api/health`, and the bot image healthcheck asserts `/app/logs/prometheus.log` was touched < ~12 min ago.

### Learning loop (learning.py)

`LearningEngine.record(...)` appends one row per (signal × outcome) to `signals_learning.jsonl` (append-only, dedup via in-memory set — do **not** revert to the older "rewrite full file" pattern, it was O(n²)). `calc_weights()` produces new per-signal multipliers; `main.py` calls `load_last_weights()` on startup and feeds them into `SignalEngine.update_weights()`.

When adding a new signal, update both `SIGNAL_NAMES` in `learning.py` and the ensemble in `signals.py` — otherwise the signal won't be weighted.

### Intel pipeline (intel.py, intel_ext.py, domain_intel.py)

Fetches news / RSS / Google Trends / Wikipedia / Polymarket sentiment / (optionally) sports odds. Dedup uses a SQLite db in `LOGS_DIR`. Output is a list of typed `DataPoint`s consumed by `signals.SignalEngine`. `domain_intel.build_market_context` joins market metadata with domain stats; `DomainPrior` persists per-domain posterior win rates between runs.

Sports markets are intentionally blocked by the screener (`screener._BLOCKED_KEYWORDS`) — the bot does not bet on esports/live sports. If you add a keyword family, also check `liquidity.is_market_liquid` thresholds and the `_DOMAIN_MIN_EDGE` map.

### Smart money (smart_money.py)

`SmartMoneyMonitor.scan()` pulls large ($3k+) recent trades, enriches wallet history with final outcomes via Gamma API (closed market's outcomePrices → realized P&L), scores wallets by win-rate / ROI / specialization → produces "copy this insider now" signals. The core heuristic: insiders tend to bet on low-probability outcomes (5–20%).

## Conventions specific to this codebase

- **Comments and log messages are often in Russian**; identifiers, keys, and docstring summaries are in English. Preserve this style when editing existing modules — don't translate surrounding Russian prose for cosmetic reasons.
- **All configuration flows through `config.cfg`** (a singleton constructed at import). Do not read `os.environ` directly from business logic; add a field to `Config` and a helper (`_s`/`_f`/`_i`/`_b`) instead. Add validation to `Config.validate()` for any new invariant.
- **`cfg.dry_run` is the kill switch for on-chain side effects.** Any code path that sends an order, redeems a position, or moves USDC must check it first. `Config.validate()` refuses to start in LIVE mode without `POLYMARKET_PRIVATE_KEY`.
- **Persistent state lives in `LOGS_DIR`** (`/app/logs` in containers): `prometheus.log` (rotating, 10 MB × 5), `dead_letter.jsonl`, `signals_learning.jsonl`, SQLite dedup db, `domain_prior.json`, strategy stats. In Railway/Docker this path must be a mounted volume; otherwise state is lost on redeploy.
- **The API is the persistence layer for the dashboard, not for the bot.** The bot owns its own files in `LOGS_DIR`; it pushes snapshots to the API. Don't make the bot read from the API.
- **Retry policy**: `data.py` uses a shared `requests.Session` with `Retry(total=5, backoff=2)` for 5xx; 429 is handled manually. Don't add ad-hoc `time.sleep` retry loops — extend the session instead.
- **Market URLs**: always go through `data.best_polymarket_url` — it prefers saved `market_url`, falls back to slugs, and last-resorts to a search URL. Constructing `polymarket.com/event/<slug>` manually produces broken links for esports/dated markets (see `test_reconcile_links.py`).
- **Dead letters are audit records, not a retry queue.** `_process_pending_orders` handles retries via `PendingAction` in the risk manager; `dead_letter.jsonl` is write-only for humans.
