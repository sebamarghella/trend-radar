"""Basket-level performance: pool every symbol's strategy state into one portfolio.

Sizing model (deliberately simple and explicit):
- `slots=0`: no cap. Every open position gets position_fraction/n_open of
  equity; the remainder stays in cash.
- `slots=N>0`: a hard cap of N positions, each position_fraction/N of equity,
  with unfilled slots and the remaining fraction in cash.
  A new signal (flat -> long flip) is skipped when the book is full, and when
  several arrive the same day the largest market cap (lowest rank) wins. A
  skipped signal is not entered later; the next flip is the next chance.
Commission is charged per side on turnover, matching `TradeRecord.net_return`
(0.1%).

A position is held on day t when the strategy state at the close of day t-1 was
LONG (enter at the flip-bar close, exit at the exit-bar close — same fills as
the per-symbol tear sheet).

No Streamlit imports: the headless cron uses this module too.
"""

from __future__ import annotations

from typing import Iterable

import numpy as np
import pandas as pd

DEFAULT_SLOTS = 0   # 0 = no cap, equal weight across all open positions
DEFAULT_COMMISSION = 0.001
GC_STOCKS_POSITION_FRACTION = 0.95  # Pine default_qty_value=95, margin_long=100
# A single-day move this big while held is suspicious even after split
# adjustment (bad tick, spin-off, delisting artefact).
SUSPECT_MOVE = 0.35

# Prices are raw (unadjusted, to match TradingView/Signum), so every split shows
# up as a fake crash (or, for reverse splits, a fake spike). Without split data we
# detect them: a one-day move that lands within SPLIT_TOL of an exact split ratio.
# A real move that happens to land on one is mis-zeroed, costing at most one
# stock's day out of hundreds — far cheaper than leaving ~100 fake -50..-95% days
# in an 8-year equal-weight book.
SPLIT_RATIOS = (1.5, 2, 3, 4, 5, 6, 7, 8, 10, 12, 15, 20, 25, 30, 40, 50, 100)
SPLIT_TOL = 0.03
SPLIT_MIN_MOVE = 0.30


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


def split_factors(close_df: pd.DataFrame) -> pd.DataFrame:
    """Per-day multiplier that undoes a detected split (1.0 = none). Forward k:1
    (price / k) gets factor k; reverse 1:k (price x k) gets 1/k."""
    c = close_df.ffill()
    ratio = (c / c.shift(1)).to_numpy()
    fac = np.ones(ratio.shape)
    with np.errstate(invalid="ignore"):
        big = np.abs(ratio - 1.0) >= SPLIT_MIN_MOVE      # NaN compares False
    if big.any():                                        # ~0.1% of cells: only test those
        r = ratio[big]
        out = np.ones(len(r))
        for k in SPLIT_RATIOS:
            out[np.abs(r * k - 1.0) <= SPLIT_TOL] = float(k)
            out[np.abs(r / k - 1.0) <= SPLIT_TOL] = 1.0 / k
        fac[big] = out
    return pd.DataFrame(fac, index=close_df.index, columns=close_df.columns)


def _capped_holdings(state_df: pd.DataFrame, cap: int, ranks: dict | None) -> tuple[pd.DataFrame, list]:
    """Positions actually held at each close under a hard cap, plus the entries
    taken as (symbol, date). See the module docstring for the selection rule."""
    cols = list(state_df.columns)
    prio = np.array([ranks.get(c, np.inf) if ranks else i for i, c in enumerate(cols)], dtype=float)
    order = np.argsort(prio, kind="stable")
    S = state_df.to_numpy().astype(bool)
    T, N = S.shape
    held = np.zeros(N, dtype=bool)
    out = np.zeros((T, N), dtype=bool)
    taken: list[tuple[str, pd.Timestamp]] = []
    for t in range(T):
        prev = S[t - 1] if t else np.zeros(N, dtype=bool)
        held &= S[t]                                        # exits: state went FLAT
        if held.sum() < cap:
            for j in order:
                if S[t, j] and not prev[j] and not held[j]:  # fresh flip to LONG
                    held[j] = True
                    taken.append((cols[j], state_df.index[t]))
                    if held.sum() >= cap:
                        break
        out[t] = held
    return pd.DataFrame(out, index=state_df.index, columns=cols), taken


