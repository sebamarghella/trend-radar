"""Streamlit panel: basket performance (historical backtest + live forward log)."""

from __future__ import annotations

import altair as alt
import pandas as pd
import streamlit as st

import live_log
import performance as perf

WINDOWS = ["All history", "3 years", "2 years", "1 year", "YTD"]


def _fmt(v: float | None, spec: str = ".2f", suffix: str = "") -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    if v == float("inf"):
        return "∞"
    return f"{v:{spec}}{suffix}"


def _window_start(daily: pd.DataFrame, window: str) -> pd.Timestamp | None:
    if window == "All history" or daily.empty:
        return None
    end = daily.index[-1]
    if window == "YTD":
        return pd.Timestamp(year=end.year, month=1, day=1, tz=end.tz)
    years = int(window.split()[0])
    return end - pd.DateOffset(years=years)


def _headline(stats: dict, palette: dict) -> None:
    """All headline numbers in one row (scrolls sideways on narrow screens)."""
    import html
    dd = stats.get("max_dd_pct")
    aw, al = stats.get("avg_win_pct"), stats.get("avg_loss_pct")
    items = [
        ("Total return", _fmt(stats.get("total_return_pct"), "+.1f", "%"), "", palette["ACCENT"] if (stats.get("total_return_pct") or 0) >= 0 else palette["BEARISH"]),
        ("Max drawdown", _fmt(dd, ".1f", "%"), "Deepest peak-to-trough fall of the equity curve.", palette["BEARISH"]),
        ("Return / MDD", _fmt(stats.get("return_over_mdd")), "Total return ÷ |max drawdown| over the window. >1 means the gain outweighs the worst dip.", None),
        ("Profit factor", _fmt(stats.get("profit_factor")), "Sum of winning trades ÷ sum of losing trades (closed trades, after commission). >1 = net profitable.", None),
        ("Win rate", _fmt(stats.get("win_rate"), ".1f", "%"), "Share of closed trades with a positive net return.", None),
        ("CAGR", _fmt(stats.get("cagr_pct"), "+.1f", "%"), "Annualised return. Blank under ~3 months of data.", None),
        ("Closed trades", _fmt(stats.get("closed_trades"), "d"), "", None),
        ("Open now", _fmt(stats.get("open_trades"), "d"), "Positions currently held.", None),
        ("Calmar", _fmt(stats.get("calmar")), "CAGR ÷ |max drawdown|.", None),
        ("Expectancy", _fmt(stats.get("expectancy_pct"), "+.2f", "%"), "Mean net return per closed trade.", None),
        ("Avg win / loss", f"{_fmt(aw, '+.1f')} / {_fmt(al, '.1f')}%", "Average winning and losing trade, net of commission.", None),
        ("Invested", _fmt(stats.get("avg_open"), ".1f"),
         f"Average number of open positions; at least one open on {_fmt(stats.get('exposure_pct'), '.0f')}% of days.", None),
    ]
    cells = "".join(
        f'<div class="bp-cell{" bp-wide" if label == "Avg win / loss" else ""}" title="{html.escape(tip)}"><div class="bp-l">{html.escape(label)}</div>'
        f'<div class="bp-v{" bp-sm" if label == "Avg win / loss" else ""}"'
        f'{f" style=color:{color}" if color else ""}>{html.escape(value)}</div></div>'
        for label, value, tip, color in items
    )
    st.markdown(
        f"""<style>
.bp-strip{{display:flex;gap:4px;overflow-x:auto;padding:2px 0 6px 0}}
.bp-cell{{flex:1 1 0;min-width:92px;padding:4px 6px 2px 0;cursor:default}}
.bp-cell.bp-wide{{flex:1.5 1 0;min-width:128px}}
.bp-l{{font-size:10.5px;text-transform:uppercase;letter-spacing:.05em;color:{palette["FG_MUTED"]};white-space:nowrap}}
.bp-v{{font-family:{palette["FONT_MONO"]};font-variant-numeric:tabular-nums;font-size:19px;font-weight:500;color:{palette["FG_PRIMARY"]};white-space:nowrap}}
.bp-v.bp-sm{{font-size:14px;line-height:27px}}
</style><div class="bp-strip">{cells}</div>""",
        unsafe_allow_html=True,
    )


