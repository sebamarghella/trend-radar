# Learnings

## Streamlit + AgGrid

- Keep the `AgGrid(...)` call outside any optional focus or deep-link conditionals. If the grid render is nested under a branch like `if focus_symbol ...`, `grid_response` can be undefined for normal page loads.
- When tightening the table layout, `fit_columns_on_grid_load=True` is the safe way to make columns fill the available table width and remove the empty right gutter.
- If we make the grid denser, reduce both the AgGrid theme variables and the cell/header font sizes together so row height, header height, and text stay visually aligned.
- A field added to the signal dict does not show up in the grid by itself: it also needs a `COLUMN_FLEX` weight (weights must sum to 100) and a `configure_column(...)` call. Grid column order follows `COLUMN_FLEX`. (`confluence` and `flow_delta` were computed for months without being displayed.)

## Strategy Integration

- A new strategy only needs to return the fields the app already reads:
  `snapshot`, `state_series`, `trades`, and optional `overlays`.
- The snapshot must expose exactly the `SignalState` field names (`gaussian_channel.py`):
  `in_position`, `bars_in_state`, `entry_index`, `entry_price`, `bar_color`, `filter_up`, `close_vs_hband_pct`, `stoch_k`, `last_close`, `last_filt`, `last_hband`, `last_lband`.
  **It is `last_filt`, not `last_filter`.** An earlier version of this file said `last_filter`; passing that to `SignalState(...)` raises a `TypeError` on every render as soon as the strategy is registered.
- Overlay columns the chart draws are `filt`, `hband`, `lband`. Any other name is silently ignored.
- If a custom strategy hits constructor mismatch issues with the shared `SignalState` object in deployment, a lightweight attribute object like `SimpleNamespace` is a safe fallback as long as it exposes the same field names.
- For Donchian logic in this app, use the previous-bar channel values with `.shift(1)` so the breakout compares the close against the prior channel, not the current bar's own high/low.
- `pandas-ta` needs Python 3.12+, and its pinned `numba==0.61.2` refuses to install on 3.14. `requirements.txt` gates it to `>=3.12,<3.14`, and `indicator_engine.py` hides the EMA/Supertrend logics when it is missing. The Streamlit Cloud app is pinned to **Python 3.13** (App settings → General → Python version; changeable in place, no redeploy) so those logics are available. Streamlit's default for new deploys was 3.14. An open-ended `>=` marker took the app down on 2026-09-27: a failed dependency install doesn't show on the next push (the old process keeps running) — it breaks on the next restart.

## Data & Universe

- The crypto universe is live (`coins.live_universe`): CoinGecko ranking, cached daily in `.cache/universe.json`, stablecoins/tokenized funds dropped, top 120 that an exchange can supply. The static `TOP_100` list is only a fallback. A static list silently rots: by September 2026 it was missing 35 of Signum's 100 coins.
- CoinGecko ranks mid-caps differently from Signum's source (JTO was #141 vs #114), which is why the target is 120 rather than 100.
- Rebrands (TON → GRAM): Binance flags the old pair as non-trading, but Kraken kept serving the old ticker with the new prices, so the coin showed up under the old name rather than disappearing. Add rebrands to `sources.RENAMES`; the old ticker's history gets stitched in front (1:1 rebrands only).
- All fetching goes through `sources.fetch_series()`. It skips a source whose last bar is more than 3 intervals old (6 days for markets that close) and falls through to the next exchange.
- When comparing against Signum, their Green/Red is the direction of the Gaussian filter, not our LONG/FLAT position state.
- **Breakouts (`breakouts.py`) are Signum-*style*, not a Signum copy.** From their crypto data (2026-09-27): resistance = the high of a confirmed swing high (≥3 bars each side; exact on 34/34 same-exchange coins); support = a later *higher-low* swing inside the range, not the range extreme; validated = GC trend flips Green after the breakout. How they pick *which* swing high anchors a range couldn't be recovered - a 216-design grid search matched their latest breakout on 3/34 coins. Ours anchors a range at a swing high, needs it to hold 20 bars, ends it only on a close below the range's lowest swing low. Agreement: statuses 15/16 where dates line up, dates within a week on ~1 in 5 coins; ours flags more recent breakouts in strong rallies (22 vs 6 over 30 days).
- The Stocks universe is live too (`stocks_universe.py`): NASDAQ's public screener (every NYSE/NASDAQ/AMEX listing incl. ADRs, no key), ranked by market cap, preferreds dropped, share classes collapsed (GOOGL/GOOG → one), top 570. Signum's Pro stocks radar is built the same way; ranks match within ~1 place.
- Yahoo prices are **raw (unadjusted)**, like TradingView and Signum. Dividend-adjusted prices shift the Gaussian filter 0.1–1% on dividend payers and moved stock flip dates by up to ~8 days vs Signum; with raw prices 15/15 sampled Signum stocks match to the day.

## Local Development

- Streamlit reruns the whole script on every click. At ~1,500 series that was ~20s of Gaussian-filter CPU per interaction; `strategies.run_strategy_cached` (LRU keyed by logic + params + OHLC fingerprint) brings a rerun under 1s. Use it for anything the UI recomputes; treat results as read-only.
- Bar-based cache freshness (`now < last_bar + interval`) is right for 24/7 crypto but calls Friday's stock bar stale all weekend and overnight. Non-24/7 classes also accept a cache file younger than 30 minutes.
- Don't run the app from a virtualenv inside Google Drive. Package imports stream through Drive's virtual filesystem (`import requests` alone took 5.6s) and the app never finishes loading. Keep the venv on local disk (e.g. `~/.venvs/trend-radar`, Python 3.13).
- Running the app locally rewrites `.cache/alerts_state.json` / `alerts_history.json`, which are tracked and owned by the cron job. Discard them (`git checkout -- .cache/alerts_*.json`) before committing.
- Drive copies use CRLF line endings and the repo uses LF. Diff with `--strip-trailing-cr`, or every file looks 100% changed.

## Deployment

- "Oh no. Error running app" hides the real error from logged-out visitors. Signed in as the app owner (GitHub login, not Google), the same page says e.g. "Error installing requirements", and **Manage app** shows the install log.
- Streamlit Cloud is reading from the GitHub repo, not just local workspace edits. Local fixes are not live until they are committed and pushed.
- Work that exists only in the Drive copy is invisible to the deployed app and easy to forget: two months of features sat unpushed and broken by one missing line. Push small and often.