def basket_daily(
    entries: Iterable[tuple[str, pd.DataFrame, pd.Series]],
    *,
    slots: int = DEFAULT_SLOTS,
    commission: float = DEFAULT_COMMISSION,
    ranks: dict | None = None,
    position_fraction: float = 1.0,
) -> tuple[pd.DataFrame, dict]:
    """Daily portfolio returns from (symbol, ohlc_df, state_series) triples.

    Returns (daily, info). `daily` is indexed by UTC date with columns
    ret (net of commission), equity (starts at 1.0 on the first date with any
    state), n_open (positions held that day), drawdown (% below running peak).
    """
    if not 0.0 < position_fraction <= 1.0:
        raise ValueError("position_fraction must be in (0, 1]")
    close_df, state_df = _daily_frame(entries)
    if close_df.empty:
        return pd.DataFrame(columns=["ret", "equity", "n_open", "drawdown"]), {"suspect_bars": 0, "splits_adjusted": 0}

    close_ff = close_df.ffill()
    fac = split_factors(close_df)
    rets = ((close_ff / close_ff.shift(1)) * fac - 1.0).fillna(0.0)
    n_splits = int((fac != 1.0).to_numpy().sum())
    taken = None
    if slots > 0:
        book, taken = _capped_holdings(state_df, int(slots), ranks)
        held = book.shift(1).fillna(False).astype(float)   # held at prior close → earns today
        n_open = held.sum(axis=1)
        weights = held * position_fraction / float(slots)
        close_weights = book.astype(float) * position_fraction / float(slots)
    else:
        held = state_df.shift(1).fillna(0).astype(float)   # LONG at prior close → held today
        n_open = held.sum(axis=1)
        weights = held.div(n_open.replace(0, 1.0), axis=0) * position_fraction
        close_held = state_df.astype(float)
        close_weights = close_held.div(close_held.sum(axis=1).replace(0, 1.0), axis=0) * position_fraction
    gross = (weights * rets).sum(axis=1)
    # Fill at the signal bar's close: charge its entry/exit fee now, while the
    # price return still belongs to positions held at the previous close.
    turnover = close_weights.diff().abs().sum(axis=1)
    turnover.iloc[0] = close_weights.iloc[0].abs().sum()
    net = gross - commission * turnover

    suspect = int(((rets.abs() > SUSPECT_MOVE) & (held > 0)).to_numpy().sum())

    # HODL benchmark: equal-weight, daily-rebalanced across every symbol that has
    # a price on both days (no signals, no commission) — the same universe held blindly.
    raw = (close_df / close_df.shift(1)) * fac - 1.0
    hodl = raw.mean(axis=1, skipna=True).fillna(0.0)

    active = state_df.sum(axis=1) > 0
    first = active.idxmax() if active.any() else close_df.index[0]
    net = net.loc[first:]
    # The benchmark buys at the first entry close, so exclude the price move
    # leading into that bar from its displayed return.
    hodl = hodl.loc[first:].copy()
    hodl.iloc[0] = 0.0
    daily = pd.DataFrame({"ret": net, "n_open": n_open.loc[first:].astype(int),
                          "hodl_ret": hodl})
    daily["equity"] = (1.0 + daily["ret"]).cumprod()
    daily["hodl_equity"] = (1.0 + daily["hodl_ret"]).cumprod()
    daily["drawdown"] = (daily["equity"] / daily["equity"].cummax() - 1.0) * 100.0
    return daily, {"suspect_bars": suspect, "splits_adjusted": n_splits,
                    "universe": close_df.shape[1], "taken": taken, "first_date": daily.index[0], "last_date": daily.index[-1]}


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


def _split_multiplier(fac: pd.Series | None, start, end) -> float:
    """Product of split factors strictly after `start`, up to and including `end`."""
    if fac is None:
        return 1.0
    seg = fac[(fac.index > start) & ((fac.index <= end) if end is not None else True)]
    return float(seg.prod()) if len(seg) else 1.0


def trades_frame(
    entries: Iterable[tuple],
    commission: float = DEFAULT_COMMISSION,
) -> pd.DataFrame:
    """Flatten per-symbol TradeRecords to rows. `entries` = (symbol, trades,
    last_close[, close_series]). With `close_series`, trade returns are corrected
    for detected splits (see `split_factors`). Open trades carry a mark-to-market
    return and closed=False."""
    rows = []
    for item in entries:
        sym, trades, last_close = item[:3]
        fac = None
        if len(item) > 3 and item[3] is not None:
            c = item[3].astype(float).copy()
            c.index = _utc_index(c.index).normalize()
            f = split_factors(c.to_frame("x"))["x"]
            fac = f[f != 1.0]
        for t in trades:
            if t.entry_price in (None, 0):
                continue
            entry_ts = _utc_index([t.entry_ts])[0].normalize()
            if t.closed and t.exit_ts is not None:
                exit_norm = _utc_index([t.exit_ts])[0].normalize()
                mult = _split_multiplier(fac, entry_ts, exit_norm)
                ret = (t.exit_price * mult / t.entry_price) * (1.0 - commission) ** 2 - 1.0
                exit_ts, closed = t.exit_ts, True
            else:
                mult = _split_multiplier(fac, entry_ts, None)
                ret = last_close * mult / t.entry_price * (1.0 - commission) ** 2 - 1.0
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
