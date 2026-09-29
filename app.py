"""Trend Radar — GaussianChannel Strategy v3.1 across multiple asset classes.

Run:
    streamlit run app.py
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import altair as alt
import pandas as pd
import streamlit as st
from st_aggrid import AgGrid, GridOptionsBuilder
from st_aggrid.shared import JsCode

import json

import alerts
import live_log
import performance_ui as perf_ui
import ui_tweaks

# Streamlit Cloud can keep an old copy of an imported module in memory after a
# push (partial reload → stale UI or AttributeError). These modules are small,
# so re-import them on every run; dependency order matters.
import importlib
import performance as _performance
for _m in (_performance, live_log, perf_ui, ui_tweaks):
    importlib.reload(_m)
import breakouts as bo_mod
import cache as ohlc_cache
import strategies as strat_registry
import theme as T
from asset_classes import ASSET_CLASSES, AssetClass
from gaussian_channel import BAR_COLORS, compute_stats
from sources import DataSource, Resolver, SourceError, fetch_series
from strategies import Strategy


st.set_page_config(
    page_title="Trend Radar — GaussianChannel v3.1",
    layout="wide",
    initial_sidebar_state="expanded",
)

# --- Theme toggle (must run before any other UI so the palette propagates) ---

if "ui_mode" not in st.session_state:
    st.session_state.ui_mode = "Light"  # default per request

# The native Streamlit sidebar toggle lets the settings dock collapse.
st.sidebar.header("Settings")
# Compact segmented radio at the top of the settings dock.
_ui_mode = st.sidebar.radio(
    "Theme", ["Light", "Dark"],
    index=0 if st.session_state.ui_mode == "Light" else 1,
    horizontal=True, key="ui_mode_radio",
)
st.session_state.ui_mode = _ui_mode
PALETTE = T.get_palette(_ui_mode)

# Custom CSS — UI Pro Max guidance applied with the active palette. Streamlit's
# .streamlit/config.toml only sets the BOOT theme (base="light"); these rules
# repaint the entire app to match the toggle on every rerun.
st.markdown(f"""
<link rel='preconnect' href='https://fonts.googleapis.com'>
<link rel='preconnect' href='https://fonts.gstatic.com' crossorigin>
<link href='https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap' rel='stylesheet'>
<style>
:root {{
    color-scheme: {PALETTE["MODE"]};
}}
/* Page-level color + bg only — NO global font-family override (the previous
   `[class*='css']` rule cascaded into the AgGrid table and inflated cell
   width via Inter, truncating columns). */
.stApp {{
    background-color: {PALETTE["BG_BASE"]} !important;
    color: {PALETTE["FG_PRIMARY"]};
}}
section[data-testid='stSidebar'] {{
    background-color: {PALETTE["BG_CARD"]} !important;
    border-right: 1px solid {PALETTE["BORDER"]};
}}
/* Inter for Streamlit's own widgets via the [data-testid] hook — explicitly
   does NOT touch .ag-root-wrapper. */
[data-testid='stMarkdownContainer'],
[data-testid='stHeader'],
[data-testid='stSidebar'],
[data-testid='stMetricLabel'],
[data-testid='stMetricValue'],
[data-testid='stCaptionContainer'],
.stTabs, .stButton, .stSelectbox, .stRadio, .stTextInput, .stNumberInput, .stSlider, .stExpander {{
    font-family: {PALETTE["FONT_SANS"]};
    font-feature-settings: "tnum";
}}
[data-testid='stMetricValue'] {{
    font-family: {PALETTE["FONT_MONO"]};
    font-variant-numeric: tabular-nums;
}}
/* AgGrid is explicitly NOT styled by us — let balham/balham-dark drive the
   table's font, row height, and column sizing. The earlier attempts to pin
   --ag-font-size lost to higher-specificity host font-family overrides. */
/* Keep the dashboard close to the top toolbar. */
[data-testid='stMainBlockContainer'] {{ padding-top: 2.25rem; }}
[data-testid='stSidebarHeader'] {{ height: 2.25rem; min-height: 0; padding-top: 0.25rem; padding-bottom: 0; }}
[data-testid='stSidebarUserContent'] {{ padding-top: 0.25rem; }}
/* Keep the native settings collapse affordance visible without a hover. */
[data-testid='stSidebarCollapseButton'] {{ visibility: visible !important; }}
@media (max-width: 900px) {{
    /* Let the chart use the full width before the watchlist on smaller screens. */
    [data-testid='stHorizontalBlock']:has([class*='st-key-drilldown_pane_']) {{
        flex-direction: column !important;
    }}
    [data-testid='stHorizontalBlock']:has([class*='st-key-drilldown_pane_']) > [data-testid='stColumn'] {{
        width: 100% !important;
        flex: 1 1 auto !important;
    }}
    /* The six existing toolbar controls wrap into usable rows. */
    [data-testid='stHorizontalBlock']:has(> [data-testid='stColumn']:nth-child(6)) {{
        flex-wrap: wrap !important;
    }}
    [data-testid='stHorizontalBlock']:has(> [data-testid='stColumn']:nth-child(6)) > [data-testid='stColumn'] {{
        flex: 1 1 28% !important;
        min-width: 150px !important;
    }}
}}
h1 {{ padding-top: 0; }}
/* Page heading scale */
h1, h2, h3, h4, h5, h6 {{ color: {PALETTE["FG_PRIMARY"]}; }}
h1 {{ font-size: 28px; font-weight: 600; letter-spacing: -0.02em; margin-bottom: 4px; }}
h2 {{ font-size: 20px; font-weight: 600; letter-spacing: -0.01em; }}
h3 {{ font-size: 16px; font-weight: 600; }}
[data-testid='stCaptionContainer'] {{ color: {PALETTE["FG_MUTED"]}; font-size: 13px; }}
/* Tab bar — accent underline, no excessive padding */
.stTabs [data-baseweb='tab-list'] {{ border-bottom: 1px solid {PALETTE["BORDER"]}; gap: 4px; }}
.stTabs [data-baseweb='tab'] {{ padding: 8px 14px; color: {PALETTE["FG_MUTED"]}; }}
.stTabs [aria-selected='true'] {{ color: {PALETTE["FG_PRIMARY"]}; border-bottom: 2px solid {PALETTE["ACCENT"]}; }}
/* Sidebar polish */
section[data-testid='stSidebar'] h2 {{ font-size: 14px; text-transform: uppercase; letter-spacing: 0.05em; color: {PALETTE["FG_MUTED"]}; margin-top: 12px; }}
section[data-testid='stSidebar'] [data-testid='stCaptionContainer'] {{ font-size: 12px; }}
/* Metric cards a touch denser */
[data-testid='stMetric'] {{ padding: 6px 0; }}
[data-testid='stMetricLabel'] {{ color: {PALETTE["FG_MUTED"]}; font-size: 12px; text-transform: uppercase; letter-spacing: 0.05em; }}
[data-testid='stMetricValue'] {{ color: {PALETTE["FG_PRIMARY"]}; }}
</style>
""", unsafe_allow_html=True)

st.markdown(f"""
<style>
.radar-help-row {{
    display:flex;
    flex-wrap:wrap;
    gap:8px;
    margin: 2px 0 10px 0;
}}
.radar-help-pill {{
    position:relative;
    display:inline-flex;
    align-items:center;
    gap:6px;
    padding:4px 10px;
    border-radius:999px;
    border:1px solid {PALETTE["BORDER"]};
    background:{PALETTE["BG_CARD"]};
    color:{PALETTE["FG_MUTED"]};
    font-size:12px;
    line-height:1.2;
    cursor:default;
}}
.radar-help-pill b {{
    color:{PALETTE["FG_PRIMARY"]};
    font-family:{PALETTE["FONT_MONO"]};
    font-weight:600;
}}
.radar-help-bubble {{
    position:absolute;
    left:0;
    top:calc(100% + 8px);
    width:220px;
    padding:10px 12px;
    border-radius:12px;
    border:1px solid {PALETTE["BORDER"]};
    background:{PALETTE["BG_CARD"]};
    color:{PALETTE["FG_PRIMARY"]};
    box-shadow:0 10px 24px rgba(0,0,0,0.12);
    font-size:12px;
    line-height:1.45;
    opacity:0;
    transform:translateY(-4px);
    pointer-events:none;
    transition:opacity .16s ease, transform .16s ease;
    z-index:999;
}}
.radar-help-pill:hover .radar-help-bubble {{
    opacity:1;
    transform:translateY(0);
}}
</style>
""", unsafe_allow_html=True)


# --- Global sidebar (shared across all tabs) -----------------------------------

# Reserve a slot at the top of the sidebar for the alerts feed; we fill it
# after the tabs render so any flips this rerun show up immediately.
_alerts_slot = st.sidebar.container()


# --- Fear & Greed Index (crypto macro regime) --------------------------------

@st.cache_data(ttl=60 * 60, show_spinner=False)
def _fetch_fear_greed() -> tuple[int | None, str | None]:
    """alternative.me Fear & Greed Index. Cached 1h; the source updates daily."""
    import requests
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=8)
        payload = r.json()
        d = payload["data"][0]
        return int(d["value"]), d.get("value_classification") or "—"
    except Exception:  # noqa: BLE001
        return None, None


def _fg_color(v: int | None) -> str:
    if v is None: return PALETTE["FG_MUTED"]
    if v <= 25:   return PALETTE["DESTRUCTIVE"]   # extreme fear
    if v <= 45:   return "#F59E0B"                # fear (amber)
    if v <= 55:   return PALETTE["FG_MUTED"]      # neutral
    if v <= 75:   return PALETTE["ACCENT"]        # greed
    return "#16A34A"                              # extreme greed


_fg_val, _fg_class = _fetch_fear_greed()
if _fg_val is not None:
    _fg_chip_color = _fg_color(_fg_val)
    st.sidebar.markdown(
        f"""
        <div style="padding:8px 12px;border:1px solid {PALETTE["BORDER"]};
                    border-radius:8px;margin:0 0 12px 0;background:{PALETTE["BG_CARD"]};">
          <div style="font-size:11px;color:{PALETTE["FG_MUTED"]};
                      text-transform:uppercase;letter-spacing:0.05em;
                      display:flex;align-items:center;justify-content:space-between;">
            <span>Fear &amp; Greed</span>
            <span style="font-size:10px;color:{PALETTE["FG_MUTED"]};">crypto · 24h</span>
          </div>
          <div style="display:flex;align-items:baseline;gap:10px;margin-top:4px;">
            <div style="font-size:26px;font-weight:700;color:{_fg_chip_color};
                        font-family:{PALETTE["FONT_MONO"]};line-height:1;">{_fg_val}</div>
            <div style="font-size:12px;color:{_fg_chip_color};font-weight:600;">{_fg_class}</div>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


