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
- **Drilldown**: per-coin price chart with the Gaussian channel overlaid and long-position bars highlighted.
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

Each of the four tabs (Crypto / Stocks / Metals / Commodities) picks its own
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

**Streamlit Cloud caveat**: uploaded presets and assignment changes live only for
the session (ephemeral disk). To make them permanent *and* drive the alert cron,
commit the `strategies/*.json` and `strategy_assignments.json` to the repo.

## Files

- `app.py` — Streamlit UI (4 tabs, per-class strategy dropdowns)
- `run_alerts.py` — Headless alert engine; each class uses its assigned strategy
- `strategies.py` — Logic registry, Strategy presets, JSON load/save, assignments
- `asset_classes.py` — Crypto / Stocks / Metals / Commodities universes + resolvers
- `sources.py` — Binance / Gate.io / Kraken / Yahoo data sources + multi-source resolver
- `gaussian_channel.py` — N-pole Gaussian filter, True Range, Wilder RSI, Stoch RSI, replay
- `alerts.py` — Telegram + per-class state-diff
- `cache.py` — On-disk OHLC cache with bar-aware freshness
- `coins.py` — Top-100 crypto universe + exclusion list
- `strategies/` — user preset JSONs
- `strategy_assignments.json` — which strategy each asset class uses

## Tweaking the strategy

The sidebar exposes the same inputs as the Pine script. If you have a different version with a non-default `mult` or `period`, change them there — no code edits needed.

## Autonomous alerts via GitHub Actions

You don't need to keep the Streamlit app open to receive Telegram alerts — there's a separate `run_alerts.py` script that does only the fetch → replay → flip-detect → Telegram pipeline, no UI. Wire it to a free GitHub Actions cron and your PC can be off.

**Setup, one-time:**

1. Create a private GitHub repo. Copy the entire `trend_radar/` folder to the repo root and push.
2. Repo → **Settings → Secrets and variables → Actions → New repository secret**, add:
   - `TELEGRAM_BOT_TOKEN` — your bot token from @BotFather
   - `TELEGRAM_CHAT_ID` — your numeric chat id (e.g. from @userinfobot)
3. The workflow at `.github/workflows/alerts.yml` runs daily at **00:05 UTC** (5 min after the daily bar closes). Trigger it once manually first via the Actions tab to seed the alert state.
4. First run is silent — it just saves the current LONG/FLAT baseline. From the second run on, every flip vs. the saved baseline sends a Telegram message.

**Tweaking the cadence or timeframe:**

- Edit the `cron:` line in `alerts.yml`. e.g. `0 */4 * * *` runs every 4 hours.
- Set `TR_INTERVAL_MINUTES` in the workflow `env:` to match (1440 for daily, 240 for 4h).
- For higher-frequency timeframes, GitHub's free tier caps at 2000 min/month for private repos — plenty for hourly runs of this script (each is ~30s).

**State persistence:** the workflow commits `alerts_state.json` back to the repo after each run. Without that, every run would think it's the first run and never alert.

**Local cron alternative:** if you have a Linux box / WSL / Mac that's always on, just put this in your crontab and skip GitHub entirely:

```cron
5 0 * * * cd /path/to/trend_radar && TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... python run_alerts.py >> alerts.log 2>&1
```