def _charts(
    daily: pd.DataFrame,
    palette: dict,
    *,
    benchmark_label: str = "HODL (equal-weight universe)",
    benchmark_caption: str = "HODL (same universe, equal-weight, no signals)",
) -> None:
    if daily.empty:
        return
    frame = daily.reset_index(names="date")
    frame["equity_pct"] = (frame["equity"] - 1.0) * 100.0
    axis_kw = dict(labelColor=palette["FG_MUTED"], tickColor=palette["BORDER"],
                   domainColor=palette["BORDER"], titleColor=palette["FG_MUTED"])
    x = alt.X("date:T", axis=alt.Axis(grid=False, title=None, format="%b %Y", tickCount=8, labelOverlap=True, **axis_kw))
    has_hodl = "hodl_equity" in frame
    frame["hodl_pct"] = (frame["hodl_equity"] - 1.0) * 100.0 if has_hodl else None
    long = frame.melt(id_vars=["date", "n_open"], value_vars=["equity_pct"] + (["hodl_pct"] if has_hodl else []),
                      var_name="series", value_name="pct")
    long["series"] = long["series"].map({"equity_pct": "Strategy", "hodl_pct": benchmark_label})
    hodl_color = "#9CA3AF" if palette.get("MODE") == "dark" else "#4B5563"   # dark grey, legible on both themes
    eq = (
        alt.Chart(long).mark_line(strokeWidth=1.8)
        .encode(
            x=x,
            y=alt.Y("pct:Q", axis=alt.Axis(title="Return %", gridColor=palette["BORDER"], **axis_kw)),
            color=alt.Color("series:N", legend=alt.Legend(title=None, orient="top", labelColor=palette["FG_MUTED"]),
                            scale=alt.Scale(domain=["Strategy", benchmark_label],
                                            range=[palette["ACCENT"], hodl_color])),
            tooltip=[alt.Tooltip("date:T"), alt.Tooltip("series:N", title="Series"),
                     alt.Tooltip("pct:Q", title="Return %", format="+.1f")],
        )
        .properties(height=230)
    )
    dd = (
        alt.Chart(frame).mark_area(color=palette["BEARISH"], opacity=0.45, line={"color": palette["BEARISH"]})
        .encode(x=x, y=alt.Y("drawdown:Q", axis=alt.Axis(title="Drawdown %", gridColor=palette["BORDER"], **axis_kw)),
                tooltip=[alt.Tooltip("date:T"), alt.Tooltip("drawdown:Q", title="DD %", format=".1f")])
        .properties(height=120)
    )
    if "hodl_equity" in daily and len(daily) > 1:
        st.caption(
            f"**{benchmark_caption}**: "
            f"{(daily['hodl_equity'].iloc[-1] - 1) * 100:+.1f}% return · "
            f"{(daily['hodl_equity'] / daily['hodl_equity'].cummax() - 1).min() * 100:.1f}% max drawdown — "
            f"vs strategy {(daily['equity'].iloc[-1] - 1) * 100:+.1f}% · {daily['drawdown'].min():.1f}%."
        )
    st.altair_chart(alt.vconcat(eq, dd).properties(background=palette["BG_CARD"]).configure_view(stroke=None), width="stretch")


def _year_table(daily: pd.DataFrame, trades: pd.DataFrame | None) -> None:
    yt = perf.by_year(daily, trades)
    if yt.empty:
        return
    st.dataframe(
        yt, hide_index=True, width="stretch",
        column_config={
            "Year": st.column_config.NumberColumn(format="%d"),
            "Return %": st.column_config.NumberColumn(format="%+.1f"),
            "MDD %": st.column_config.NumberColumn(format="%.1f"),
            "Return / MDD": st.column_config.NumberColumn(format="%.2f"),
            "Win %": st.column_config.NumberColumn(format="%.0f"),
            "PF": st.column_config.NumberColumn(format="%.2f"),
        },
    )


def _fingerprint(signals: list[dict]) -> int:
    """Cheap identity of the data + strategy output, so results are recomputed
    only when a bar, a price or a strategy state actually changes."""
    return hash(tuple(
        (s["symbol"], str(s["_df"].index[-1]), len(s["_df"]), round(float(s["last_close"]), 6),
         int(s["_state_series"].sum()), len(s["_trades"]))
        for s in signals
    ))


@st.cache_data(show_spinner=False, max_entries=12)
def _basket_compute(fp: int, slots: int, position_fraction: float, _signals: list[dict]):
    """Backtest matrices + flattened trades. `fp` is the cache key; `_signals`
    is deliberately not hashed."""
    entries = [(s["symbol"], s["_df"], s["_state_series"]) for s in _signals]
    ranks = {s["symbol"]: s["rank"] for s in _signals if s.get("rank") is not None}
    daily, info = perf.basket_daily(entries, slots=slots, ranks=ranks,
                                    position_fraction=position_fraction)
    trades = perf.trades_frame(
        [(s["symbol"], s["_trades"], s["last_close"], s["_df"]["close"]) for s in _signals]
    )
    return daily, info, trades