st.sidebar.header("Strategies")
st.sidebar.caption(
    "Each tab picks its own strategy. Edit params inline on a tab, or upload a "
    "preset JSON here. Format: `{name, logic_key, params}`."
)
_uploaded = st.sidebar.file_uploader("Upload strategy JSON", type=["json"], key="strat_upload")
if _uploaded is not None:
    try:
        _d = json.load(_uploaded)
        _strat = strat_registry.parse_strategy_dict(_d)
        strat_registry.save_strategy(_strat)
        st.sidebar.success(f"Added '{_strat.name}'. Pick it in a tab's Strategy dropdown.")
    except Exception as e:  # noqa: BLE001
        st.sidebar.error(f"Invalid strategy JSON: {e}")

# Downloadable template so users know the schema.
_template = json.dumps(
    strat_registry.Strategy(
        "My strategy", "gaussian_channel_v3_1",
        strat_registry.LOGICS["gaussian_channel_v3_1"].defaults(),
    ).to_dict(),
    indent=2,
)
st.sidebar.download_button("Download template JSON", _template, file_name="strategy_template.json")


def _github_secrets() -> tuple[str, str, str]:
    try:
        gh = st.secrets.get("github", {})
        return str(gh.get("token", "")), str(gh.get("repo", "")), str(gh.get("branch", "") or "main")
    except Exception:
        return "", "", "main"


_gh_token, _gh_repo, _gh_branch = _github_secrets()
if _gh_token and _gh_repo:
    if st.sidebar.button("⬆ Commit presets to repo", help="Push saved presets + per-class assignments to GitHub so they persist across restarts and drive the alert cron."):
        import repo_sync
        with st.spinner("Committing presets to GitHub…"):
            res = repo_sync.sync_presets(
                _gh_repo, _gh_token, _gh_branch,
                strat_registry.STRATEGIES_DIR, strat_registry.ASSIGNMENTS_FILE,
            )
        n = len(res["created"]) + len(res["updated"]) + len(res["deleted"])
        if res["errors"]:
            st.sidebar.error("Commit errors: " + "; ".join(res["errors"][:3]))
        elif n == 0:
            st.sidebar.info("Nothing to commit — repo already up to date.")
        else:
            st.sidebar.success(
                f"Committed {n} change(s) to {_gh_repo}. Presets now persist "
                "(the app may briefly redeploy)."
            )
    st.sidebar.caption(
        "Saved presets persist for this session. Click **Commit presets** to push "
        "them to the repo (permanent + used by alerts)."
    )
else:
    st.sidebar.caption(
        "On Streamlit Cloud, uploads/saves last for the session. Add a GitHub "
        "token in secrets (`[github] token, repo`) to enable a **Commit presets** "
        "button. For now, commit `strategies/*.json` to the repo manually."
    )

st.sidebar.header("Backtest")
lookback_days = st.sidebar.slider(
    "Lookback (days)", min_value=30, max_value=365, value=180, step=15,
    help="Window for trade count / net % / win % in the radar grid.",
)

st.sidebar.header("Layout")
grid_height = st.sidebar.slider(
    "Table height", min_value=300, max_value=1000, value=620, step=20, format="%d px",
    help="Vertical size of the radar grid.",
)

st.sidebar.header("Telegram alerts")


def _load_telegram_secrets() -> tuple[str, str]:
    try:
        tg = st.secrets.get("telegram", {})
        return str(tg.get("bot_token", "")), str(tg.get("chat_id", ""))
    except Exception:
        return "", ""


_secret_token, _secret_chat = _load_telegram_secrets()
_has_server_secrets = bool(_secret_token and _secret_chat)

if _has_server_secrets:
    st.sidebar.success("Telegram configured from server secrets")
    bot_token, chat_id = _secret_token, _secret_chat
    alerts_enabled = st.sidebar.checkbox(
        "Fire on state flips", value=True,
        help="Sends a Telegram message when any coin flips FLAT↔LONG.",
    )
else:
    alerts_enabled = st.sidebar.checkbox(
        "Fire on state flips", value=False,
        help="Sends a Telegram message when any coin flips FLAT↔LONG.",
    )
    bot_token = st.sidebar.text_input("Bot token", type="password")
    chat_id = st.sidebar.text_input("Chat ID")
    st.sidebar.caption(
        "Persist by saving to `.streamlit/secrets.toml` "
        "(local) or the Secrets panel (Streamlit Cloud)."
    )

if st.sidebar.button("Send test alert", help="Verify your token + chat ID"):
    ok, err = alerts.send_telegram(bot_token, chat_id, "Trend Radar: test alert ✅")
    if ok:
        st.sidebar.success("Telegram OK")
    else:
        st.sidebar.error(f"Telegram failed: {err}")

if st.sidebar.button("Wipe disk cache", help="Delete cached OHLC files"):
    n = ohlc_cache.clear()
    st.sidebar.success(f"Removed {n} cache file(s).")
    st.cache_data.clear()


# --- Data fetch ----------------------------------------------------------------


@st.cache_resource(ttl=15 * 60, show_spinner=False)
def cached_resolver(asset_key: str) -> Resolver:
    """One resolver per asset class — Yahoo for stocks/metals/commodities,
    multi-source for crypto. Cached so each tab visit reuses the same instance."""
    for ac in ASSET_CLASSES:
        if ac.key == asset_key:
            return ac.resolver_factory()
    raise ValueError(f"unknown asset class {asset_key}")


NON_24_7_CACHE_TTL_S = 30 * 60


def fetch_one(
    base: str, resolver: Resolver, interval: int,
    force_refresh: bool = False, is_24_7: bool = True,
) -> dict:
    statuses: dict[str, str] = {}

    def cached_fetch(src: DataSource, pair: str, interval_minutes: int) -> pd.DataFrame:
        key = f"{src.name}:{pair}"
        cached_df = None if force_refresh else ohlc_cache.load(src.name, pair, interval_minutes)
        if cached_df is not None and ohlc_cache.is_fresh(cached_df, interval_minutes):
            statuses[key] = "cache"
            return cached_df
        if cached_df is not None and not is_24_7:
            # Markets that close (stocks/futures): the bar-based rule calls
            # Friday's bar stale all weekend and overnight, which refetched
            # ~1,100 stock series on every load. A recent fetch is good enough.
            age = ohlc_cache.age_seconds(src.name, pair, interval_minutes)
            if age is not None and age < NON_24_7_CACHE_TTL_S:
                statuses[key] = "cache"
                return cached_df
        try:
            df = src.fetch_with_retry(pair, interval_minutes=interval_minutes)
        except SourceError:
            if cached_df is not None:
                statuses[key] = "stale"  # source errored — serve last good copy
                return cached_df
            raise
        ohlc_cache.save(src.name, pair, interval_minutes, df)
        statuses[key] = "fresh"
        return df

    try:
        res = fetch_series(base, resolver, interval, is_24_7=is_24_7, fetch=cached_fetch)
    except SourceError as e:
        return {"symbol": base, "ok": False, "reason": str(e)}
    src = res.source
    if len(res.df) < 60:
        return {"symbol": base, "ok": False, "reason": f"insufficient history on {src.name}"}
    return {
        "symbol": base, "ok": True, "pair": res.pair,
        "exchange": src.name, "exchange_short": src.short, "tv_prefix": src.tv_prefix,
        "df": res.df, "cache_status": statuses.get(f"{src.name}:{res.pair}", "fresh"),
        "stitched_from": res.stitched_from,
    }


@st.cache_data(ttl=15 * 60, show_spinner=False)
def load_universe_data(
    asset_key: str, interval: int, bust: int, force_refresh: bool,
) -> tuple[list[dict], list[dict]]:
    ac = next(a for a in ASSET_CLASSES if a.key == asset_key)
    resolver = cached_resolver(asset_key)
    universe = ac.get_universe(resolver)
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=20) as ex:
        futures = {
            ex.submit(fetch_one, c["symbol"], resolver, interval, force_refresh, ac.is_24_7): c
            for c in universe
        }
        progress = st.progress(0.0, text=f"Loading {ac.label.lower()} candles…")
        done = 0
        cache_hits = 0
        for fut in as_completed(futures):
            meta = futures[fut]
            res = fut.result()
            res["rank"] = meta["rank"]
            res["name"] = meta["name"]
            if res.get("cache_status") == "cache":
                cache_hits += 1
            results.append(res)
            done += 1
            suffix = (
                f"({cache_hits} from cache)"
                if cache_hits or done < 5
                else "(bar just rolled — refetching all)"
            )
            progress.progress(
                done / len(universe),
                text=f"Loaded {done}/{len(universe)} {suffix}",
            )
        progress.empty()
    ok = [r for r in results if r["ok"]]
    skipped = [r for r in results if not r["ok"]]
    return ok, skipped


def _trend_start_pnl(df: pd.DataFrame, trend_up: pd.Series | None) -> float | None:
    """Signum's "Trend Start P&L %": while the trend is up, % change from the open
    of the bar after the flip to Green to the latest close. None in a downtrend.
    Matches Signum to the hundredth (5/5 stocks, median 0.00pp on 91 crypto rows)."""
    if trend_up is None or trend_up.empty or not bool(trend_up.iloc[-1]):
        return None
    changed = trend_up.ne(trend_up.shift())
    flip_i = df.index.get_loc(changed[changed].index[-1])
    if flip_i + 1 >= len(df):
        return 0.0  # flipped on the latest bar: the trend starts next bar
    start_open = float(df["open"].iloc[flip_i + 1])
    return (float(df["close"].iloc[-1]) / start_open - 1) * 100 if start_open else None


def _short_date(ts: pd.Timestamp, ref: pd.Timestamp) -> str:
    """Grid-width date: 'Aug 21' within a year of `ref`, else "Aug '25"."""
    return ts.strftime("%b %d") if (ref - ts).days < 330 else ts.strftime("%b '%y")


