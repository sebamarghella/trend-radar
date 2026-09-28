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


def _headline(stats: dict, *, label_suffix: str = "") -> None:
    a1, a2, a3, a4, a5, a6 = st.columns(6)
    a1.metric("Total return", _fmt(stats.get("total_return_pct"), "+.1f", "%"))
    a2.metric("Max drawdown", _fmt(stats.get("max_dd_pct"), ".1f", "%"))
    a3.metric("Return / MDD", _fmt(stats.get("return_over_mdd")),
              help="Total return ÷ |max drawdown| over the window. >1 means the gain outweighs the worst dip.")
    a4.metric("Profit factor", _fmt(stats.get("profit_factor")),
              help="Sum of winning trades ÷ sum of losing trades (closed trades, after commission). >1 = net profitable.")
    a5.metric("Win rate", _fmt(stats.get("win_rate"), ".1f", "%"))
    a6.metric("CAGR", _fmt(stats.get("cagr_pct"), "+.1f", "%"))
    b1, b2, b3, b4, b5, b6 = st.columns(6)
    b1.metric("Closed trades", _fmt(stats.get("closed_trades"), "d"))
    b2.metric("Open now", _fmt(stats.get("open_trades"), "d"))
    b3.metric("Calmar", _fmt(stats.get("calmar")), help="CAGR ÷ |max drawdown|.")
    b4.metric("Expectancy", _fmt(stats.get("expectancy_pct"), "+.2f", "%"), help="Mean net return per closed trade.")
    b5.metric("Avg win / loss",
              f"{_fmt(stats.get('avg_win_pct'), '+.1f', '%')} / {_fmt(stats.get('avg_loss_pct'), '+.1f', '%')}")
    b6.metric("Invested", _fmt(stats.get("avg_open"), ".1f"),
              delta=f"{_fmt(stats.get('exposure_pct'), '.0f', '%')} of days", delta_color="off",
              help="Average number of open positions (and share of days with at least one).")


def _charts(daily: pd.DataFrame, palette: dict, *, strategy_marks: bool = False) -> None:
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
    long["series"] = long["series"].map({"equity_pct": "Strategy", "hodl_pct": "HODL (equal-weight universe)"})
    hodl_color = "#9CA3AF" if palette.get("MODE") == "dark" else "#4B5563"   # dark grey, legible on both themes
    eq = (
        alt.Chart(long).mark_line(strokeWidth=1.8)
        .encode(
            x=x,
            y=alt.Y("pct:Q", axis=alt.Axis(title="Return %", gridColor=palette["BORDER"], **axis_kw)),
            color=alt.Color("series:N", legend=alt.Legend(title=None, orient="top", labelColor=palette["FG_MUTED"]),
                            scale=alt.Scale(domain=["Strategy", "HODL (equal-weight universe)"],
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
            f"**HODL** (same universe, equal-weight, no signals): "
            f"{(daily['hodl_equity'].iloc[-1] - 1) * 100:+.1f}% return · "
            f"{(daily['hodl_equity'] / daily['hodl_equity'].cummax() - 1).min() * 100:.1f}% max drawdown — "
            f"vs strategy {(daily['equity'].iloc[-1] - 1) * 100:+.1f}% · {daily['drawdown'].min():.1f}%."
        )
    st.altair_chart(alt.vconcat(eq, dd).properties(background=palette["BG_CARD"]).configure_view(stroke=None), use_container_width=True)


def _year_table(daily: pd.DataFrame, trades: pd.DataFrame | None) -> None:
    yt = perf.by_year(daily, trades)
    if yt.empty:
        return
    st.dataframe(
        yt, hide_index=True, use_container_width=True,
        column_config={
            "Year": st.column_config.NumberColumn(format="%d"),
            "Return %": st.column_config.NumberColumn(format="%+.1f"),
            "MDD %": st.column_config.NumberColumn(format="%.1f"),
            "Return / MDD": st.column_config.NumberColumn(format="%.2f"),
            "Win %": st.column_config.NumberColumn(format="%.0f"),
            "PF": st.column_config.NumberColumn(format="%.2f"),
        },
    )


def _backtest_tab(signals: list[dict], palette: dict) -> None:
    c1, c2, c3 = st.columns([1, 1, 3])
    slots = c1.number_input(
        "Max positions", min_value=0, max_value=200, value=perf.DEFAULT_SLOTS, step=1, key="bp_slots",
        help="Hard cap: at most this many positions at once, each 1/N of equity (idle slots = cash). New "
             "signals are skipped while full; ties go to the largest market cap. "
             "0 = no cap, equal weight across everything open.",
    )
    window = c2.selectbox("Window", WINDOWS, key="bp_window")

    entries = [(s["symbol"], s["_df"], s["_state_series"]) for s in signals]
    ranks = {s["symbol"]: s["rank"] for s in signals if s.get("rank") is not None}
    daily_all, info = perf.basket_daily(entries, slots=int(slots), ranks=ranks)
    if daily_all.empty:
        st.info("No strategy history to aggregate yet.")
        return
    start = _window_start(daily_all, window)
    daily = perf.rebase(daily_all, start)

    trades = perf.trades_frame([(s["symbol"], s["_trades"], s["last_close"], s["_df"]["close"]) for s in signals])
    if info.get("taken") is not None and not trades.empty:
        taken = {(sym, pd.Timestamp(ts).normalize()) for sym, ts in info["taken"]}
        keys = list(zip(trades["symbol"], pd.to_datetime(trades["entry"], utc=True).dt.normalize()))
        trades = trades[[k in taken for k in keys]]
    if start is not None and not trades.empty:
        exit_ts = pd.to_datetime(trades["exit"], utc=True)
        trades = trades[(~trades["closed"]) | (exit_ts >= start)]
    stats = perf.basket_stats(daily, trades)

    _headline(stats)
    _charts(daily, palette)
    st.caption("By calendar year")
    _year_table(daily, trades)

    notes = [
        f"Period: **{daily.index[0]:%d %b %Y} → {daily.index[-1]:%d %b %Y}** "
        "(the strategy only enters from 1 Jan 2018).",
        f"Equal-weight book, **{'no cap, equal weight across all open' if slots == 0 else f'hard cap of {int(slots)} position(s), largest market cap first'}**, 0.1% commission per side, "
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
    _headline(stats)
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
            hide_index=True, use_container_width=True,
            column_config={"Return %": st.column_config.NumberColumn(format="%+.2f")},
        )


def render_basket_performance(signals: list[dict], key: str, palette: dict) -> None:
    """Expander with both views. `signals` are compute_signal() rows (need _df,
    _state_series, _trades, last_close)."""
    with st.expander("📈 Basket performance", expanded=False):
        st.caption(
            "The whole tab treated as one portfolio. **Backtest** replays the strategy over history; "
            "**Live** is the forward record kept by the daily cron, which can't be rewritten by later tweaks."
        )
        bt, live = st.tabs(["Backtest (history)", "Live (forward log)"])
        with bt:
            _backtest_tab(signals, palette)
        with live:
            _live_tab(signals, key, palette)
