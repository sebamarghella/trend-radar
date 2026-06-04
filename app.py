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
from st_aggrid import AgGrid, GridOptionsBuilder, GridUpdateMode
from st_aggrid.shared import JsCode

import alerts
import cache as ohlc_cache
from asset_classes import ASSET_CLASSES, AssetClass
from gaussian_channel import (
    BAR_COLORS,
    GCParams,
    compute_stats,
    gaussian_channel,
    replay_strategy,
    stoch_rsi_k,
)
from sources import Resolver, SourceError


st.set_page_config(
    page_title="Trend Radar — GaussianChannel v3.1",
    layout="wide",
    initial_sidebar_state="expanded",
)


# --- Global sidebar (shared across all tabs) -----------------------------------

st.sidebar.header("Gaussian Channel")
poles = st.sidebar.slider("Poles (N)", 1, 9, 4)
period = st.sidebar.number_input("Sampling period", min_value=2, value=144, step=1)
multiplier = st.sidebar.number_input("TR multiplier", min_value=0.0, value=1.414, step=0.1, format="%.3f")
reduced_lag = st.sidebar.checkbox("Reduced lag mode", value=False)
fast_response = st.sidebar.checkbox("Fast response mode", value=False)

st.sidebar.header("Stoch RSI")
rsi_length = st.sidebar.number_input("RSI length", min_value=2, value=14, step=1)
stoch_length = st.sidebar.number_input("Stoch length", min_value=2, value=14, step=1)
smooth_k = st.sidebar.number_input("Smooth K", min_value=1, value=3, step=1)

st.sidebar.caption(
    "Strategy: long when Gaussian filter is rising AND close > upper band AND "
    "stoch K > 80 or < 20. Exit when close crosses below upper band."
)

st.sidebar.header("Backtest")
lookback_days = st.sidebar.slider(
    "Lookback (days)", min_value=30, max_value=365, value=180, step=15,
    help="Window for trade count / net % / win % in the radar grid.",
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


def fetch_one(base: str, resolver: Resolver, interval: int, force_refresh: bool = False) -> dict:
    hit = resolver.resolve(base)
    if hit is None:
        return {"symbol": base, "ok": False, "reason": "not on any source"}
    src, resolved = hit
    cached_df = None
    if not force_refresh:
        cached_df = ohlc_cache.load(src.name, resolved, interval)
        if cached_df is not None and ohlc_cache.is_fresh(cached_df, interval):
            return {
                "symbol": base, "ok": True, "pair": resolved,
                "exchange": src.name, "exchange_short": src.short, "tv_prefix": src.tv_prefix,
                "df": cached_df, "cache_status": "cache",
            }
    try:
        df = src.fetch_with_retry(resolved, interval_minutes=interval)
    except SourceError as e:
        if cached_df is not None:
            return {
                "symbol": base, "ok": True, "pair": resolved,
                "exchange": src.name, "exchange_short": src.short, "tv_prefix": src.tv_prefix,
                "df": cached_df, "cache_status": "stale",
            }
        return {"symbol": base, "ok": False, "reason": f"{src.name}: {e}"}
    if len(df) < 60:
        return {"symbol": base, "ok": False, "reason": f"insufficient history on {src.name}"}
    ohlc_cache.save(src.name, resolved, interval, df)
    return {
        "symbol": base, "ok": True, "pair": resolved,
        "exchange": src.name, "exchange_short": src.short, "tv_prefix": src.tv_prefix,
        "df": df, "cache_status": "fresh",
    }


@st.cache_data(ttl=15 * 60, show_spinner=False)
def load_universe_data(
    asset_key: str, interval: int, bust: int, force_refresh: bool,
) -> tuple[list[dict], list[dict]]:
    ac = next(a for a in ASSET_CLASSES if a.key == asset_key)
    resolver = cached_resolver(asset_key)
    universe = ac.universe
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=20) as ex:
        futures = {
            ex.submit(fetch_one, c["symbol"], resolver, interval, force_refresh): c
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


def compute_signal(
    row: dict,
    gc_params: GCParams,
    rsi_len: int,
    stoch_len: int,
    sm_k: int,
    lookback_days_: int,
) -> dict:
    df = row["df"]
    channel = gaussian_channel(df, gc_params)
    k = stoch_rsi_k(df["close"].to_numpy(), rsi_len, stoch_len, sm_k)
    snap, state, trades = replay_strategy(df, channel, k)
    stats = compute_stats(trades, now=df.index[-1], lookback_days=lookback_days_)
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
        "last_filt": snap.last_filt,
        "last_hband": snap.last_hband,
        "last_lband": snap.last_lband,
        "trades": stats.trades,
        "net_pct": stats.net_pct,
        "win_pct": stats.win_pct,
        "_df": df,
        "_channel": channel,
        "_state_series": state,
    }


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


