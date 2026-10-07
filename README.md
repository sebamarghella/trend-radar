# Trend Radar — GaussianChannel Strategy v3.1

Streamlit dashboard that runs the **GaussianChannel Strategy v3.1** (Donovan Wall Gaussian filter + Stoch RSI confluence, exit on close crossunder upper band) across the top 100 crypto by market cap, using **Binance** public OHLC.

## Setup

```powershell
cd trend_radar
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

## What it shows

- **Radar grid**: every top-100 coin tradable on Binance, with current strategy state (LONG / FLAT), Pine bar color, filter slope, bars in state, Stoch K, and distance from the upper band.
- **Low-Float tab**: broker-neutral US regular-session gainers discovered through Benzinga, locally validated to price $1–$20, session volume above 10K, and float 100K–20M. The grid retains Benzinga's scan price, day change, volume, and float alongside Yahoo-backed strategy signals. A visible warning appears if Benzinga reaches its current 500-row response ceiling.
- **Order-flow context**: crypto rows can now expose Binance taker buy/sell delta from the native kline payload, derived from `taker_buy_base_volume` vs total bar volume.
- **Drilldown**: per-coin price chart with the Gaussian channel overlaid and long-position bars highlighted.
- **Strategy comparison**: on a selected coin, compare two presets side by side with a head-to-head equity curve in drilldown.
- **Headline strip**: total coverage, % in long, % with rising filter, current timeframe, cache hit rate.

## Strategy rules (faithful to the Pine v6 source)

- **Filter**: N-pole Gaussian (default N=4) over a sampling period (default 144), on HLC3.
- **Channel**: filter ± filtered True Range × multiplier (default 1.414).
- **Long entry**: filter rising AND close > upper band AND (Stoch K > 80 OR K < 20).
- **Exit**: close crosses below upper band.

All parameters are exposed in the sidebar so you can match a specific version's settings.

## Performance

- **First cold run**: ~8–12 seconds for ~70 coins. 20 parallel workers under Binance's 1200 weight/min limit.
- **Subsequent reloads** within the current 4h bar: ~2s (served from on-disk cache).
- **On-disk cache** at `.cache/ohlc/*.pkl`, keyed by `(symbol, interval)`. A cached frame stays valid until the next bar should open.
- **Two refresh modes**:
  - **Refresh** — soft: only refetches coins whose bar has rolled over.
  - **Force refetch** — hard: ignores cache, refetches everything.

## Coverage notes

- Stablecoins and tokenized RWAs (~25 of the top 100) are excluded — no meaningful trend signal.
- Coins not listed on Binance (a handful — typically rival exchange tokens like KCS, OKB, BGB, GT, LEO, WBT) are skipped and shown in the "Skipped" expander.
- Realistic coverage is ~60–68 coins out of the top 100.

## Geo-blocking

Binance.com (`api.binance.com`) is blocked in the US/UK. The client transparently falls back to `data-api.binance.vision`, Binance's CDN-fronted public data mirror, which serves the same exchangeInfo and klines endpoints from regions where the main API is restricted.

## Strategies

Each of the five tabs (Crypto / Stocks / Low-Float / Metals / Commodities) picks its own
strategy from a dropdown. A *strategy* is a named preset: a logic + its parameters.

- **Logic**: the actual Python implementation (currently `gaussian_channel_v3_1`).
  Adding a new logic = registering a `LogicSpec` in `strategies.py` (e.g. when you
  port another Pine script). It then appears for all tabs automatically.
- **Presets**: same logic, different params. Built-ins ship in code; user presets
  live as JSON in `strategies/*.json`.
- **Editing params**: open "⚙ Strategy parameters" on any tab. Changes apply live;
  "Save as preset" writes a new JSON you can then pick from the dropdown.
- **Uploading**: sidebar → "Upload strategy JSON". Format:
  `{"name": ..., "logic_key": "gaussian_channel_v3_1", "params": {...}}`.
  Use the "Download template JSON" button for a starting point.
- **Assignment persistence**: which strategy each class uses is stored in
  `strategy_assignments.json`.

**Persisting presets (Commit button)**: Streamlit Cloud's disk is ephemeral, so
saved presets and assignment changes live only for the session by default. Add a
GitHub token to secrets and the sidebar shows a **⬆ Commit presets to repo**
button that pushes `strategies/*.json` + `strategy_assignments.json` to the repo
(via the GitHub Contents API) — making them permanent *and* picked up by the alert
cron. Token setup:

```toml
# .streamlit/secrets.toml  (or Streamlit Cloud "Secrets" panel)
[github]
token = "<fine-grained PAT with Contents: Read and write on this repo>"
repo = "sebamarghella/trend-radar"
branch = "main"
```

Without a token, commit `strategies/*.json` to the repo manually.

### Benzinga low-float candidates

Add the Benzinga Stock Scanner API key locally or in the Streamlit Cloud Secrets
panel. It is read server-side and must not be committed:

```toml
[benzinga]
api_key = "your-key"
```

The Low-Float tab uses one cached market-movers request per refresh (five-minute
cache by default). Its optional daily alert workflow is isolated from Stocks:
it uses its own Telegram bot, state/history files, and open-position ledger.
Neither workflow places trades.

**Security**: the commit button is server-side (the token is never exposed to the
browser), but anyone who can open the app can click it. Keep the deployed app's
sharing set to "Only specific people" if commit is enabled.

## Files

- `app.py` — Streamlit UI (5 tabs, per-class strategy dropdowns)
- `benzinga_universe.py` — Benzinga low-float candidate discovery and local validation
- `run_alerts.py` — Headless alert engine; each class uses its assigned strategy
- `strategies.py` — Logic registry, Strategy presets, JSON load/save, assignments
- `indicator_engine.py` — optional `pandas-ta` wrapper for library-backed indicators
- `asset_classes.py` — Crypto / Stocks / Metals / Commodities universes + resolvers
- `sources.py` — Binance / Gate.io / Kraken / Yahoo data sources + multi-source resolver
- `gaussian_channel.py` — N-pole Gaussian filter, True Range, Wilder RSI, Stoch RSI, replay
- `alerts.py` — Telegram + per-class state-diff
- `cache.py` — On-disk OHLC cache with bar-aware freshness
- `coins.py` — Top-100 crypto universe + exclusion list
- `strategies/` — user preset JSONs
- `strategy_assignments.json` — which strategy each asset class uses
- `export_crypto_signals.py` - Headless JSON producer for external routines
- `data/crypto_signals.json` - Latest committed crypto signal snapshot

## Tweaking the strategy

The sidebar exposes the same inputs as the Pine script. If you have a different version with a non-default `mult` or `period`, change them there — no code edits needed.

## pandas-ta engine

The app now supports an optional `pandas-ta` indicator engine for library-backed
strategy logics such as EMA cross and Supertrend. When `pandas-ta` is installed,
those logics appear in the same strategy dropdown as the hand-ported strategies.

- Install path: `pip install -r requirements.txt`
- Current package constraint: `pandas-ta` on PyPI requires Python 3.12+, so the
  dependency is gated with an environment marker and older runtimes will simply
  keep showing the hand-ported logics only.

## Autonomous alerts via GitHub Actions

You don't need to keep the Streamlit app open to receive Telegram alerts. `run_alerts.py` scans daily Stocks or Low-Float signals, selected by its workflow. The scheduled job sends green-flip OPEN LONG and red-flip CLOSE LONG alerts shortly after modeled fills at the following trading bar's open. OPEN alerts include the entry fill price; CLOSE alerts include both the entry and exit fill prices. The Streamlit app displays the Stocks alert history but does not send Telegram messages. These are modeled market-open fills, not broker executions.

The delivered-alert position ledger starts on 29 September 2026 with **AMD only**, opened at $616.96. A CLOSE alert is sent only for a stock with a delivered OPEN alert in that ledger; older model positions are not treated as user trades. Each tracked stock remains in the daily scan even after it leaves the top-570 ranking, until its matching closing fill is reported. Its opening strategy settings are saved with the position so later changes to the tab's preset do not change the exit rule for that open trade. An unavailable or stale bar for a tracked stock fails the job so the run monitor can warn about it. A recovered close from a missed scan is marked as a late notice.

**Setup, one-time:**

1. Create a private GitHub repo. Copy the entire `trend_radar/` folder to the repo root and push.
2. Repo → **Settings → Secrets and variables → Actions → New repository secret**, add:
   - `TELEGRAM_BOT_TOKEN` — your bot token from @BotFather
   - `TELEGRAM_CHAT_ID` — your numeric chat id (e.g. from @userinfobot)
   - `LOW_FLOAT_TELEGRAM_BOT_TOKEN` — a separate bot token for Low-Float alerts
   - `LOW_FLOAT_TELEGRAM_CHAT_ID` — the Low-Float bot's destination chat ID
3. `.github/workflows/alerts.yml` runs Stocks **Monday-Friday at 09:50 New York time**. `.github/workflows/low-float-alerts.yml` runs Low-Float separately at **09:55 New York time**. GitHub handles daylight saving time through the workflow's timezone. Triggering either manually before 09:45 New York time or on a weekend does nothing.
4. The first run after an alert-logic change silently seeds the filled-position baseline. Future filled flips generate Telegram messages. If the latest Yahoo daily bar is not for the current New York trading day, it cannot generate an alert.
   For a one-time check of fills that already occurred today, dispatch the workflow with `send_todays_fills=true` after 09:45 New York time. That first dispatch sends only today's filled flips and establishes the new baseline; later routine runs will not repeat them.

**Tweaking the cadence or timeframe:**

- Keep the schedule after the US market open. The engine enforces the weekday and 09:45 New York time gates, and the Stocks alert timeframe is daily.

**State persistence:** each workflow commits only its own state, open-position ledger, and alert history after each run, including when a tracked fetch or Telegram send fails. Stocks retains its legacy files; Low-Float uses `low_float_alerts_state.json`, `low_float_alert_positions.json`, and `low_float_alerts_history.json`. Without these files, a workflow could lose track of open alerted trades or repeat messages.

**Local cron alternative:** if you have a Linux box / WSL / Mac that's always on, set its cron timezone to New York:

```cron
CRON_TZ=America/New_York
50 9 * * 1-5 cd /path/to/trend_radar && TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... python run_alerts.py >> alerts.log 2>&1
```

## Public crypto signals JSON

The repo also publishes `data/crypto_signals.json` for unattended routines that
need one stable file to read with a single GET. The separate workflow at
`.github/workflows/crypto-signals.yml` runs daily at **00:30 UTC**, computes the
Crypto tab's assigned strategy on the last completed 1-day bar, enriches symbols
with HyperLiquid perp availability, and commits the JSON back to the repo.

Raw URL pattern:

```text
https://raw.githubusercontent.com/sebamarghella/<repo>/<branch>/data/crypto_signals.json
```

The exporter is independent of Streamlit and Telegram, so it keeps running even
when the app is asleep or alert secrets are not configured.