def _backtest_tab(signals: list[dict], palette: dict, position_fraction: float) -> None:
    c1, c2, c3 = st.columns([1, 1, 3])
    slots = c1.number_input(
        "Max positions", min_value=0, max_value=200, value=perf.DEFAULT_SLOTS, step=1, key="bp_slots",
        help="Hard cap: at most this many positions at once, each an equal share of the active allocation (idle slots = cash). New "
             "signals are skipped while full; ties go to the largest market cap. "
             "0 = no cap, equal weight across everything open.",
    )
    window = c2.selectbox("Window", WINDOWS, key="bp_window")

    daily_all, info, trades = _basket_compute(_fingerprint(signals), int(slots),
                                               position_fraction, signals)
    if daily_all.empty:
        st.info("No strategy history to aggregate yet.")
        return
    start = _window_start(daily_all, window)
    daily = perf.rebase(daily_all, start)

    if info.get("taken") is not None and not trades.empty:
        taken = {(sym, pd.Timestamp(ts).normalize()) for sym, ts in info["taken"]}
        keys = list(zip(trades["symbol"], pd.to_datetime(trades["entry"], utc=True).dt.normalize()))
        trades = trades[[k in taken for k in keys]]
    if start is not None and not trades.empty:
        exit_ts = pd.to_datetime(trades["exit"], utc=True)
        trades = trades[(~trades["closed"]) | (exit_ts >= start)]
    stats = perf.basket_stats(daily, trades)

    _headline(stats, palette)
    _charts(daily, palette)
    st.caption("By calendar year")
    _year_table(daily, trades)

    notes = [
        f"Period: **{daily.index[0]:%d %b %Y} → {daily.index[-1]:%d %b %Y}** "
        "(the strategy only enters from 1 Jan 2018).",
        f"Daily-rebalanced equal-weight book, **{'no cap, equal weight across all open' if slots == 0 else f'hard cap of {int(slots)} position(s), largest market cap first'}**, "
        f"{position_fraction:.0%} of equity allocated when all slots are filled, 0.1% commission per side, "
        "entries/exits at the signal-bar close. Window stats count trades that closed inside the window.",
        f"**Survivorship bias:** the universe is *today's* top {info.get('universe', '?')} — names that later fell out "
        "or delisted are absent, so history reads better than a live book would have.",
    ]
    if info.get("splits_adjusted"):
        notes.append(
            f"Prices are raw (unadjusted), so **{info['splits_adjusted']} split-like one-day moves** "
            "(price landing on an exact split ratio) were neutralised in both lines and in trade returns."
        )
    if info.get("suspect_bars"):
        notes.append(
            f"⚠ {info['suspect_bars']} held-position day(s) still moved more than {perf.SUSPECT_MOVE:.0%} "
            "after that adjustment — real crashes, spin-offs or bad ticks."
        )
    for n in notes:
        st.caption(n)


def _live_tab(signals: list[dict], key: str, palette: dict) -> None:
    daily = live_log.live_daily_frame(key)
    meta = live_log.load_equity(key).get("meta", {})
    if len(daily) < 2:
        st.info(
            "The forward log starts with the next daily cron run (00:05 UTC) and grows one row per completed "
            "session from then on. Nothing has been recorded yet." if daily.empty else
            f"Tracking began {meta.get('start')} — need at least one completed session after that to chart."
        )
        return
    last_close = {s["symbol"]: s["last_close"] for s in signals}
    trades = live_log.live_trades_frame(key, last_close)
    stats = perf.basket_stats(daily, trades)

    st.caption(
        f"Tracking since **{meta.get('start')}** · {len(daily)} sessions · "
        f"{meta.get('slots', perf.DEFAULT_SLOTS)} slots · positions open on day one are logged as *carried* "
        "(entered at that day's close)."
    )
    strategies_used = sorted(set(daily["strategy"].dropna())) if "strategy" in daily else []
    if len(strategies_used) > 1:
        st.warning("The strategy changed during tracking: " + " → ".join(strategies_used) +
                   ". Numbers below chain across both.")
    if "position_fraction" in daily:
        fractions = sorted(set(daily["position_fraction"].fillna(1.0)))
        if len(fractions) > 1:
            st.warning("Position sizing changed during tracking: " +
                       " → ".join(f"{fraction:.0%}" for fraction in fractions) +
                       ". The forward curve chains both sizing settings.")
    _headline(stats, palette)
    _charts(daily, palette)
    st.caption("By calendar year")
    _year_table(daily, trades)

    with st.expander(f"Trade log ({len(trades)})"):
        show = trades.sort_values("entry", ascending=False).copy()
        show["Status"] = show["closed"].map({True: "Closed", False: "Open (MTM)"})
        show["Return %"] = show["ret"] * 100.0
        show["entry"] = show["entry"].dt.strftime("%Y-%m-%d")
        show["exit"] = show["exit"].dt.strftime("%Y-%m-%d").fillna("—")
        st.dataframe(
            show[["symbol", "entry", "exit", "Status", "Return %", "carried"]].rename(
                columns={"symbol": "Symbol", "entry": "Entry", "exit": "Exit", "carried": "Carried"}),
            hide_index=True, width="stretch",
            column_config={"Return %": st.column_config.NumberColumn(format="%+.2f")},
        )


