"""Basket-level performance: pool every symbol's strategy state into one portfolio.

Sizing model (deliberately simple and explicit): the portfolio has `slots`
equal-weight positions. Each open position gets 1/max(slots, n_open) of equity,
so the book is never levered and idle slots sit in cash. `slots=0` means fully
invested equal weight across whatever is open. Commission is charged per side
on turnover, matching `TradeRecord.net_return` (0.1%).

A position is held on day t when the strategy state at the close of day t-1 was
LONG (enter at the flip-bar close, exit at the exit-bar close — same fills as
the per-symbol tear sheet).

No Streamlit imports: the headless cron uses this module too.
"""

from __future__ import annotations

from typing import Iterable

import numpy as np
import pandas as pd

DEFAULT_SLOTS = 20
DEFAULT_COMMISSION = 0.001
# A single-day move this big while held is almost certainly a split / bad tick
# in raw (unadjusted) prices rather than a real return.
SUSPECT_MOVE = 0.35


def _utc_index(idx: pd.DatetimeIndex) -> pd.DatetimeIndex:
    idx = pd.DatetimeIndex(idx)
    return idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")


def _daily_frame(entries: Iterable[tuple[str, pd.DataFrame, pd.Series]]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(closes, states) matrices, dates x symbols, on a UTC-midnight index."""
    closes: dict[str, pd.Series] = {}
    states: dict[str, pd.Series] = {}
    for sym, df, state in entries:
        c = df["close"].astype(float).copy()
        c.index = _utc_index(c.index).normalize()
        c = c[~c.index.duplicated(keep="last")]
        s = pd.Series(state.to_numpy(), index=_utc_index(state.index).normalize())
        s = s[~s.index.duplicated(keep="last")]
        closes[sym] = c
        states[sym] = s
    close_df = pd.DataFrame(closes).sort_index()
    state_df = pd.DataFrame(states).reindex(close_df.index).fillna(0).astype("int8")
    return close_df, state_df


def basket_daily(
    entries: Iterable[tuple[str, pd.DataFrame, pd.Series]],
    *,
    slots: int = DEFAULT_SLOTS,
    commission: float = DEFAULT_COMMISSION,
) -> tuple[pd.DataFrame, dict]:
    """Daily portfolio returns from (symbol, ohlc_df, state_series) triples.

    Returns (daily, info). `daily` is indexed by UTC date with columns
    ret (net of commission), equity (starts at 1.0 on the first date with any
    state), n_open (positions held that day), drawdown (% below running peak).
    """
    close_df, state_df = _daily_frame(entries)
    if close_df.empty:
        return pd.DataFrame(columns=["ret", "equity", "n_open", "drawdown"]), {"suspect_bars": 0}

    rets = close_df.ffill().pct_change().fillna(0.0)
    held = state_df.shift(1).fillna(0).astype(float)      # LONG at prior close → held today
    n_open = held.sum(axis=1)
    denom = np.maximum(float(slots) if slots > 0 else 1.0, n_open).replace(0, 1.0)
    weights = held.div(denom, axis=0)
    gross = (weights * rets).sum(axis=1)
    turnover = weights.diff().abs().sum(axis=1)
    turnover.iloc[0] = weights.iloc[0].abs().sum()
    net = gross - commission * turnover

    suspect = int(((rets.abs() > SUSPECT_MOVE) & (held > 0)).to_numpy().sum())

    # HODL benchmark: equal-weight, daily-rebalanced across every symbol that has
    # a price on both days (no signals, no commission) — the same universe held blindly.
    raw = close_df.pct_change(fill_method=None)
    hodl = raw.mean(axis=1, skipna=True).fillna(0.0)

    active = state_df.sum(axis=1) > 0
    first = active.idxmax() if active.any() else close_df.index[0]
    net = net.loc[first:]
    daily = pd.DataFrame({"ret": net, "n_open": n_open.loc[first:].astype(int),
                          "hodl_ret": hodl.loc[first:]})
    daily["equity"] = (1.0 + daily["ret"]).cumprod()
    daily["hodl_equity"] = (1.0 + daily["hodl_ret"]).cumprod()
    daily["drawdown"] = (daily["equity"] / daily["equity"].cummax() - 1.0) * 100.0
    return daily, {"suspect_bars": suspect, "universe": close_df.shape[1]}


def rebase(daily: pd.DataFrame, start: pd.Timestamp | None) -> pd.DataFrame:
    """Restart equity at 1.0 from `start` (positions already open keep earning)."""
    if start is None or daily.empty:
        return daily
    d = daily.loc[daily.index >= start].copy()
    if d.empty:
        return d
    d["equity"] = (1.0 + d["ret"]).cumprod()
    if "hodl_ret" in d:
        d["hodl_equity"] = (1.0 + d["hodl_ret"]).cumprod()
    d["drawdown"] = (d["equity"] / d["equity"].cummax() - 1.0) * 100.0
    return d


def trades_frame(
    entries: Iterable[tuple[str, list, float]],
    commission: float = DEFAULT_COMMISSION,
) -> pd.DataFrame:
    """Flatten per-symbol TradeRecords to rows. `entries` = (symbol, trades, last_close).
    Open trades carry a mark-to-market return and closed=False."""
    rows = []
    for sym, trades, last_close in entries:
        for t in trades:
            if t.entry_price in (None, 0):
                continue
            if t.closed and t.exit_ts is not None:
                ret, exit_ts, closed = t.net_return(commission), t.exit_ts, True
            else:
                ret = last_close / t.entry_price * (1.0 - commission) ** 2 - 1.0
                exit_ts, closed = None, False
            rows.append({"symbol": sym, "entry": t.entry_ts, "exit": exit_ts,
                         "entry_price": t.entry_price, "exit_price": t.exit_price,
                         "ret": ret, "closed": closed})
    cols = ["symbol", "entry", "exit", "entry_price", "exit_price", "ret", "closed"]
    return pd.DataFrame(rows, columns=cols)


def profit_factor(rets: pd.Series) -> float | None:
    gains = rets[rets > 0].sum()
    losses = -rets[rets < 0].sum()
    if losses > 0:
        return float(gains / losses)
    return float("inf") if gains > 0 else None


def basket_stats(daily: pd.DataFrame, trades: pd.DataFrame | None = None) -> dict:
    """Headline numbers. `trades` needs columns ret, closed."""
    out: dict = {"days": len(daily)}
    if daily.empty:
        return out
    total = float(daily["equity"].iloc[-1] - 1.0)
    mdd = float(daily["drawdown"].min())
    years = max((daily.index[-1] - daily.index[0]).days / 365.25, 1 / 365.25)
    # Annualising under ~3 months of data is noise, so leave CAGR/Calmar blank.
    cagr = ((1.0 + total) ** (1.0 / years) - 1.0 if total > -1 else -1.0) if years >= 0.25 else None
    out.update(
        total_return_pct=total * 100.0,
        cagr_pct=cagr * 100.0 if cagr is not None else None,
        max_dd_pct=mdd,
        return_over_mdd=(total / (abs(mdd) / 100.0)) if mdd < 0 else None,
        calmar=(cagr / (abs(mdd) / 100.0)) if (mdd < 0 and cagr is not None) else None,
        exposure_pct=float((daily["n_open"] > 0).mean() * 100.0),
        avg_open=float(daily["n_open"].mean()),
        current_dd_pct=float(daily["drawdown"].iloc[-1]),
    )
    if "hodl_equity" in daily:
        h = daily["hodl_equity"]
        out.update(hodl_return_pct=float((h.iloc[-1] - 1.0) * 100.0),
                   hodl_max_dd_pct=float((h / h.cummax() - 1.0).min() * 100.0))
    if trades is not None and not trades.empty:
        closed = trades[trades["closed"]]["ret"].astype(float)
        wins, losses = closed[closed > 0], closed[closed < 0]
        out.update(
            trades=len(trades),
            closed_trades=len(closed),
            open_trades=int((~trades["closed"]).sum()),
            win_rate=float(len(wins) / len(closed) * 100.0) if len(closed) else None,
            profit_factor=profit_factor(closed) if len(closed) else None,
            avg_win_pct=float(wins.mean() * 100.0) if len(wins) else None,
            avg_loss_pct=float(losses.mean() * 100.0) if len(losses) else None,
            expectancy_pct=float(closed.mean() * 100.0) if len(closed) else None,
        )
    return out


def by_year(daily: pd.DataFrame, trades: pd.DataFrame | None = None) -> pd.DataFrame:
    """Calendar-year breakdown: return, MDD inside the year, closed-trade stats."""
    rows = []
    for year, g in daily.groupby(daily.index.year):
        eq = (1.0 + g["ret"]).cumprod()
        row = {
            "Year": int(year),
            "Return %": (eq.iloc[-1] - 1.0) * 100.0,
            "MDD %": float((eq / eq.cummax() - 1.0).min() * 100.0),
        }
        mdd = abs(row["MDD %"])
        row["Return / MDD"] = row["Return %"] / mdd if mdd > 0 else None
        if trades is not None and not trades.empty:
            closed = trades[trades["closed"] & (pd.DatetimeIndex(trades["exit"]).year == year)]["ret"].astype(float)
            row["Closed"] = len(closed)
            row["Win %"] = float((closed > 0).mean() * 100.0) if len(closed) else None
            row["PF"] = profit_factor(closed) if len(closed) else None
        rows.append(row)
    return pd.DataFrame(rows)
