"""Trend Radar — GaussianChannel Strategy v3.1 across the top 100 crypto.

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
from binance_client import BinanceError, fetch_with_retry, resolve_symbol, tradable_symbols
from coins import candidate_symbols, tradable_universe
from gaussian_channel import (
    BAR_COLORS,
    GCParams,
    compute_stats,
    gaussian_channel,
    replay_strategy,
    stoch_rsi_k,
)


st.set_page_config(
    page_title="Trend Radar — GaussianChannel v3.1",
    layout="wide",
    initial_sidebar_state="expanded",
)


# --- Sidebar -------------------------------------------------------------------

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

st.sidebar.header("Universe")
interval_label = st.sidebar.selectbox(
    "Timeframe",
    options=[("1 day", 1440), ("4 hour", 240), ("1 hour", 60)],
    format_func=lambda x: x[0],
    index=0,
)
interval_minutes = interval_label[1]
sort_by = st.sidebar.selectbox(
    "Sort by",
    ["Rank", "State (long first)", "Bars in state", "Stoch K",
     "Close vs HBand %", "Net %", "Win %", "Trades"],
)
col_a, col_b = st.sidebar.columns(2)
soft_refresh = col_a.button("Refresh", type="primary", help="Refetch coins whose bar has closed")
hard_refresh = col_b.button("Force refetch", help="Ignore on-disk cache entirely")

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
    """Read [telegram] bot_token and chat_id from secrets.toml if present."""
    try:
        tg = st.secrets.get("telegram", {})
        return str(tg.get("bot_token", "")), str(tg.get("chat_id", ""))
    except Exception:
        # StreamlitSecretNotFoundError when no secrets.toml exists. Treat as empty.
        return "", ""


_secret_token, _secret_chat = _load_telegram_secrets()
alerts_enabled = st.sidebar.checkbox(
    "Fire on state flips", value=bool(_secret_token and _secret_chat),
    help="Sends a Telegram message when any coin flips FLAT↔LONG.",
)
bot_token = st.sidebar.text_input("Bot token", value=_secret_token, type="password")
chat_id = st.sidebar.text_input("Chat ID", value=_secret_chat)
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


@st.cache_data(ttl=15 * 60, show_spinner=False)
def cached_symbols() -> set:
    return tradable_symbols()


def fetch_one(base: str, available: set, interval: int, force_refresh: bool = False) -> dict:
    cands = candidate_symbols(base)
    resolved = resolve_symbol(cands, available)
    if resolved is None:
        return {"symbol": base, "ok": False, "reason": "no Binance pair"}
    cached_df = None
    if not force_refresh:
        cached_df = ohlc_cache.load(resolved, interval)
        if cached_df is not None and ohlc_cache.is_fresh(cached_df, interval):
            return {"symbol": base, "ok": True, "pair": resolved, "df": cached_df, "source": "cache"}
    try:
        df = fetch_with_retry(resolved, interval_minutes=interval)
    except BinanceError as e:
        if cached_df is not None:
            return {"symbol": base, "ok": True, "pair": resolved, "df": cached_df, "source": "stale"}
        return {"symbol": base, "ok": False, "reason": str(e)}
    if len(df) < 60:
        return {"symbol": base, "ok": False, "reason": "insufficient history"}
    ohlc_cache.save(resolved, interval, df)
    return {"symbol": base, "ok": True, "pair": resolved, "df": df, "source": "fresh"}


@st.cache_data(ttl=15 * 60, show_spinner=False)
def load_universe_data(interval: int, bust: int, force_refresh: bool) -> tuple[list[dict], list[dict]]:
    """Returns (ok_rows, skipped_rows). `bust` is a manual cache key.

    When `force_refresh=True`, bypass the on-disk OHLC cache and fetch every coin
    from Binance. Otherwise, use cached candles that are still within their bar.
    """
    available = cached_symbols()
    universe = tradable_universe()
    results: list[dict] = []
    # Binance public REST tolerates 1200 weight/min; 20 workers stays well under.
    with ThreadPoolExecutor(max_workers=20) as ex:
        futures = {
            ex.submit(fetch_one, c["symbol"], available, interval, force_refresh): c
            for c in universe
        }
        progress = st.progress(0.0, text="Loading candles…")
        done = 0
        cache_hits = 0
        for fut in as_completed(futures):
            meta = futures[fut]
            res = fut.result()
            res["rank"] = meta["rank"]
            res["name"] = meta["name"]
            if res.get("source") == "cache":
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


# --- Header --------------------------------------------------------------------

st.title("Trend Radar")
st.caption(
    "GaussianChannel Strategy v3.1 (Donovan Wall filter + Stoch RSI confluence) — top 100 crypto by mcap"
)

if "bust" not in st.session_state:
    st.session_state.bust = 0
if soft_refresh or hard_refresh:
    st.session_state.bust += 1
    st.cache_data.clear()
force_refetch = hard_refresh

gc_params = GCParams(
    poles=poles,
    period=period,
    multiplier=multiplier,
    reduced_lag=reduced_lag,
    fast_response=fast_response,
)


def _bar_cycle(interval_minutes_: int) -> tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp]:
    """Returns (now, current bar open, next bar open) in UTC."""
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


_now, _bar_open, _bar_next = _bar_cycle(interval_minutes)
_bar_age = _now - _bar_open
_bar_remaining = _bar_next - _now
st.caption(
    f"⏱ Current {interval_label[0]} bar: **{_bar_open.strftime('%H:%M UTC')} → "
    f"{_bar_next.strftime('%H:%M UTC')}**  ·  "
    f"{_fmt_hm(_bar_age)} in, **{_fmt_hm(_bar_remaining)} to next rollover**  ·  "
    f"caches refresh at the rollover."
)

with st.spinner("Loading Binance symbols and klines…"):
    ok_rows, skipped_rows = load_universe_data(
        interval_minutes, st.session_state.bust, force_refetch
    )

if not ok_rows:
    st.error("No coins resolved to Binance symbols. Check your network and try Refresh.")
    if skipped_rows:
        st.dataframe(pd.DataFrame(skipped_rows)[["symbol", "reason"]])
    st.stop()

signals = [
    compute_signal(r, gc_params, rsi_length, stoch_length, smooth_k, lookback_days)
    for r in ok_rows
]

# --- Alert detection & firing --------------------------------------------------

prev_alert_state = alerts.load_state()
was_first_run = not prev_alert_state
flips, new_alert_state = alerts.detect_flips(signals, interval_minutes, prev_alert_state)
alerts.save_state(new_alert_state)

if was_first_run:
    st.info(
        f"Seeded alert state for {len(new_alert_state)} coins on this timeframe. "
        "Future flips will be diffed against this baseline."
    )
elif flips and alerts_enabled and bot_token and chat_id:
    sent, errs = alerts.fire_alerts(flips, bot_token, chat_id)
    if sent:
        st.toast(f"📨 Sent {sent} Telegram alert(s)", icon="📨")
    for e in errs:
        st.warning(f"Alert failed for {e}")
elif flips:
    flip_summary = ", ".join(
        f"{f.symbol} {'↗' if f.direction == 'ENTRY' else '↘'}" for f in flips
    )
    st.info(f"State flips detected (alerts disabled): {flip_summary}")


# --- Headline strip ------------------------------------------------------------

long_count = sum(1 for s in signals if s["state"] == "LONG")
flat_count = len(signals) - long_count
green_filter = sum(1 for s in signals if s["filter_up"])
covered = len(signals)
total_universe = len(tradable_universe())
cache_hits = sum(1 for r in ok_rows if r.get("source") == "cache")
stale_hits = sum(1 for r in ok_rows if r.get("source") == "stale")

c1, c2, c3, c4 = st.columns(4)
c1.metric("Coins covered", f"{covered} / {total_universe}")
c2.metric("In long", long_count, delta=f"{long_count / covered * 100:.0f}%")
c3.metric("Filter rising", green_filter, delta=f"{green_filter / covered * 100:.0f}%")
c4.metric("Timeframe", interval_label[0])

cache_msg = f"{cache_hits}/{covered} from cache"
if stale_hits:
    cache_msg += f" · {stale_hits} stale (Binance errored)"
st.caption(cache_msg)


# --- Radar grid ----------------------------------------------------------------

df = pd.DataFrame(signals)
df_display = df.drop(columns=["_df", "_channel", "_state_series"]).copy()

sort_map = {
    "Rank": ("rank", True),
    "State (long first)": ("state", False),  # LONG < FLAT alphabetically
    "Bars in state": ("bars_in_state", False),
    "Stoch K": ("stoch_k", False),
    "Close vs HBand %": ("close_vs_hband_pct", False),
    "Net %": ("net_pct", False),
    "Win %": ("win_pct", False),
    "Trades": ("trades", False),
}
sort_col, ascending = sort_map[sort_by]
df_display = df_display.sort_values(sort_col, ascending=ascending, na_position="last").reset_index(drop=True)
df_display["tv"] = df_display["pair"]  # value used by the TV link renderer


# AgGrid cell-style JS — color the Pos / Bar / Filter↑ / vs HBand cells.
_CELLSTYLE_STATE = JsCode("""
function(p) {
    if (p.value === 'LONG') {
        return { backgroundColor: '', color: 'black', fontWeight: 700 };
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
    if (p.value === true) return { color: '', fontWeight: 600 };
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

_TV_LINK_RENDERER = JsCode("""
function(p) {
    if (!p.value) return '';
    const url = 'https://www.tradingview.com/symbols/' + p.value + '/?exchange=BINANCE';
    return '<a href="' + url + '" target="_blank" rel="noopener noreferrer" '
        + 'style="color:#0aff68;text-decoration:none;font-weight:600;" '
        + 'onclick="event.stopPropagation();">TV ↗</a>';
}
""")


def build_grid_options(df: pd.DataFrame) -> dict:
    gb = GridOptionsBuilder.from_dataframe(df)
    gb.configure_default_column(resizable=True, sortable=True, filterable=False)
    gb.configure_selection(selection_mode="single", use_checkbox=False, suppressRowDeselection=True)
    gb.configure_column("rank", header_name="#", maxWidth=60, type=["numericColumn"])
    gb.configure_column("symbol", header_name="Sym", maxWidth=80, pinned="left")
    gb.configure_column("name", header_name="Name", minWidth=120)
    gb.configure_column("pair", header_name="Pair", maxWidth=110)
    gb.configure_column("state", header_name="Pos", maxWidth=70, cellStyle=_CELLSTYLE_STATE)
    gb.configure_column("bar_color", header_name="Bar", maxWidth=120, cellStyle=_CELLSTYLE_BAR)
    gb.configure_column("filter_up", header_name="F↑", maxWidth=60, cellStyle=_CELLSTYLE_FILTER)
    gb.configure_column("bars_in_state", header_name="Bars", maxWidth=70, type=["numericColumn"])
    gb.configure_column(
        "close_vs_hband_pct",
        header_name="vs HB",
        maxWidth=90,
        type=["numericColumn"],
        valueFormatter=_FMT_PCT,
        cellStyle=_CELLSTYLE_PCT,
    )
    gb.configure_column("stoch_k", header_name="StK", maxWidth=70, type=["numericColumn"], valueFormatter=_FMT_K)
    gb.configure_column("last_close", header_name="Close", type=["numericColumn"], valueFormatter=_FMT_PRICE)
    gb.configure_column(
        "trades",
        header_name="Trades",
        maxWidth=80,
        type=["numericColumn"],
        valueFormatter=_FMT_INT,
    )
    gb.configure_column(
        "net_pct",
        header_name="Net %",
        maxWidth=100,
        type=["numericColumn"],
        valueFormatter=_FMT_PCT,
        cellStyle=_CELLSTYLE_PCT,
    )
    gb.configure_column(
        "win_pct",
        header_name="Win %",
        maxWidth=85,
        type=["numericColumn"],
        valueFormatter=_FMT_WIN,
    )
    gb.configure_column(
        "tv",
        header_name="TV",
        maxWidth=70,
        sortable=False,
        filter=False,
        cellRenderer=_TV_LINK_RENDERER,
    )
    for hidden in ("last_filt", "last_hband", "last_lband"):
        gb.configure_column(hidden, hide=True)
    return gb.build()


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
        key=f"radar_grid_{sort_by}",
    )

# --- Per-coin drilldown --------------------------------------------------------

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


# --- Skipped coins -------------------------------------------------------------

if skipped_rows:
    with st.expander(f"Skipped ({len(skipped_rows)})"):
        st.dataframe(pd.DataFrame(skipped_rows)[["symbol", "reason"]], use_container_width=True)