_CELLSTYLE_STATE = JsCode("""
function(p) {
    if (p.value === 'LONG') {
        return { backgroundColor: '#0aff68', color: 'black', fontWeight: 700 };
    }
    return { backgroundColor: '#2b2b2b', color: '#aaaaaa' };
}
""")

_BAR_COLORS_JSON = ",".join(f"'{k}':'{v}'" for k, v in BAR_COLORS.items())
_CELLSTYLE_BAR = JsCode(f"""
function(p) {{
    const colors = {{{_BAR_COLORS_JSON}}};
    return {{ backgroundColor: colors[p.value] || '#cccccc', color: 'black', fontWeight: 600 }};
}}
""")

_CELLSTYLE_FILTER = JsCode("""
function(p) {
    if (p.value === true) return { color: '#0aff68', fontWeight: 600 };
    return { color: '#ff0a5a', fontWeight: 600 };
}
""")

_CELLSTYLE_PCT = JsCode("""
function(p) {
    if (p.value == null) return {};
    return p.value > 0 ? { color: '#00752d' } : { color: '#ff0a5a' };
}
""")

_FMT_PCT = JsCode("function(p) { return p.value != null ? (p.value >= 0 ? '+' : '') + p.value.toFixed(2) + '%' : ''; }")
_FMT_K = JsCode("function(p) { return p.value != null ? p.value.toFixed(1) : ''; }")
_FMT_PRICE = JsCode("function(p) { return p.value != null ? p.value.toPrecision(6) : ''; }")
_FMT_INT = JsCode("function(p) { return p.value != null ? p.value.toFixed(0) : ''; }")
_FMT_WIN = JsCode("function(p) { return p.value != null ? p.value.toFixed(0) + '%' : '—'; }")

TV_CHART_ID = "6O2rb5Ql"

_TV_VALUE_FMT = JsCode("function(p) { return p.value ? 'TV ↗' : ''; }")

