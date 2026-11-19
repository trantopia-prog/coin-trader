# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"   # Ubuntu: needs python3.X-venv package
.venv/bin/python -m app                    # run server (HOST/PORT from .env, default 127.0.0.1:8000)
.venv/bin/uvicorn app.main:app --reload    # dev server
.venv/bin/pytest                           # all offline tests (live_quote excluded by default via addopts)
.venv/bin/pytest tests/test_trading.py::test_grid_buy_and_sell_lot   # single test
.venv/bin/pytest -m live_quote             # hits real Upbit quotation API
```

Windows distribution: `packaging/build_windows.bat` (on Windows) or `.github/workflows/build-windows.yml` runs PyInstaller (`packaging/coin_trader.spec`, onedir, entry `app/launcher.py`) and then Inno Setup (`packaging/installer.iss`). The spec can be smoke-built on Linux with `.venv/bin/python -m PyInstaller packaging/coin_trader.spec --distpath /tmp/x/dist --workpath /tmp/x/build`. When frozen, `app/config.py` reads resources from `sys._MEIPASS` and user data/`.env` from `%LOCALAPPDATA%\CoinTrader`. `COIN_TRADER_HOME` overrides both the data dir and the `.env` location, also in dev. `COIN_TRADER_NO_BROWSER=1` skips opening the browser.
- `app/launcher.py` runs the windowed exe (`console=False`):
  - uvicorn runs in a background thread (`ServerThread`), and the pystray tray icon (`TrayApp`) owns the main thread.
  - Tray actions call into the server loop via `run_coroutine_threadsafe`.
  - stdout/stderr go to `<data>/logs/coin-trader.log` when there is no console.
  - `instance.json` enforces a single instance.
  - The first-run password uses a tkinter dialog.
- Launcher flags:
  - `--console`: no tray; used by CI, which has no desktop session.
  - `--no-browser`: used by the HKCU Run autostart entry. The installer's autostart task writes the same value.
  - `--self-test`: checks the bundled modules and resources, then exits.
- `tests/test_launcher.py` drives the launcher in subprocesses with a fake `pystray.Icon`, so no real tray icon appears. Keep `installer.iss` UTF-8 **with BOM** and the `.bat` with **CRLF** line endings.

No Node/build step: the frontend is static HTML in `web/` using Alpine.js and lightweight-charts@4.2.0 from CDN, served by FastAPI `StaticFiles`.

## Architecture

FastAPI app (`app/main.py`) wires every service in `build_services()` and stores the result as `app.state.svc` (`app/services.py:Services`). Routes get it via `Depends(get_svc)`. The lifespan starts three long-running parts: the Upbit WebSocket client, the `TradingEngine` loops, and the `NewsService` collectors.

**Event flow**: `upbit/websocket.py` → `events.EventBus` (in-process pub/sub that also caches the last ticker/orderbook per market) → consumers: the engine (prices), `PaperBroker` (limit-order matching), and `api/ws.py` (pushes to the browser). Topics: `ticker`, `orderbook`, `trade`, `order`, `order_update`, `log`, `news`, `engine`, `system`.

**Trading pipeline** (`trading/engine.py`): every `ENGINE_EVAL_SEC` the engine evaluates each enabled `StrategyConfig`:
1. Fetch candles and overwrite the last (in-progress) bar's close/high/low with the live price.
2. Call `strategy.on_bar(df, ctx)`, which returns `Signal`s.
3. Clamp each signal to the strategy budget, then run `RiskManager.check`.
4. Place the order via `broker.place` and record an `Order` row.
5. Update the per-strategy ledger (`StrategyRuntime`) and call `strategy.on_fill`.

- Strategies (`trading/strategy/`) never place orders. They are pure. `ctx.state` is a dict persisted to `StrategyRuntime.state`; use it for cross-call memory, and use `Signal.tag` to round-trip identifiers to `on_fill` (e.g. grid cell index, bar id).
- `backtest/runner.py` runs the **same strategy classes** by replaying each bar as a 4-point intrabar path (O→L→H→C or O→H→L→C). Keep `on_bar` semantics valid for both live and backtest.
  - Set `Signal.price` to the trigger level (breakout target, grid level, band, stop price). The backtest fills at that price when the path segment crosses it; otherwise it would always fill at the bar's high/low. Live trading ignores it and sends market orders.
  - Set `Signal.cost_price` on sells when the strategy tracks per-lot cost (grid). Realized PnL in both the engine and the backtest then uses the lot's cost instead of the blended average.
- To register a new strategy: subclass `Strategy`, declare `params_spec` (`Param` list; the UI form and validation are generated from it), and add it to `STRATEGIES` in `trading/strategy/__init__.py`.

**Paper vs live**: `PaperBroker` and `UpbitBroker` implement the same `Broker` interface. Switching modes only swaps `engine.broker`. Going live requires `.env` `TRADING_MODE=live` **and** a password-confirmed `/api/mode` call. The armed state persists in `AppState["live_armed"]`, and startup reconciles strategy ledgers against `/accounts`. `StrategyRuntime` rows and `Order` rows are keyed by `mode`, so paper and live positions never mix.

**Risk** (`trading/risk.py`): only buys are blocked (when halted, coin flagged, over max order, or over position ratio). Sells are always allowed so positions can be exited. The daily loss limit resets at the KST date change. Upbit notices classified as `warning`/`delisting` by `news/base.classify_notice` call `risk.flag(symbol)`, and `release` unflags it.

**News** (`news/`): each `NewsCollector` runs in its own task, so one failure doesn't affect the others. Items are deduped by URL SHA-256. Coins are extracted from titles using the Upbit market list (`Screener.symbol_names`).

## Gotchas

- **SQLite single-writer**: keep DB sessions short. Never hold a session open (with pending writes) across `broker.*` calls or `engine.log()`. Those open their own sessions, and the result is `database is locked`. The engine deliberately uses load → compute → save with detached objects (`expire_on_commit=False`).
- SQLite drops tzinfo; serialize datetimes with `models.iso()`, which treats naive values as UTC.
- Upbit auth (`upbit/auth.py`): JWT is signed HS512. `query_hash` is SHA512 of the **unencoded** query string in the **same param order** as the actual request. Array params use `key[]=a&key[]=b`. POST bodies are hashed the same way, from the JSON dict's insertion order.
- Upbit rate limits are handled in `upbit/ratelimit.py` per group: `quotation` 10/s, `order` 8/s, `exchange` 30/s. Screener RSI scans are capped to the top N markets for this reason.
- KRW tick sizes and the 5,000 KRW minimum live in `trading/tick.py`, sourced from Upbit docs as of the 2025-07-31 policy. Strategy orders use market orders (`price`-type buy, `market`-type sell), so tick rounding only matters for manual limit orders.
- Frontend charts add `KST_OFFSET` (+9h) to UTC epoch seconds because lightweight-charts renders in UTC. Upbit day candles start at 00:00 UTC.
- The upbit notice endpoint (`api-manager.upbit.com`) is unofficial and may change.