def render_symbol_performance(
    signal: dict, asset_key: str, strategy_name: str, strategy_logic_key: str,
    timeframe: str, palette: dict,
) -> None:
    """Backtest the selected symbol on the active strategy and bar timeframe."""
    symbol = signal["symbol"]
    with st.expander(f"📈 {symbol} performance", expanded=True, key=f"symbol_performance_{asset_key}"):
        st.caption(f"{signal['name']} · {strategy_name} · {timeframe} · historical backtest")
        window = st.selectbox("Window", WINDOWS, key=f"symbol_perf_window_{asset_key}")
        position_fraction = (perf.GC_STOCKS_POSITION_FRACTION
                             if strategy_logic_key == "gaussian_channel_stocks_v1" else 1.0)
        daily_all, _ = perf.basket_daily(
            [(symbol, signal["_df"], signal["_state_series"])], slots=0,
            position_fraction=position_fraction,
        )
        if daily_all.empty:
            st.info("No strategy history is available for this symbol yet.")
            return
        trades = perf.trades_frame([
            (symbol, signal["_trades"], signal["last_close"], signal["_df"]["close"]),
        ])
        start = _window_start(daily_all, window)
        daily = perf.rebase(daily_all, start)
        if start is not None and not trades.empty:
            exit_ts = pd.to_datetime(trades["exit"], utc=True)
            trades = trades[(~trades["closed"]) | (exit_ts >= start)]
        stats = perf.basket_stats(daily, trades)

        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("Strategy return", _fmt(stats.get("total_return_pct"), "+.1f", "%"))
        m2.metric("Max drawdown", _fmt(stats.get("max_dd_pct"), ".1f", "%"))
        m3.metric("Closed trades", stats.get("closed_trades", 0))
        m4.metric("Win rate", _fmt(stats.get("win_rate"), ".1f", "%"))
        m5.metric("Time invested", _fmt(stats.get("exposure_pct"), ".0f", "%"))

        _charts(
            daily, palette,
            benchmark_label=f"{symbol} buy & hold",
            benchmark_caption=f"Buy & hold {symbol} from first entry close (no signals, no commission)",
        )
        st.caption("By calendar year")
        _year_table(daily, trades)
        st.caption(
            f"Period: **{daily.index[0]:%d %b %Y} → {daily.index[-1]:%d %b %Y}** · "
            "strategy holds this symbol after a LONG close and otherwise stays in cash; "
            f"{position_fraction:.0%} position sizing with the rest in cash; 0.1% commission per side."
        )
        if strategy_logic_key == "gaussian_channel_stocks_v1":
            st.caption(
                "The supplied Pine strategy sizes each entry at 95% of then-current equity, "
                "fills at the next bar's open and uses 3 ticks of slippage. This dashboard "
                "rebalances a 95% allocation at signal-bar closes, so its return is "
                "not an exact TradingView Strategy Tester result."
            )


def render_basket_performance(signals: list[dict], key: str, palette: dict,
                              strategy_logic_key: str) -> None:
    """Expander with both views. `signals` are compute_signal() rows (need _df,
    _state_series, _trades, last_close)."""
    with st.expander("📈 Overall account performance", expanded=True):
        st.caption(
            "The whole tab treated as one portfolio. **Backtest** replays the strategy over history; "
            "**Live** is the forward record kept by the daily cron, which can't be rewritten by later tweaks."
        )
        bt, live = st.tabs(["Backtest (history)", "Live (forward log)"])
        with bt:
            position_fraction = (perf.GC_STOCKS_POSITION_FRACTION
                                 if strategy_logic_key == "gaussian_channel_stocks_v1" else 1.0)
            _backtest_tab(signals, palette, position_fraction)
        with live:
            _live_tab(signals, key, palette)