def compute_signal(row: dict, strategy: Strategy, lookback_days_: int) -> dict:
    df = row["df"]
    result = strat_registry.run_strategy_cached(strategy, df)
    snap = result.snapshot
    trend_up = None
    if result.overlays is not None and "filt" in result.overlays.columns:
        filt = result.overlays["filt"]
        trend_up = filt > filt.shift()
    bo_state = bo_mod.detect_cached(df, trend_up, strat_registry._df_fingerprint(df))
    bo_last = bo_state.last
    stats = compute_stats(result.trades, now=df.index[-1], lookback_days=lookback_days_)
    taker_delta_pct = None
    taker_buy_base = None
    if {"volume", "taker_buy_base_volume"}.issubset(df.columns):
        volume_now = pd.to_numeric(df["volume"], errors="coerce").iloc[-1]
        taker_buy_base = pd.to_numeric(df["taker_buy_base_volume"], errors="coerce").iloc[-1]
        if pd.notna(volume_now) and volume_now not in (None, 0) and pd.notna(taker_buy_base):
            taker_delta_pct = float(((2.0 * taker_buy_base) - volume_now) / volume_now * 100.0)
    return {
        "rank": row["rank"],
        "symbol": row["symbol"],
        "name": row["name"],
        "pair": row["pair"],
        "exchange": row["exchange"],
        "exchange_short": row["exchange_short"],
        "tv_prefix": row["tv_prefix"],
        "state": "LONG" if snap.in_position else "FLAT",
        "bar_color": snap.bar_color,
        "filter_up": snap.filter_up,
        "bars_in_state": snap.bars_in_state,
        "close_vs_hband_pct": snap.close_vs_hband_pct,
        "stoch_k": snap.stoch_k,
        "last_close": snap.last_close,
        "trades": stats.trades,
        "net_pct": stats.net_pct,
        "win_pct": stats.win_pct,
        "sharpe": stats.sharpe,
        "max_dd_pct": stats.max_drawdown_pct,
        "flow_delta_pct": taker_delta_pct,
        "trend_pnl": _trend_start_pnl(df, trend_up),
        "breakout": (f"{_short_date(bo_last.date, df.index[-1])} {bo_mod.STATUS_GLYPH[bo_last.status]}"
                     if bo_last else None),
        "bo_date": bo_last.date.date().isoformat() if bo_last else None,
        "_bo": bo_state,
        "_df": df,
        "_overlays": result.overlays,
        "_state_series": result.state_series,
        "_trades": result.trades,
    }


def _mark_to_market_return(trade, latest_close: float, commission_per_side: float = 0.001) -> float | None:
    if trade is None or trade.entry_price in (None, 0):
        return None
    gross = latest_close / trade.entry_price
    fee_factor = (1.0 - commission_per_side) ** 2
    return gross * fee_factor - 1.0


def _build_equity_curve(
    trades: list,
    *,
    now: pd.Timestamp,
    lookback_days_: int,
    latest_ts: pd.Timestamp,
    latest_close: float,
    commission_per_side: float = 0.001,
) -> pd.DataFrame:
    cutoff = now - pd.Timedelta(days=lookback_days_)
    points = [{"time": cutoff, "equity": 1.0, "stage": "Start"}]
    equity = 1.0

    for trade in [t for t in trades if t.entry_ts >= cutoff]:
        if trade.closed and trade.exit_ts is not None:
            ret = trade.net_return(commission_per_side)
            point_ts = trade.exit_ts
            stage = "Closed trade"
        else:
            ret = _mark_to_market_return(trade, latest_close, commission_per_side)
            point_ts = latest_ts
            stage = "Open trade"
        if ret is None:
            continue
        equity *= 1.0 + ret
        points.append({"time": point_ts, "equity": equity, "stage": stage})

    curve = (
        pd.DataFrame(points)
        .sort_values("time")
        .drop_duplicates(subset=["time"], keep="last")
        .reset_index(drop=True)
    )
    curve["peak"] = curve["equity"].cummax()
    curve["drawdown"] = (curve["equity"] - curve["peak"]) / curve["peak"] * 100.0
    return curve[["time", "equity", "drawdown", "stage"]]


def _compute_tear_sheet(
    trades: list,
    *,
    now: pd.Timestamp,
    lookback_days_: int,
    latest_ts: pd.Timestamp,
    latest_close: float,
    commission_per_side: float = 0.001,
) -> dict:
    cutoff = now - pd.Timedelta(days=lookback_days_)
    recent = [t for t in trades if t.entry_ts >= cutoff]
    closed = [t for t in recent if t.closed and t.exit_ts is not None]
    closed_returns = [
        ret for ret in (t.net_return(commission_per_side) for t in closed) if ret is not None
    ]
    equity_curve = _build_equity_curve(
        recent,
        now=now,
        lookback_days_=lookback_days_,
        latest_ts=latest_ts,
        latest_close=latest_close,
        commission_per_side=commission_per_side,
    )

    end_equity = float(equity_curve["equity"].iloc[-1]) if not equity_curve.empty else 1.0
    net_return = end_equity - 1.0
    max_dd_pct = float(equity_curve["drawdown"].min()) if not equity_curve.empty else 0.0
    max_dd_abs = abs(max_dd_pct) / 100.0
    annual_return = (end_equity ** (365.0 / lookback_days_) - 1.0) if lookback_days_ > 0 else 0.0
    calmar = (annual_return / max_dd_abs) if max_dd_abs > 0 else None
    recovery = (net_return / max_dd_abs) if max_dd_abs > 0 else None

    profit_sum = sum(r for r in closed_returns if r > 0)
    loss_sum = abs(sum(r for r in closed_returns if r < 0))
    profit_factor = (profit_sum / loss_sum) if loss_sum > 0 else (None if profit_sum == 0 else float("inf"))
    expectancy = (sum(closed_returns) / len(closed_returns)) if closed_returns else None

    sharpe = None
    sortino = None
    if len(closed_returns) >= 2 and lookback_days_ > 0:
        mean_ret = sum(closed_returns) / len(closed_returns)
        variance = sum((r - mean_ret) ** 2 for r in closed_returns) / (len(closed_returns) - 1)
        std_ret = variance ** 0.5
        trades_per_year = len(closed_returns) * 365.0 / lookback_days_
        if std_ret > 0:
            sharpe = (mean_ret / std_ret) * (trades_per_year ** 0.5)
        downside = [r for r in closed_returns if r < 0]
        if downside:
            downside_sq = sum(r * r for r in downside) / len(downside)
            downside_dev = downside_sq ** 0.5
            if downside_dev > 0:
                sortino = (mean_ret / downside_dev) * (trades_per_year ** 0.5)

    wins = sum(1 for r in closed_returns if r > 0)
    avg_win = (sum(r for r in closed_returns if r > 0) / wins) if wins else None
    losses = sum(1 for r in closed_returns if r < 0)
    avg_loss = (sum(r for r in closed_returns if r < 0) / losses) if losses else None

    recent_trades = []
    for trade in recent[-5:]:
        if trade.closed and trade.exit_ts is not None:
            ret = trade.net_return(commission_per_side)
            exit_ts = trade.exit_ts
            status = "Closed"
        else:
            ret = _mark_to_market_return(trade, latest_close, commission_per_side)
            exit_ts = latest_ts
            status = "Open (MTM)"
        recent_trades.append(
            {
                "Entry": trade.entry_ts.strftime("%Y-%m-%d"),
                "Exit": exit_ts.strftime("%Y-%m-%d") if exit_ts is not None else "—",
                "Status": status,
                "Return %": (ret * 100.0) if ret is not None else None,
            }
        )

    return {
        "trades": len(recent),
        "closed_trades": len(closed_returns),
        "win_rate": (wins / len(closed_returns) * 100.0) if closed_returns else None,
        "net_return_pct": net_return * 100.0,
        "annual_return_pct": annual_return * 100.0,
        "max_dd_pct": max_dd_pct,
        "sharpe": sharpe,
        "sortino": sortino,
        "calmar": calmar,
        "profit_factor": profit_factor,
        "recovery_factor": recovery,
        "expectancy_pct": (expectancy * 100.0) if expectancy is not None else None,
        "avg_win_pct": (avg_win * 100.0) if avg_win is not None else None,
        "avg_loss_pct": (avg_loss * 100.0) if avg_loss is not None else None,
        "recent_trades": pd.DataFrame(recent_trades),
    }


def _fmt_number(value: float | None, *, pct: bool = False) -> str:
    if value is None:
        return "—"
    if value == float("inf"):
        return "Inf"
    return f"{value:+.2f}%" if pct else f"{value:.2f}"


def _comparison_curve_frame(
    name: str,
    strategy_result,
    *,
    now: pd.Timestamp,
    lookback_days_: int,
    latest_ts: pd.Timestamp,
    latest_close: float,
) -> pd.DataFrame:
    curve = _build_equity_curve(
        strategy_result.trades,
        now=now,
        lookback_days_=lookback_days_,
        latest_ts=latest_ts,
        latest_close=latest_close,
    ).copy()
    curve["strategy"] = name
    return curve


def _add_confluence(
    signals: list[dict],
    ok_rows: list[dict],
    *,
    asset_key: str,
    strategy: Strategy,
    interval_options: list[tuple[str, int]],
    current_interval_minutes: int,
    force_refetch: bool,
    only_symbols: set[str] | None = None,
) -> None:
    """Fill signal["confluence"]. With `only_symbols`, the extra-timeframe fetch +
    strategy run happens just for those symbols (the rows actually shown); the
    rest get "—". That fetch is the single biggest cost of a cold load."""
    interval_labels: dict[int, str] = {}
    ordered_intervals: list[int] = []
    for label, minutes in interval_options:
        if minutes not in interval_labels:
            interval_labels[minutes] = label
            ordered_intervals.append(minutes)

    if not signals:
        return

    state_by_symbol = {
        signal["symbol"]: {current_interval_minutes: signal["state"] == "LONG"}
        for signal in signals
    }
    extra_intervals = [m for m in ordered_intervals if m != current_interval_minutes]

    if extra_intervals:
        resolver = cached_resolver(asset_key)
        is_24_7 = next(a.is_24_7 for a in ASSET_CLASSES if a.key == asset_key)
        with ThreadPoolExecutor(max_workers=min(24, max(1, len(extra_intervals) * 8))) as ex:
            futures = {
                ex.submit(fetch_one, row["symbol"], resolver, minutes, force_refetch, is_24_7): (row["symbol"], minutes)
                for row in ok_rows
                if only_symbols is None or row["symbol"] in only_symbols
                for minutes in extra_intervals
            }
            for fut in as_completed(futures):
                symbol, minutes = futures[fut]
                try:
                    res = fut.result()
                except Exception:
                    state_by_symbol.setdefault(symbol, {})[minutes] = None
                    continue
                if not res.get("ok"):
                    state_by_symbol.setdefault(symbol, {})[minutes] = None
                    continue
                snap = strat_registry.run_strategy_cached(strategy, res["df"]).snapshot
                state_by_symbol.setdefault(symbol, {})[minutes] = bool(snap.in_position)

    for signal in signals:
        if only_symbols is not None and signal["symbol"] not in only_symbols:
            signal["confluence"] = "—"
            continue
        flags = [state_by_symbol.get(signal["symbol"], {}).get(minutes) for minutes in ordered_intervals]
        known = [flag for flag in flags if flag is not None]
        if not known:
            signal["confluence"] = "—"
            continue
        long_count = sum(1 for flag in known if flag)
        signal["confluence"] = f"{long_count}/{len(known)} LONG"