_TV_CELL_STYLE = {
    "cursor": "pointer",
    "color": "#00752d",
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


def build_grid_options(df: pd.DataFrame) -> dict:
    gb = GridOptionsBuilder.from_dataframe(df)
    gb.configure_default_column(resizable=True, sortable=True, filterable=False)
    gb.configure_selection(selection_mode="single", use_checkbox=False, suppressRowDeselection=True)
    gb.configure_column("rank", header_name="#", maxWidth=60, type=["numericColumn"])
    gb.configure_column("symbol", header_name="Sym", maxWidth=90, pinned="left")
    gb.configure_column("name", header_name="Name", minWidth=120)
    gb.configure_column("exchange_short", header_name="Src", maxWidth=70)
    gb.configure_column("pair", header_name="Pair", maxWidth=120)
    gb.configure_column("exchange", hide=True)
    gb.configure_column("tv_prefix", hide=True)
    gb.configure_column("state", header_name="Pos", maxWidth=70, cellStyle=_CELLSTYLE_STATE)
    gb.configure_column("bar_color", header_name="Bar", maxWidth=120, cellStyle=_CELLSTYLE_BAR)
    gb.configure_column("filter_up", header_name="F↑", maxWidth=60, cellStyle=_CELLSTYLE_FILTER)
    gb.configure_column("bars_in_state", header_name="Bars", maxWidth=70, type=["numericColumn"])
    gb.configure_column(
        "close_vs_hband_pct", header_name="vs HB", maxWidth=90,
        type=["numericColumn"], valueFormatter=_FMT_PCT, cellStyle=_CELLSTYLE_PCT,
    )
    gb.configure_column("stoch_k", header_name="StK", maxWidth=70, type=["numericColumn"], valueFormatter=_FMT_K)
    gb.configure_column("last_close", header_name="Close", type=["numericColumn"], valueFormatter=_FMT_PRICE)
    gb.configure_column("trades", header_name="Trades", maxWidth=80, type=["numericColumn"], valueFormatter=_FMT_INT)
    gb.configure_column(
        "net_pct", header_name="Net %", maxWidth=100,
        type=["numericColumn"], valueFormatter=_FMT_PCT, cellStyle=_CELLSTYLE_PCT,
    )
    gb.configure_column("win_pct", header_name="Win %", maxWidth=85, type=["numericColumn"], valueFormatter=_FMT_WIN)
    gb.configure_column(
        "tv", header_name="TV", maxWidth=70,
        sortable=False, filter=False,
        valueFormatter=_TV_VALUE_FMT, cellStyle=_TV_CELL_STYLE,
    )
    for hidden in ("last_filt", "last_hband", "last_lband"):
        gb.configure_column(hidden, hide=True)
    gb.configure_grid_options(onCellClicked=_TV_CLICK_HANDLER)
    return gb.build()


# --- Per-tab render ------------------------------------------------------------


SORT_MAP = {
    "Rank": ("rank", True),
    "State (long first)": ("state", False),
    "Bars in state": ("bars_in_state", False),
    "Stoch K": ("stoch_k", False),
    "Close vs HBand %": ("close_vs_hband_pct", False),
    "Net %": ("net_pct", False),
    "Win %": ("win_pct", False),
    "Trades": ("trades", False),
}


def render_radar(ac: AssetClass, gc_params: GCParams) -> None:
    """Render the radar UI for one asset class."""
    key = ac.key
    if "bust" not in st.session_state:
        st.session_state.bust = {}
    if key not in st.session_state.bust:
        st.session_state.bust[key] = 0

    st.caption(ac.description)

    # Per-tab controls row
    c1, c2, c3, c4 = st.columns([2, 2, 1, 1])
    interval_label = c1.selectbox(
        "Timeframe", options=ac.interval_options,
        format_func=lambda x: x[0], index=ac.default_interval_idx,
        key=f"tf_{key}",
    )
    interval_minutes = interval_label[1]
    sort_by = c2.selectbox("Sort by", list(SORT_MAP.keys()), key=f"sort_{key}")
    with c3:
        st.write("")
        soft_refresh = st.button("Refresh", key=f"refresh_{key}", type="primary")
    with c4:
        st.write("")
        hard_refresh = st.button("Force", key=f"force_{key}", help="Ignore disk cache")

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

    signals = [
        compute_signal(r, gc_params, rsi_length, stoch_length, smooth_k, lookback_days)
        for r in ok_rows
    ]

    # Alert detection — per-asset-class state key prevents cross-contamination
    prev_alert_state = alerts.load_state()
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
    elif flips and alerts_enabled and bot_token and chat_id:
        sent, errs = alerts.fire_alerts(flips, bot_token, chat_id)
        if sent:
            st.toast(f"📨 Sent {sent} Telegram alert(s) for {ac.label}", icon="📨")
        for e in errs:
            st.warning(f"Alert failed for {e}")
    elif flips:
        flip_summary = ", ".join(
            f"{f.symbol} {'↗' if f.direction == 'ENTRY' else '↘'}" for f in flips
        )
        st.info(f"State flips detected (alerts disabled): {flip_summary}")

    # Headline strip
    long_count = sum(1 for s in signals if s["state"] == "LONG")
    green_filter = sum(1 for s in signals if s["filter_up"])
    covered = len(signals)
    total = len(ac.universe)
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
    df_display = df.drop(columns=["_df", "_channel", "_state_series"]).copy()
    sort_col, ascending = SORT_MAP[sort_by]
    df_display = df_display.sort_values(sort_col, ascending=ascending, na_position="last").reset_index(drop=True)
    df_display["tv"] = df_display["pair"]

    left, right = st.columns([1, 1], gap="large")
    with left:
        st.subheader("Radar")
        st.caption("Click any cell in a row to drill down into that coin's chart.")
        grid_response = AgGrid(
            df_display,
            gridOptions=build_grid_options(df_display),
            height=620,
            update_mode=GridUpdateMode.SELECTION_CHANGED,
            allow_unsafe_jscode=True,
            fit_columns_on_grid_load=False,
            theme="balham-dark",
            key=f"grid_{key}_{sort_by}",
        )

    selected = grid_response.get("selected_rows")
    selected_sym: str | None = None
    if isinstance(selected, pd.DataFrame) and not selected.empty:
        selected_sym = selected.iloc[0]["symbol"]
    elif isinstance(selected, list) and selected:
        selected_sym = selected[0].get("symbol")
    if not selected_sym:
        selected_sym = df_display.iloc[0]["symbol"]

    sel = next(s for s in signals if s["symbol"] == selected_sym)

    with right:
        st.subheader(f"Drilldown — {selected_sym}")
        st.caption(f"{sel['name']} · {sel['pair']} · last 150 bars")

        chart_df = sel["_df"].copy()
        chart_df["filt"] = sel["_channel"]["filt"]
        chart_df["hband"] = sel["_channel"]["hband"]
        chart_df["lband"] = sel["_channel"]["lband"]
        chart_df["long"] = sel["_state_series"].astype(int)
        chart_df = chart_df.tail(150).reset_index().rename(columns={"ts": "time"})

        price_layer = alt.Chart(chart_df).mark_line(color="#cccccc", strokeWidth=1.2).encode(
            x=alt.X("time:T", title=None),
            y=alt.Y("close:Q", title=selected_sym, scale=alt.Scale(zero=False)),
        )
        filt_layer = alt.Chart(chart_df).mark_line(color="#0aff68", strokeWidth=2).encode(
            x="time:T", y="filt:Q",
        )
        hband_layer = alt.Chart(chart_df).mark_line(color="#0aff68", strokeWidth=1, opacity=0.6).encode(
            x="time:T", y="hband:Q",
        )
        lband_layer = alt.Chart(chart_df).mark_line(color="#ff0a5a", strokeWidth=1, opacity=0.6).encode(
            x="time:T", y="lband:Q",
        )
        long_layer = alt.Chart(chart_df[chart_df["long"] == 1]).mark_point(
            color="#0aff68", filled=True, size=20, opacity=0.5,
        ).encode(x="time:T", y="close:Q")

        chart = (price_layer + filt_layer + hband_layer + lband_layer + long_layer).properties(height=460)
        st.altair_chart(chart, use_container_width=True)

        mc1, mc2, mc3, mc4 = st.columns(4)
        mc1.metric("State", sel["state"])
        mc2.metric("Bars in state", sel["bars_in_state"])
        mc3.metric("Stoch K", f"{sel['stoch_k']:.1f}" if sel["stoch_k"] is not None else "—")
        mc4.metric("Close vs HBand", f"{sel['close_vs_hband_pct']:+.2f}%")

    if skipped_rows:
        with st.expander(f"Skipped ({len(skipped_rows)})"):
            st.dataframe(pd.DataFrame(skipped_rows)[["symbol", "reason"]], use_container_width=True)


# --- Page header + tabs --------------------------------------------------------

st.title("Trend Radar")
st.caption(
    "GaussianChannel Strategy v3.1 (Donovan Wall filter + Stoch RSI confluence) "
    "across crypto / stocks / metals / commodities"
)

gc_params = GCParams(
    poles=poles, period=period, multiplier=multiplier,
    reduced_lag=reduced_lag, fast_response=fast_response,
)

tabs = st.tabs([ac.label for ac in ASSET_CLASSES])
for tab, ac in zip(tabs, ASSET_CLASSES):
    with tab:
        render_radar(ac, gc_params)