# --- Bar cycle helpers ---------------------------------------------------------


def _bar_cycle(interval_minutes_: int) -> tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp]:
    now_ = pd.Timestamp.now(tz="UTC")
    sec_per_bar = interval_minutes_ * 60
    epoch = int(now_.timestamp())
    current_open = pd.Timestamp((epoch // sec_per_bar) * sec_per_bar, unit="s", tz="UTC")
    next_open = current_open + pd.Timedelta(minutes=interval_minutes_)
    return now_, current_open, next_open


def _fmt_hm(td: pd.Timedelta) -> str:
    total = int(td.total_seconds())
    hours, rem = divmod(max(total, 0), 3600)
    minutes = rem // 60
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"


# --- AgGrid styling (shared across all tabs) -----------------------------------


_BAR_COLORS_JSON = ",".join(f"'{k}':'{v}'" for k, v in BAR_COLORS.items())


def _cellstyles(palette: dict) -> dict:
    """Build the per-mode AgGrid cellStyle / valueFormatter JsCode objects.
    Called on every render with the live palette so light/dark switch cleanly."""
    bar_fg_white = ",".join(f"'{k}'" for k in palette["BAR_FG_WHITE"])
    return {
        "STATE": JsCode(f"""
function(p) {{
    if (p.value === 'LONG') {{
        return {{ backgroundColor: '{palette["STATE_LONG_BG"]}', color: '{palette["STATE_LONG_FG"]}', fontWeight: 700 }};
    }}
    return {{ backgroundColor: '{palette["STATE_FLAT_BG"]}', color: '{palette["STATE_FLAT_FG"]}' }};
}}
"""),
        "BAR": JsCode(f"""
function(p) {{
    const colors = {{{_BAR_COLORS_JSON}}};
    const whitelist = [{bar_fg_white}];
    const fg = whitelist.includes(p.value) ? 'white' : '{palette["FG_PRIMARY"]}';
    return {{ backgroundColor: colors[p.value] || '{palette["BAR_COLORS"]["NEUTRAL"]}', color: fg, fontWeight: 600 }};
}}
"""),
        "FILTER": JsCode(f"""
function(p) {{
    if (p.value === true) return {{ color: '{palette["ACCENT"]}', fontWeight: 600 }};
    return {{ color: '{palette["DESTRUCTIVE"]}', fontWeight: 600 }};
}}
"""),
        "PCT": JsCode(f"""
function(p) {{
    if (p.value == null) return {{}};
    return p.value > 0 ? {{ color: '{palette["ACCENT"]}' }} : {{ color: '{palette["DESTRUCTIVE"]}' }};
}}
"""),
        # Sharpe: red <0, muted 0-1 (no risk-adjusted edge), green >1, deeper green >2.
        "SHARPE": JsCode(f"""
function(p) {{
    if (p.value == null) return {{}};
    if (p.value < 0) return {{ color: '{palette["DESTRUCTIVE"]}', fontWeight: 600 }};
    if (p.value < 1) return {{ color: '{palette["FG_MUTED"]}' }};
    if (p.value < 2) return {{ color: '{palette["ACCENT"]}', fontWeight: 600 }};
    return {{ color: '{palette["ACCENT"]}', fontWeight: 700 }};
}}
"""),
        # Max DD: always red — there are no "good" drawdowns. Bolder for worse.
        "MDD": JsCode(f"""
function(p) {{
    if (p.value == null) return {{}};
    if (p.value < -25) return {{ color: '{palette["DESTRUCTIVE"]}', fontWeight: 700 }};
    return {{ color: '{palette["DESTRUCTIVE"]}' }};
}}
"""),
    }

_FMT_PCT = JsCode("function(p) { return p.value != null ? (p.value >= 0 ? '+' : '') + p.value.toFixed(2) + '%' : ''; }")
_FMT_K = JsCode("function(p) { return p.value != null ? p.value.toFixed(1) : ''; }")
_FMT_PRICE = JsCode("function(p) { return p.value != null ? p.value.toPrecision(6) : ''; }")
_FMT_INT = JsCode("function(p) { return p.value != null ? p.value.toFixed(0) : ''; }")
_FMT_WIN = JsCode("function(p) { return p.value != null ? p.value.toFixed(0) + '%' : '—'; }")
_FMT_SHARPE = JsCode("function(p) { return p.value != null ? p.value.toFixed(2) : '—'; }")
# Max DD is always negative (or zero on flawless run). Format as percentage
# WITHOUT a leading '+' sign — there's never a positive DD.
_FMT_MDD = JsCode("function(p) { return p.value != null ? p.value.toFixed(1) + '%' : '—'; }")

TV_CHART_ID = "6O2rb5Ql"

_TV_VALUE_FMT = JsCode("function(p) { return p.value ? 'TV ↗' : ''; }")

def _tv_cell_style(palette: dict) -> dict:
    return {
        "cursor": "pointer",
        "color": palette["ACCENT"],
        "fontWeight": "600",
        "textAlign": "center",
    }

# Open the user's saved TV chart for the clicked coin. For crypto, the exchange
# prefix (BINANCE/GATEIO/KRAKEN) comes from the row. For non-crypto, the
# tv_prefix is empty and TradingView resolves the ticker itself (NASDAQ/NYSE/
# COMEX/NYMEX) — works for most US tickers, may fail for some futures.
_TV_CLICK_HANDLER = JsCode(f"""
function(event) {{
    if (event.colDef.field === 'tv' && event.data && event.data.pair) {{
        const cleanedPair = event.data.pair.replace(/_/g, '');
        const prefix = event.data.tv_prefix || '';
        const symbolPart = prefix ? (prefix + '%3A' + cleanedPair) : cleanedPair;
        const url = 'https://www.tradingview.com/chart/{TV_CHART_ID}/?symbol=' + symbolPart;
        window.open(url, '_blank', 'noopener,noreferrer');
    }}
}}
""")


# Per-column width as a percentage of the table. AgGrid has no literal percent
# unit, but `flex` weights distribute width proportionally — so weights that sum
# to 100 make each column occupy that % of the pane. No maxWidth anywhere, so
# the grid always fills 100% width; minWidth is just a readability floor that
# triggers horizontal scroll only when the pane gets very narrow.
COLUMN_FLEX = {
    "rank": 4, "symbol": 6, "name": 5, "exchange_short": 4, "pair": 5,
    "state": 5, "confluence": 5, "bar_color": 5, "filter_up": 4, "bars_in_state": 3,
    "close_vs_hband_pct": 5, "stoch_k": 4, "flow_delta": 5, "trend_pnl": 5, "breakout": 6, "last_close": 4,
    "trades": 4, "net_pct": 5, "win_pct": 3, "sharpe": 5, "max_dd_pct": 5, "tv": 3,
}  # sums to 100

assert sum(COLUMN_FLEX.values()) == 100, "column flex weights must sum to 100"


def build_grid_options(df: pd.DataFrame, palette: dict) -> dict:
    cs = _cellstyles(palette)
    tv_style = _tv_cell_style(palette)
    gb = GridOptionsBuilder.from_dataframe(df)
    gb.configure_default_column(resizable=True, sortable=True, filterable=False)
    gb.configure_selection(selection_mode="single", use_checkbox=False, suppressRowDeselection=True)
    F = COLUMN_FLEX
    gb.configure_column("rank", header_name="#", flex=F["rank"], minWidth=40, type=["numericColumn"])
    gb.configure_column("symbol", header_name="Sym", flex=F["symbol"], minWidth=55)
    gb.configure_column("name", header_name="Name", flex=F["name"], minWidth=90, maxWidth=220,
                        tooltipField="name")
    gb.configure_column("exchange_short", header_name="Src", flex=F["exchange_short"], minWidth=45)
    gb.configure_column("pair", header_name="Pair", flex=F["pair"], minWidth=65)
    gb.configure_column("exchange", hide=True)
    gb.configure_column("tv_prefix", hide=True)
    gb.configure_column("state", header_name="Pos", flex=F["state"], minWidth=55, cellStyle=cs["STATE"])
    if "confluence" in df.columns:
        gb.configure_column(
            "confluence", header_name="TF✓", flex=F["confluence"], minWidth=55,
            headerTooltip="Timeframe confluence: how many of 1d / 4h / 1h are LONG under this strategy.",
        )
    gb.configure_column("bar_color", header_name="Bar", flex=F["bar_color"], minWidth=80, cellStyle=cs["BAR"])
    gb.configure_column("filter_up", header_name="F↑", flex=F["filter_up"], minWidth=40, cellStyle=cs["FILTER"])
    gb.configure_column("bars_in_state", header_name="Bars", flex=F["bars_in_state"], minWidth=45, type=["numericColumn"])
    gb.configure_column(
        "close_vs_hband_pct", header_name="vs HB", flex=F["close_vs_hband_pct"], minWidth=84,
        type=["numericColumn"], valueFormatter=_FMT_PCT, cellStyle=cs["PCT"],
    )
    gb.configure_column("stoch_k", header_name="StK", flex=F["stoch_k"], minWidth=45, type=["numericColumn"], valueFormatter=_FMT_K)
    if "flow_delta" in df.columns:
        gb.configure_column(
            "flow_delta", header_name="Flow", flex=F["flow_delta"], minWidth=76,
            type=["numericColumn"], valueFormatter=_FMT_PCT, cellStyle=cs["PCT"],
            headerTooltip="Taker buy vs sell volume on the last bar (Binance only). Positive = aggressive buying.",
        )
    if "trend_pnl" in df.columns:
        gb.configure_column(
            "trend_pnl", header_name="Trend P&L", flex=F["trend_pnl"], minWidth=96,
            type=["numericColumn"], valueFormatter=_FMT_PCT, cellStyle=cs["PCT"],
            headerTooltip="Trend start P&L %: open of the day after the trend flipped up to the latest close "
                          "(same as Signum). Blank in a downtrend.",
        )
    if "breakout" in df.columns:
        gb.configure_column(
            "breakout", header_name="Breakout", flex=F["breakout"], minWidth=96,
            headerTooltip="Latest breakout above a consolidation range (held at least 20 days). "
                          "\u2713 validated (trend turned up), \u2026 pending, \u2717 invalidated (closed below support).",
        )
    gb.configure_column("last_close", header_name="Close", flex=F["last_close"], minWidth=60, type=["numericColumn"], valueFormatter=_FMT_PRICE)
    gb.configure_column("trades", header_name="Trades", flex=F["trades"], minWidth=50, type=["numericColumn"], valueFormatter=_FMT_INT)
    gb.configure_column(
        "net_pct", header_name="Net %", flex=F["net_pct"], minWidth=80,
        type=["numericColumn"], valueFormatter=_FMT_PCT, cellStyle=cs["PCT"],
    )
    gb.configure_column("win_pct", header_name="Win %", flex=F["win_pct"], minWidth=50, type=["numericColumn"], valueFormatter=_FMT_WIN)
    gb.configure_column(
        "sharpe", header_name="Sharpe", flex=F["sharpe"], minWidth=55,
        type=["numericColumn"], valueFormatter=_FMT_SHARPE, cellStyle=cs["SHARPE"],
        headerTooltip="Annualized per-trade Sharpe ratio. >1 ≈ decent, >2 ≈ strong, <0 ≈ losing edge.",
    )
    gb.configure_column(
        "max_dd_pct", header_name="MaxDD", flex=F["max_dd_pct"], minWidth=60,
        type=["numericColumn"], valueFormatter=_FMT_MDD, cellStyle=cs["MDD"],
        headerTooltip="Max drawdown on the strategy's equity curve over the lookback window. The deeper the worse.",
    )
    gb.configure_column(
        "tv", header_name="TV", flex=F["tv"], minWidth=45,
        sortable=False, filter=False,
        valueFormatter=_TV_VALUE_FMT, cellStyle=tv_style,
    )
    gb.configure_grid_options(onCellClicked=_TV_CLICK_HANDLER)
    opts = gb.build()
    # Size every column to its content (header included) instead of flex weights:
    # with ~22 columns, flex + fit-to-viewport crushed narrow columns below their
    # minWidth and truncated values. The grid scrolls horizontally when needed.
    for col in opts.get("columnDefs", []):
        col.pop("flex", None)
    opts["autoSizeStrategy"] = {"type": "fitCellContents"}
    # Autosize only measures rendered columns; render all ~22 so off-screen ones
    # get sized too (otherwise they keep a 200px default and leave gaps).
    opts["suppressColumnVirtualisation"] = True
    # Tabs other than the first render their grid while hidden (0px wide), so the
    # initial autosize measures nothing. Re-fit when the grid gets a real size
    # (i.e. when its tab is opened) and on window resizes.
    opts["onGridSizeChanged"] = JsCode(
        "function(p) { if (p.clientWidth > 0) { p.api.autoSizeAllColumns(); } }"
    )
    return opts


# --- Per-tab render ------------------------------------------------------------


# First key is the default selection on load.
SORT_MAP = {
    "State (long first)": ("state", False),  # LONG before FLAT (desc)
    "Rank": ("rank", True),
    "Bars in state": ("bars_in_state", False),
    "Bars in state (newest first)": ("bars_in_state", True),  # fresh flips on top
    "Stoch K": ("stoch_k", False),
    "Close vs HBand %": ("close_vs_hband_pct", False),
    "Net %": ("net_pct", False),
    "Win %": ("win_pct", False),
    "Trades": ("trades", False),
    "Sharpe": ("sharpe", False),
    "MaxDD (shallowest first)": ("max_dd_pct", False),  # closer to 0 = better
    "Trend start P&L %": ("trend_pnl", False),
    "Breakout (most recent first)": ("bo_date", False),
}


def _render_strategy_editor(
    ac_key: str, logic_key: str, base: Strategy, preset_widget_key: str,
) -> Strategy:
    """Editable params for the chosen preset, plus Save / Delete. Returns a
    Strategy reflecting the live-edited param values for this session."""
    logic = strat_registry.LOGICS[logic_key]
    edited: dict = {}
    with st.expander("⚙ Strategy parameters", expanded=False):
        st.caption(logic.description)
        cols = st.columns(2)
        for i, p in enumerate(logic.param_schema):
            col = cols[i % 2]
            wkey = f"param_{ac_key}_{base.name}_{p.key}"
            baseval = base.params.get(p.key, p.default)
            if p.kind == "bool":
                edited[p.key] = col.checkbox(p.label, value=bool(baseval), key=wkey)
            elif p.kind == "int":
                edited[p.key] = col.number_input(
                    p.label, value=int(baseval),
                    min_value=int(p.min) if p.min is not None else None,
                    max_value=int(p.max) if p.max is not None else None,
                    step=int(p.step or 1), key=wkey,
                )
            else:  # float
                edited[p.key] = col.number_input(
                    p.label, value=float(baseval),
                    min_value=float(p.min) if p.min is not None else None,
                    max_value=float(p.max) if p.max is not None else None,
                    step=float(p.step or 0.1), format="%.4f", key=wkey,
                )

        st.divider()
        name_col, save_col = st.columns([2, 1])
        new_name = name_col.text_input(
            "Save current params as a new preset", value="",
            key=f"newpreset_{ac_key}", placeholder="e.g. Crypto 4h aggressive",
        )
        save_col.write("")
        if save_col.button("💾 Save preset", key=f"savepreset_{ac_key}"):
            nm = new_name.strip()
            if nm:
                strat_registry.save_strategy(Strategy(nm, logic_key, edited))
                strat_registry.save_assignment(ac_key, nm)
                st.session_state[preset_widget_key] = nm  # auto-select on rerun
                st.rerun()
            else:
                st.warning("Enter a preset name first.")

        if not strat_registry.is_builtin(base.name):
            if st.button(f"🗑 Delete preset “{base.name}”", key=f"delpreset_{ac_key}"):
                strat_registry.delete_strategy(base.name)
                st.session_state.pop(preset_widget_key, None)
                strat_registry.save_assignment(ac_key, strat_registry.DEFAULT_STRATEGY_NAME)
                st.rerun()
        else:
            st.caption("Built-in presets can't be deleted. Save a copy under a new name to edit.")

    return Strategy(base.name, logic_key, edited)


def render_radar(ac: AssetClass, focus_symbol: str | None = None) -> None:
    """Render the radar UI for one asset class.
    If `focus_symbol` is set, the grid pre-selects + scrolls to that row (used
    when the user clicks an alert in the sidebar)."""
    key = ac.key
    if "bust" not in st.session_state:
        st.session_state.bust = {}
    if key not in st.session_state.bust:
        st.session_state.bust[key] = 0

    st.caption(ac.description)

    # --- Strategy (logic) + Preset selection, persisted per asset class ---
    all_strategies = strat_registry.load_strategies()
    assigned_name = strat_registry.get_assignment(key)
    assigned_strat = all_strategies.get(assigned_name)
    assigned_logic = assigned_strat.logic_key if assigned_strat else strat_registry.DEFAULT_LOGIC_KEY

    logics = strat_registry.list_logics(key)
    logic_keys = [k for k, _ in logics]
    logic_labels = dict(logics)

    # TradingView-style toolbar: the existing selectors and actions share one row.
    c0, c1, c2, c3, r1, r2 = st.columns(
        [2.4, 2.4, 1.5, 2.3, 1, 1], gap="small", vertical_alignment="bottom"
    )
    logic_idx = logic_keys.index(assigned_logic) if assigned_logic in logic_keys else 0
    chosen_logic = c0.selectbox(
        "Strategy", logic_keys, index=logic_idx,
        format_func=lambda k: logic_labels[k], key=f"logic_{key}",
    )
    presets = strat_registry.presets_for_logic(chosen_logic, all_strategies)
    preset_names = list(presets.keys())
    # Preset dropdown key is scoped to the logic so switching strategy gives a
    # clean preset list (no stale-selection error).
    preset_widget_key = f"preset_{key}_{chosen_logic}"
    preset_idx = preset_names.index(assigned_name) if assigned_name in preset_names else 0
    chosen_preset = c1.selectbox("Preset", preset_names, index=preset_idx, key=preset_widget_key)
    if chosen_preset != assigned_name:
        strat_registry.save_assignment(key, chosen_preset)

    interval_label = c2.selectbox(
        "Timeframe", options=ac.interval_options,
        format_func=lambda x: x[0],
        index=ac.default_interval_idx if ac.ui_default_interval_idx is None else ac.ui_default_interval_idx,
        key=f"tf_{key}",
    )
    interval_minutes = interval_label[1]
    sort_options = list(SORT_MAP.keys())
    # Stocks default to newest trades first (pairs with the "Pos: Long" filter).
    default_sort = "Bars in state (newest first)" if key == "stocks" else sort_options[0]
    sort_by = c3.selectbox("Sort by", sort_options, index=sort_options.index(default_sort), key=f"sort_{key}")

    soft_refresh = r1.button("Refresh", key=f"refresh_{key}", type="primary")
    hard_refresh = r2.button("Force", key=f"force_{key}", help="Ignore disk cache")

    # Assignment hint: this tab uses (logic, preset); how to make it permanent.
    if _gh_token and _gh_repo:
        _persist = "Sidebar → **⬆ Commit presets to repo** saves this choice permanently (and for alerts)."
    else:
        _persist = "Add a GitHub token in secrets to enable permanent saving (sidebar)."
    st.caption(
        f"**{ac.label}** uses **{logic_labels[chosen_logic]}** · preset "
        f"**{chosen_preset}**. {_persist}"
    )

    # Param editor (returns the strategy with live-edited params for this session)
    strategy = _render_strategy_editor(key, chosen_logic, presets[chosen_preset], preset_widget_key)

    if soft_refresh or hard_refresh:
        st.session_state.bust[key] += 1
        st.cache_data.clear()
    force_refetch = hard_refresh

    # Bar cycle context (only meaningful for 24/7 markets)
    if ac.is_24_7:
        _now, _bar_open, _bar_next = _bar_cycle(interval_minutes)
        st.caption(
            f"⏱ Current {interval_label[0]} bar: **{_bar_open.strftime('%H:%M UTC')} → "
            f"{_bar_next.strftime('%H:%M UTC')}** · "
            f"{_fmt_hm(_now - _bar_open)} in, **{_fmt_hm(_bar_next - _now)} to next rollover** · "
            f"caches refresh at the rollover."
        )
    else:
        st.caption(
            "Non-24/7 market — data updates when the underlying exchange publishes a new close. "
            "Run during your local market hours for fresh prices."
        )

    # Load
    with st.spinner(f"Loading {ac.label.lower()} data…"):
        ok_rows, skipped_rows = load_universe_data(
            key, interval_minutes, st.session_state.bust[key], force_refetch
        )

    if not ok_rows:
        st.error(f"No {ac.label.lower()} symbols resolved. Check your network and try Refresh.")
        if skipped_rows:
            st.dataframe(pd.DataFrame(skipped_rows)[["symbol", "reason"]])
        return

    signals = [compute_signal(r, strategy, lookback_days) for r in ok_rows]
    # Stocks default to "Pos: Long" (only ~half the rows are shown), so only those
    # need the TF✓ lookup. The checkbox value from the previous run is already in
    # session_state; unticking it reruns and fills in the rest (fetches are cached).
    _conf_only: set[str] | None = None
    if key == "stocks" and st.session_state.get(f"long_only_{key}", True):
        _conf_only = {s["symbol"] for s in signals if s["state"] == "LONG"}
        if focus_symbol:
            _conf_only.add(focus_symbol)
    _add_confluence(
        signals,
        ok_rows,
        only_symbols=_conf_only,
        asset_key=key,
        strategy=strategy,
        interval_options=ac.interval_options,
        current_interval_minutes=interval_minutes,
        force_refetch=force_refetch,
    )

    # In-app alerts run only on the timeframe the cron uses (daily). Other views
    # (e.g. 1 week, 4 hour) neither seed a baseline nor send anything, so browsing
    # them can never fire extra Telegram messages.
    _alert_interval = ac.interval_options[ac.default_interval_idx]
    if not ac.alerts_enabled:
        st.caption(f"🔕 Alerts are turned off for {ac.label}.")
    elif interval_minutes == _alert_interval[1]:
        # Alert detection — per-asset-class state key prevents cross-contamination
        prev_alert_state = alerts.reseed_on_strategy_change(
            alerts.load_state(), key, strategy.name, strat_registry.DEFAULT_STRATEGY_NAME,
        )
        class_prefix = f"{key}|"
        interval_suffix = f"|{interval_minutes}"
        had_baseline = any(
            k.startswith(class_prefix) and k.endswith(interval_suffix)
            for k in prev_alert_state
        )
        flips, new_alert_state = alerts.detect_flips(
            signals, interval_minutes, prev_alert_state, asset_class=key,
        )
        alerts.save_state(new_alert_state)

        if not had_baseline:
            st.info(
                f"Seeded alert baseline for {len(signals)} {ac.label.lower()} symbols on this timeframe. "
                "Future flips will diff against this."
            )
        elif flips:
            # Record every real flip into the history feed (drives the sidebar list).
            alerts.record_flips(flips, asset_class=key)
            if alerts_enabled and bot_token and chat_id:
                sent, errs = alerts.fire_alerts(flips, bot_token, chat_id)
                if sent:
                    st.toast(f"📨 Sent {sent} Telegram alert(s) for {ac.label}", icon="📨")
                for e in errs:
                    st.warning(f"Alert failed for {e}")
            else:
                flip_summary = ", ".join(
                    f"{f.symbol} {'↗' if f.direction == 'ENTRY' else '↘'}" for f in flips
                )
                st.info(f"State flips detected (alerts disabled): {flip_summary}")
    else:
        st.caption(
            f"🔕 Alerts are evaluated on the {_alert_interval[0]} timeframe only (matching the scheduled job); "
            f"this {interval_label[0]} view doesn't send any."
        )

    # Headline strip
    long_count = sum(1 for s in signals if s["state"] == "LONG")
    green_filter = sum(1 for s in signals if s["filter_up"])
    covered = len(signals)
    total = len(ok_rows) + len(skipped_rows)
    cache_hits = sum(1 for r in ok_rows if r.get("cache_status") == "cache")
    stale_hits = sum(1 for r in ok_rows if r.get("cache_status") == "stale")

    h1, h2, h3, h4 = st.columns(4)
    h1.metric("Covered", f"{covered} / {total}")
    h2.metric("In long", long_count, delta=f"{long_count / covered * 100:.0f}%")
    h3.metric("Filter rising", green_filter, delta=f"{green_filter / covered * 100:.0f}%")
    h4.metric("Timeframe", interval_label[0])

    cache_msg = f"{cache_hits}/{covered} from cache"
    if stale_hits:
        cache_msg += f" · {stale_hits} stale (source errored)"
    st.caption(cache_msg)

    # Grid + drilldown
    df = pd.DataFrame(signals)
    df_display = df.drop(columns=["_df", "_overlays", "_state_series", "_trades", "_bo", "bo_date"]).copy()
    if "flow_delta_pct" in df_display.columns:
        df_display["flow_delta"] = df_display.pop("flow_delta_pct")
    sort_col, ascending = SORT_MAP[sort_by]
    # Tie-break by rank so each group (e.g. all LONG rows) reads top-mcap first.
    if sort_col == "rank":
        df_display = df_display.sort_values("rank", ascending=True, na_position="last")
    else:
        df_display = df_display.sort_values(
            [sort_col, "rank"], ascending=[ascending, True], na_position="last"
        )
    df_display = df_display.reset_index(drop=True)
    df_display["tv"] = df_display["pair"]
    # Grid column order follows COLUMN_FLEX; anything else (hidden helpers) trails.
    ordered = [c for c in COLUMN_FLEX if c in df_display.columns]
    df_display = df_display[ordered + [c for c in df_display.columns if c not in ordered]]

    # Chart and watchlist sit side by side; the grid keeps its horizontal scroll
    # so every original data column remains available in the narrower dock.
    chart_col, radar_col = st.columns([2.5, 1], gap="small", vertical_alignment="top")
    with radar_col.expander("Radar", expanded=True):
        st.caption("Click any cell in a row to drill down into that coin's chart.")
        long_only = False
        if key == "stocks":
            long_only = st.checkbox("Pos: Long", value=True, key=f"long_only_{key}",
                                    help="Show only stocks currently in a LONG position.")
        search = st.text_input(
            "Search", key=f"search_{key}", type="search", placeholder="Search ticker or name",
            live="300ms", label_visibility="collapsed",
        ).strip()
        st.markdown(
            """
            <div class="radar-help-row">
              <span class="radar-help-pill"><b>Fl</b>
                <span class="radar-help-bubble">
                  Filter slope. Checked means the strategy's core trend filter is rising right now.
                </span>
              </span>
              <span class="radar-help-pill"><b>vs HB</b>
                <span class="radar-help-bubble">
                  Close versus upper band. Positive means price is above the trigger band; negative means it is still below it.
                </span>
              </span>
              <span class="radar-help-pill"><b>StK</b>
                <span class="radar-help-bubble">
                  Stochastic RSI K value. A fast momentum gauge: high values mean hot momentum, low values mean washed-out momentum.
                </span>
              </span>
              <span class="radar-help-pill"><b>MaxDD</b>
                <span class="radar-help-bubble">
                  Maximum drawdown over the selected lookback. It shows the worst peak-to-trough equity drop the strategy suffered.
                </span>
              </span>
            </div>
            """,
            unsafe_allow_html=True,
        )
        # Filter rows server-side: AgGrid only renders the rows in view, so the
        # browser's Ctrl+F misses tickers further down the list.
        df_base = df_display
        if long_only:
            # Keep an alert deep-link's row visible even if it just flipped to FLAT.
            keep = (df_display["state"] == "LONG") | (df_display["symbol"] == focus_symbol)
            df_base = df_display[keep].reset_index(drop=True)
            if df_base.empty:
                st.info(f"No {ac.label.lower()} are LONG right now. Untick “Pos: Long” to see all rows.")
                df_base = df_display
        df_grid = df_base
        if search:
            needle = search.lower()
            hay = [df_base[c].astype(str).str.lower() for c in ("symbol", "name", "pair") if c in df_base.columns]
            mask = hay[0].str.contains(needle, regex=False)
            for col in hay[1:]:
                mask |= col.str.contains(needle, regex=False)
            # Exact ticker match first, then the current sort order.
            exact = hay[0] == needle
            df_grid = pd.concat([df_base[mask & exact], df_base[mask & ~exact]]).reset_index(drop=True)
            scope = "LONG " if df_base is not df_display else ""
            if df_grid.empty:
                hint = " or untick “Pos: Long”" if scope else ""
                st.info(f"No {scope}{ac.label.lower()} match “{search}”. Clear the search{hint} to see more rows.")
                df_grid = df_base
            else:
                st.caption(f"{len(df_grid)} of {len(df_base)} {scope}rows match “{search}”.")
        grid_opts = build_grid_options(df_grid, PALETTE)
        # If we arrived via an alert deep-link, mark that row as pre-selected so
        # AgGrid highlights + ensures it's visible on first render.
        if focus_symbol and focus_symbol in set(df_grid["symbol"]):
            for row in grid_opts.get("rowData", []) or []:
                if row.get("symbol") == focus_symbol:
                    row["__pre_selected__"] = True
            grid_opts["onFirstDataRendered"] = JsCode("""
            function(p) {
                let node = null;
                p.api.forEachNode(n => { if (n.data && n.data.__pre_selected__) node = n; });
                if (node) {
                    node.setSelected(true);
                    p.api.ensureNodeVisible(node, 'middle');
                }
            }
            """)
        # Bust the AgGrid widget key when a focus changes so the renderer hook re-fires.
        grid_response = AgGrid(
            df_grid,
            gridOptions=grid_opts,
            height=grid_height,
            # Rerun only when the selected row changes (sorting/filtering stay client-side).
        update_on=["selectionChanged"],
            allow_unsafe_jscode=True,
            # Flex weights + per-column minWidth size the columns. fit_columns_on_grid_load
            # squeezed all ~22 columns into the viewport below their minWidth, truncating
            # every value; without it the grid scrolls horizontally on narrow windows.
            fit_columns_on_grid_load=False,
            theme=PALETTE["AGGRID_THEME"],
            key=f"grid_{key}_{sort_by}_{focus_symbol or ''}_{search.lower()}_{int(long_only)}",
        )

    if skipped_rows:
        with radar_col.expander(f"Skipped ({len(skipped_rows)})"):
            st.dataframe(pd.DataFrame(skipped_rows)[["symbol", "reason"]], width="stretch")

    # A fixed-height chart dock aligns with the watchlist. Its existing detail
    # sections remain accessible by scrolling inside the dock.
    with chart_col.container(height=grid_height + 160, border=False, key=f"drilldown_pane_{key}"):
        selected = grid_response.get("selected_rows")
        selected_sym: str | None = None
        if isinstance(selected, pd.DataFrame) and not selected.empty:
            selected_sym = selected.iloc[0]["symbol"]
        elif isinstance(selected, list) and selected:
            selected_sym = selected[0].get("symbol")
        # Alert deep-link takes priority over the default-first fallback.
        if not selected_sym and focus_symbol and focus_symbol in set(df_grid["symbol"]):
            selected_sym = focus_symbol
        if not selected_sym:
            selected_sym = df_grid.iloc[0]["symbol"]

        sel = next(s for s in signals if s["symbol"] == selected_sym)

        with st.expander(f"Drilldown — {selected_sym}", expanded=True):
            _spacer, action_col = st.columns([4, 1])
            with action_col:
                tear_sheet = _compute_tear_sheet(
                    sel.get("_trades", []),
                    now=sel["_df"].index[-1],
                    lookback_days_=lookback_days,
                    latest_ts=sel["_df"].index[-1],
                    latest_close=float(sel["_df"]["close"].iloc[-1]),
                )
                with st.popover("Tear sheet", width="stretch"):
                    st.caption(f"{selected_sym} · {lookback_days}d strategy report")
                    ts1, ts2 = st.columns(2)
                    ts1.metric("Sharpe", _fmt_number(tear_sheet["sharpe"]))
                    ts2.metric("Sortino", _fmt_number(tear_sheet["sortino"]))
                    ts3, ts4 = st.columns(2)
                    ts3.metric("Calmar", _fmt_number(tear_sheet["calmar"]))
                    ts4.metric("Profit factor", _fmt_number(tear_sheet["profit_factor"]))
                    ts5, ts6 = st.columns(2)
                    ts5.metric("Recovery", _fmt_number(tear_sheet["recovery_factor"]))
                    ts6.metric("Expectancy", _fmt_number(tear_sheet["expectancy_pct"], pct=True))
                    ts7, ts8 = st.columns(2)
                    ts7.metric("Net return", _fmt_number(tear_sheet["net_return_pct"], pct=True))
                    ts8.metric("Max drawdown", _fmt_number(tear_sheet["max_dd_pct"], pct=True))
                    ts9, ts10 = st.columns(2)
                    ts9.metric("Trades", str(tear_sheet["trades"]))
                    ts10.metric("Win rate", _fmt_number(tear_sheet["win_rate"], pct=True))
                    if not tear_sheet["recent_trades"].empty:
                        st.caption("Recent trades")
                        st.dataframe(tear_sheet["recent_trades"], width="stretch", hide_index=True)
            st.caption(f"{sel['name']} · {sel['pair']} · last 150 bars")

            chart_df = sel["_df"].copy()
            overlays = sel.get("_overlays")
            overlay_cols = []
            if overlays is not None:
                for col in ("filt", "hband", "lband"):
                    if col in overlays.columns:
                        chart_df[col] = overlays[col]
                        overlay_cols.append(col)
            # Transitions in the state series = trade signals.
            #   0 -> 1 : BUY (entry)
            #   1 -> 0 : SELL (exit)
            _state = sel["_state_series"].astype(int)
            _shift = _state.shift(1).fillna(0).astype(int)
            chart_df["signal"] = ""
            chart_df.loc[(_state == 1) & (_shift == 0), "signal"] = "BUY"
            chart_df.loc[(_state == 0) & (_shift == 1), "signal"] = "SELL"
            chart_df = chart_df.tail(150).reset_index().rename(columns={"ts": "time"})

            # Chart height tracks the grid height so the two panes stay aligned.
            chart_height = max(grid_height - 160, 240)

            # Chart guidance from UUPM charts.csv:
            #   - line + hover + zoom (Interactive Level for "Trend Over Time")
            #   - bullish #26A69A / bearish #EF5350 (Stock/Trading OHLC palette)
            #   - differentiate series by line style, not only color
            #   - axis grid muted, no zero-anchored Y (use scale.zero=False)
            x_axis = alt.Axis(grid=False, labelColor=PALETTE["FG_MUTED"], tickColor=PALETTE["BORDER"],
                              domainColor=PALETTE["BORDER"], title=None)
            y_axis = alt.Axis(grid=True, gridColor=PALETTE["BORDER"], gridOpacity=PALETTE["GRID_OPACITY"],
                              labelColor=PALETTE["FG_MUTED"], tickColor=PALETTE["BORDER"],
                              domainColor=PALETTE["BORDER"], title=None)
            base = alt.Chart(chart_df).encode(x=alt.X("time:T", axis=x_axis))

            layers = [
                base.mark_line(color=PALETTE["NEUTRAL_LINE"], strokeWidth=1.4).encode(
                    y=alt.Y("close:Q", axis=y_axis, scale=alt.Scale(zero=False)),
                )
            ]
            if "filt" in overlay_cols:
                layers.append(base.mark_line(color=PALETTE["BULLISH"], strokeWidth=2).encode(y="filt:Q"))
            if "hband" in overlay_cols:
                # Dashed = "channel boundary, not the trend itself" (series style cue).
                layers.append(base.mark_line(
                    color=PALETTE["BULLISH"], strokeWidth=1, opacity=0.55, strokeDash=[4, 3],
                ).encode(y="hband:Q"))
            if "lband" in overlay_cols:
                layers.append(base.mark_line(
                    color=PALETTE["BEARISH"], strokeWidth=1, opacity=0.55, strokeDash=[4, 3],
                ).encode(y="lband:Q"))

            # Breakout box: resistance / support lines + marker (latest event,
            # plus the current unbroken range when there is one).
            bo_state = sel.get("_bo")
            win_start, win_end = chart_df["time"].iloc[0], chart_df["time"].iloc[-1]
            bo_lines = []
            if bo_state is not None:
                ev = bo_state.last
                if ev is not None and ev.date >= win_start:
                    bo_lines += [("Resistance", ev.resistance, ev.resistance_start, ev.date, PALETTE["BEARISH"]),
                                 ("Support", ev.support, ev.support_start, ev.date, PALETTE["BULLISH"])]
                if bo_state.resistance is not None:
                    bo_lines.append(("Range top", bo_state.resistance, bo_state.resistance_start, win_end,
                                     PALETTE["BEARISH"]))
                    if bo_state.support is not None:
                        bo_lines.append(("Range floor", bo_state.support, bo_state.support_start, win_end,
                                         PALETTE["BULLISH"]))
            for label, level, start, end, colour in bo_lines:
                seg = pd.DataFrame({"t0": [max(start, win_start)], "t1": [end], "y": [level], "label": [label]})
                layers.append(
                    alt.Chart(seg).mark_rule(color=colour, strokeWidth=1.6, strokeDash=[2, 3]).encode(
                        x=alt.X("t0:T", axis=x_axis), x2="t1:T", y="y:Q",
                        tooltip=[alt.Tooltip("label:N", title="Level"),
                                 alt.Tooltip("y:Q", title="Price", format=".6g")],
                    )
                )
            if bo_state is not None and bo_state.last is not None and bo_state.last.date >= win_start:
                ev = bo_state.last
                pt = chart_df[chart_df["time"] == ev.date]
                if not pt.empty:
                    layers.append(
                        alt.Chart(pt.assign(status=ev.status)).mark_point(
                            shape="diamond", color=PALETTE["FG_PRIMARY"], filled=True, size=140,
                        ).encode(
                            x=alt.X("time:T", axis=x_axis), y="close:Q",
                            tooltip=[alt.Tooltip("time:T", title="Breakout"),
                                     alt.Tooltip("status:N", title="Status"),
                                     alt.Tooltip("close:Q", title="Close", format=".6g")],
                        )
                    )

            buys = chart_df[chart_df["signal"] == "BUY"]
            sells = chart_df[chart_df["signal"] == "SELL"]
            if not buys.empty:
                layers.append(
                    alt.Chart(buys).mark_point(
                        shape="triangle-up", color=PALETTE["BULLISH"], filled=True,
                        size=180, stroke=PALETTE["BG_BASE"], strokeWidth=1.5,
                    ).encode(
                        x=alt.X("time:T", axis=x_axis), y="close:Q",
                        tooltip=[alt.Tooltip("time:T", title="Buy"),
                                 alt.Tooltip("close:Q", title="Price", format=".6g")],
                    )
                )
            if not sells.empty:
                layers.append(
                    alt.Chart(sells).mark_point(
                        shape="triangle-down", color=PALETTE["BEARISH"], filled=True,
                        size=180, stroke=PALETTE["BG_BASE"], strokeWidth=1.5,
                    ).encode(
                        x=alt.X("time:T", axis=x_axis), y="close:Q",
                        tooltip=[alt.Tooltip("time:T", title="Sell"),
                                 alt.Tooltip("close:Q", title="Price", format=".6g")],
                    )
                )

            # Nearest-point hover (vertical rule + multi-series tooltip).
            hover = alt.selection_point(
                fields=["time"], nearest=True, on="pointerover", empty=False, clear="pointerout",
            )
            tooltip_fields = [alt.Tooltip("time:T", title="Time"),
                              alt.Tooltip("close:Q", title="Close", format=".6g")]
            if "filt" in overlay_cols:
                tooltip_fields.append(alt.Tooltip("filt:Q", title="Filter", format=".6g"))
            if "hband" in overlay_cols:
                tooltip_fields.append(alt.Tooltip("hband:Q", title="HBand", format=".6g"))
            layers.append(
                base.mark_rule(color=PALETTE["FG_MUTED"], opacity=0.0).encode(
                    opacity=alt.condition(hover, alt.value(0.5), alt.value(0.0)),
                    tooltip=tooltip_fields,
                ).add_params(hover)
            )

            chart = alt.layer(*layers).properties(
                height=chart_height,
                background=PALETTE["BG_CARD"],
            ).configure_view(stroke=None).interactive(bind_y=False)
            with st.container():
                st.altair_chart(chart, width="stretch")

                mc1, mc2, mc3, mc4, mc5 = st.columns(5)
                mc1.metric("State", sel["state"])
                mc2.metric("Bars in state", sel["bars_in_state"])
                mc3.metric("Stoch K", f"{sel['stoch_k']:.1f}" if sel["stoch_k"] is not None else "—")
                mc4.metric("Close vs HBand", f"{sel['close_vs_hband_pct']:+.2f}%")
                mc5.metric("Taker flow", f"{sel['flow_delta_pct']:+.1f}%" if sel.get("flow_delta_pct") is not None else "—")

                bo_state = sel.get("_bo")
                ev = bo_state.last if bo_state is not None else None
                bc1, bc2, bc3, bc4 = st.columns(4)
                if ev is not None:
                    bc1.metric("Last breakout", str(ev.date.date()), delta=ev.status.capitalize(),
                               delta_color={"validated": "normal", "invalidated": "inverse"}.get(ev.status, "off"))
                    bc2.metric("Resistance", f"{ev.resistance:.6g}")
                    bc3.metric("Support", f"{ev.support:.6g}")
                else:
                    bc1.metric("Last breakout", "—")
                    bc2.metric("Resistance", "—")
                    bc3.metric("Support", "—")
                if bo_state is not None and bo_state.resistance is not None:
                    dist = (sel["last_close"] / bo_state.resistance - 1) * 100
                    bc4.metric("Current range top", f"{bo_state.resistance:.6g}",
                               delta=f"{dist:+.1f}% from close", delta_color="off")
                else:
                    bc4.metric("Current range top", "—")

                with st.expander("Strategy comparison", expanded=False):
                    compare_strategies = strat_registry.strategies_for_asset(key)
                    compare_names = list(compare_strategies.keys())
                    default_left = strategy.name if strategy.name in compare_strategies else compare_names[0]
                    preferred_right = next(
                        (
                            candidate for candidate in (
                                "Supertrend v1 (10, 3.0)",
                                "EMA Cross v1 (21/55)",
                                "Donchian Breakout v1.0 (20/10)",
                                "GaussianChannel v3.1 (default)",
                            )
                            if candidate in compare_strategies and candidate != default_left
                        ),
                        next((name for name in compare_names if name != default_left), default_left),
                    )
                    cmp1, cmp2 = st.columns(2)
                    left_name = cmp1.selectbox(
                        "Strategy A",
                        compare_names,
                        index=compare_names.index(default_left),
                        key=f"cmp_left_{key}_{selected_sym}",
                    )
                    right_name = cmp2.selectbox(
                        "Strategy B",
                        compare_names,
                        index=compare_names.index(preferred_right),
                        key=f"cmp_right_{key}_{selected_sym}",
                    )

                    left_result = strat_registry.run_strategy_cached(compare_strategies[left_name], sel["_df"])
                    right_result = strat_registry.run_strategy_cached(compare_strategies[right_name], sel["_df"])
                    latest_ts = sel["_df"].index[-1]
                    latest_close = float(sel["_df"]["close"].iloc[-1])
                    now_ts = latest_ts
                    left_stats = compute_stats(left_result.trades, now=now_ts, lookback_days=lookback_days)
                    right_stats = compute_stats(right_result.trades, now=now_ts, lookback_days=lookback_days)

                    cmp_curves = pd.concat(
                        [
                            _comparison_curve_frame(
                                left_name,
                                left_result,
                                now=now_ts,
                                lookback_days_=lookback_days,
                                latest_ts=latest_ts,
                                latest_close=latest_close,
                            ),
                            _comparison_curve_frame(
                                right_name,
                                right_result,
                                now=now_ts,
                                lookback_days_=lookback_days,
                                latest_ts=latest_ts,
                                latest_close=latest_close,
                            ),
                        ],
                        ignore_index=True,
                    )
                    cmp_chart = (
                        alt.Chart(cmp_curves)
                        .mark_line(strokeWidth=2.2)
                        .encode(
                            x=alt.X("time:T", axis=x_axis),
                            y=alt.Y("equity:Q", axis=y_axis, scale=alt.Scale(zero=False)),
                            color=alt.Color(
                                "strategy:N",
                                legend=alt.Legend(title=None, orient="top"),
                                scale=alt.Scale(range=[PALETTE["BULLISH"], PALETTE["ACCENT"]]),
                            ),
                            strokeDash=alt.StrokeDash(
                                "strategy:N",
                                legend=None,
                                scale=alt.Scale(range=[[1, 0], [5, 3]]),
                            ),
                            tooltip=[
                                alt.Tooltip("time:T", title="Time"),
                                alt.Tooltip("strategy:N", title="Strategy"),
                                alt.Tooltip("equity:Q", title="Equity", format=".3f"),
                            ],
                        )
                        .properties(height=220, background=PALETTE["BG_CARD"])
                        .configure_view(stroke=None)
                    )
                    st.altair_chart(cmp_chart, width="stretch")

                    ca1, ca2, ca3 = st.columns(3)
                    with ca1:
                        st.caption(left_name)
                        st.metric("Net", f"{left_stats.net_pct:+.2f}%")
                        st.metric("Sharpe", _fmt_number(left_stats.sharpe))
                        st.metric("Max DD", _fmt_number(left_stats.max_drawdown_pct, pct=True))
                    with ca2:
                        st.caption(right_name)
                        st.metric("Net", f"{right_stats.net_pct:+.2f}%")
                        st.metric("Sharpe", _fmt_number(right_stats.sharpe))
                        st.metric("Max DD", _fmt_number(right_stats.max_drawdown_pct, pct=True))
                    with ca3:
                        st.caption("Head-to-head")
                        st.metric("Net edge", f"{(left_stats.net_pct - right_stats.net_pct):+.2f}%")
                        if left_stats.sharpe is not None and right_stats.sharpe is not None:
                            st.metric("Sharpe edge", _fmt_number(left_stats.sharpe - right_stats.sharpe))
                        else:
                            st.metric("Sharpe edge", "—")
                        if left_stats.max_drawdown_pct is not None and right_stats.max_drawdown_pct is not None:
                            st.metric("DD edge", _fmt_number(left_stats.max_drawdown_pct - right_stats.max_drawdown_pct, pct=True))
                        else:
                            st.metric("DD edge", "—")

    if key in live_log.LIVE_CLASSES:
        perf_ui.render_basket_performance(signals, key, PALETTE)

# --- Page header + tabs --------------------------------------------------------

st.title("Trend Radar")
st.caption(
    "Per-asset-class trend strategies across crypto / stocks / metals / commodities. "
    "Each tab picks its own strategy; defaults to GaussianChannel v3.1."
)

# Deep-link support: ?tab=stocks&symbol=AAPL pre-opens that tab + focuses that
# coin in its grid. Sidebar alert items use this to "jump to" the asset.
_qp = st.query_params
_jump_class = (_qp.get("tab") or "").strip().lower() if hasattr(_qp, "get") else ""
_jump_symbol = (_qp.get("symbol") or "").strip().upper() if hasattr(_qp, "get") else ""

ui_tweaks.install_grid_resize()
# on_change="rerun" makes Streamlit run only the selected tab's code (by default
# every tab computes on every rerun). Switching tabs reruns and renders the new one.
tabs = st.tabs([ac.label for ac in ASSET_CLASSES], default="Stocks", on_change="rerun", key="asset_tabs")
for tab, ac in zip(tabs, ASSET_CLASSES):
    with tab:
        if tab.open:
            render_radar(ac, focus_symbol=(_jump_symbol if _jump_class == ac.key else None))

# Streamlit can't switch tabs from Python; nudge it via a tiny JS snippet that
# clicks the matching tab button on page load when ?tab= is present.
if _jump_class in {ac.key for ac in ASSET_CLASSES}:
    _label = next(ac.label for ac in ASSET_CLASSES if ac.key == _jump_class)
    st.markdown(
        f"""
        <script>
        (function() {{
            const want = {json.dumps(_label)};
            const tabs = window.parent.document.querySelectorAll('button[role="tab"]');
            for (const t of tabs) {{
                if (t.innerText.trim() === want && t.getAttribute('aria-selected') !== 'true') {{
                    t.click();
                    break;
                }}
            }}
        }})();
        </script>
        """,
        unsafe_allow_html=True,
    )


# --- Sidebar alerts feed (rendered last so this rerun's flips are included) ---

def _fmt_ago(ts_iso: str) -> str:
    try:
        from datetime import datetime, timezone
        ts = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - ts
        s = int(delta.total_seconds())
        if s < 60:    return f"{s}s ago"
        if s < 3600:  return f"{s // 60}m ago"
        if s < 86400: return f"{s // 3600}h ago"
        return f"{s // 86400}d ago"
    except Exception:
        return ts_iso


_class_labels = {ac.key: ac.label for ac in ASSET_CLASSES}

with _alerts_slot:
    with st.expander("🔔 Alerts", expanded=False):
        history = alerts.load_history()
        # Newest first
        history = sorted(history, key=lambda e: e.get("ts", ""), reverse=True)

        filter_options = ["All", *(ac.label for ac in ASSET_CLASSES)]
        cf, cc = st.columns([3, 1])
        chosen_filter = cf.selectbox("Filter", filter_options, key="alerts_filter")
        if cc.button("🗑", key="alerts_clear", help="Clear all alerts"):
            alerts.clear_history()
            st.rerun()

        if chosen_filter != "All":
            target_key = next(ac.key for ac in ASSET_CLASSES if ac.label == chosen_filter)
            history = [e for e in history if e.get("asset_class") == target_key]

        if not history:
            st.caption("No alerts yet. New FLAT ↔ LONG flips appear here.")
        else:
            st.caption(f"{len(history)} alert(s) · newest first · click to jump")
            for i, e in enumerate(history[:80]):  # cap rendered count
                ac_key = e.get("asset_class", "")
                ac_label = _class_labels.get(ac_key, ac_key)
                sym = e.get("symbol", "?")
                direction = e.get("direction", "")
                arrow = "🟢" if direction == "ENTRY" else "🔴"
                verb = "LONG" if direction == "ENTRY" else "EXIT"
                price = e.get("price")
                price_str = f" @ {price:.6g}" if isinstance(price, (int, float)) else ""
                label = f"{arrow} {sym} {verb} · {ac_label} · {_fmt_ago(e.get('ts', ''))}"
                if st.button(label, key=f"alert_jump_{i}", width="stretch",
                             help=f"Open {sym} in the {ac_label} tab{price_str}"):
                    st.query_params["tab"] = ac_key
                    st.query_params["symbol"] = sym
                    st.rerun()
