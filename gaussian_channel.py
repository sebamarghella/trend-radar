"""Pure-Python port of the GaussianChannel Strategy v3.1 logic.

Source of truth: Pine v6 strategy by @DonovanWall (Gaussian filter) wrapped in
the v3.1 trading rules (long when filter rising AND close > hband AND stoch K
in extremes; exit on close crossunder hband).

All series math matches Pine semantics where it matters:
- `nz()` -> initial zero state
- True Range uses Wilder's TR with `handle_na=true` on first bar
- RSI uses Wilder's exponential smoothing
- ta.stoch uses (src - lowest(src, n)) / (highest(src, n) - lowest(src, n)) * 100
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd


# --- Gaussian filter (Donovan Wall, N-pole, max N=9) ---------------------------


def _binomial(n: int, k: int) -> int:
    if k < 0 or k > n:
        return 0
    num = 1
    for j in range(k):
        num = num * (n - j) // (j + 1)
    return num


def _filter_series(src: np.ndarray, alpha: float, n_poles: int) -> tuple[np.ndarray, np.ndarray]:
    """Compute the n-pole filter and the 1-pole filter for fast-response mode."""
    L = len(src)
    one_minus_a = 1.0 - alpha
    alpha_pow = [alpha ** i for i in range(n_poles + 1)]
    x_pow = [one_minus_a ** k for k in range(n_poles + 2)]
    # Binomial coefs: bc[i][k] = C(i, k), 1 <= i <= n_poles, 1 <= k <= i
    bc = [[_binomial(i, k) for k in range(i + 1)] for i in range(n_poles + 1)]
    # Per-pole filter arrays
    f = np.zeros((n_poles + 1, L), dtype=np.float64)
    for t in range(L):
        s = 0.0 if np.isnan(src[t]) else float(src[t])
        for i in range(1, n_poles + 1):
            v = alpha_pow[i] * s
            kmax = min(i, 9)
            for k in range(1, kmax + 1):
                if t - k < 0:
                    continue
                sign = 1.0 if (k % 2 == 1) else -1.0
                v += sign * bc[i][k] * x_pow[k] * f[i, t - k]
            f[i, t] = v
    return f[n_poles], f[1]


def true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    """True Range with handle_na=true: first bar uses high-low."""
    L = len(close)
    tr = np.empty(L, dtype=np.float64)
    tr[0] = high[0] - low[0]
    prev_close = close[:-1]
    h_l = high[1:] - low[1:]
    h_pc = np.abs(high[1:] - prev_close)
    l_pc = np.abs(low[1:] - prev_close)
    tr[1:] = np.maximum(h_l, np.maximum(h_pc, l_pc))
    return tr


@dataclass(frozen=True)
class GCParams:
    poles: int = 4
    period: int = 144
    multiplier: float = 1.414
    reduced_lag: bool = False
    fast_response: bool = False

    @property
    def alpha(self) -> float:
        beta = (1 - math.cos(2 * math.pi / self.period)) / (math.pow(1.414, 2 / self.poles) - 1)
        return -beta + math.sqrt(beta * beta + 2 * beta)

    @property
    def lag(self) -> int:
        return int((self.period - 1) / (2 * self.poles))


def gaussian_channel(df: pd.DataFrame, params: GCParams) -> pd.DataFrame:
    """Return a DataFrame with columns: src, filt, filttr, hband, lband."""
    high = df["high"].to_numpy(dtype=np.float64)
    low = df["low"].to_numpy(dtype=np.float64)
    close = df["close"].to_numpy(dtype=np.float64)
    src = (high + low + close) / 3.0  # hlc3
    tr = true_range(high, low, close)
    if params.reduced_lag:
        lag = params.lag
        if lag > 0 and lag < len(src):
            src_lag = np.concatenate([np.full(lag, src[0]), src[:-lag]])
            tr_lag = np.concatenate([np.full(lag, tr[0]), tr[:-lag]])
            srcdata = src + (src - src_lag)
            trdata = tr + (tr - tr_lag)
        else:
            srcdata, trdata = src, tr
    else:
        srcdata, trdata = src, tr
    alpha = params.alpha
    filtn, filt1 = _filter_series(srcdata, alpha, params.poles)
    filtntr, filt1tr = _filter_series(trdata, alpha, params.poles)
    if params.fast_response:
        filt = (filtn + filt1) / 2.0
        filttr = (filtntr + filt1tr) / 2.0
    else:
        filt = filtn
        filttr = filtntr
    hband = filt + filttr * params.multiplier
    lband = filt - filttr * params.multiplier
    out = pd.DataFrame(
        {"src": src, "filt": filt, "filttr": filttr, "hband": hband, "lband": lband},
        index=df.index,
    )
    return out


# --- Stochastic RSI ------------------------------------------------------------


def wilder_rsi(series: np.ndarray, length: int) -> np.ndarray:
    L = len(series)
    rsi = np.full(L, np.nan)
    if L < length + 1:
        return rsi
    delta = np.diff(series)
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    avg_gain = np.zeros(L)
    avg_loss = np.zeros(L)
    avg_gain[length] = gain[:length].mean()
    avg_loss[length] = loss[:length].mean()
    for i in range(length + 1, L):
        avg_gain[i] = (avg_gain[i - 1] * (length - 1) + gain[i - 1]) / length
        avg_loss[i] = (avg_loss[i - 1] * (length - 1) + loss[i - 1]) / length
    rs = np.where(avg_loss == 0, np.inf, avg_gain / np.where(avg_loss == 0, 1, avg_loss))
    rsi_vals = 100 - 100 / (1 + rs)
    rsi[length:] = rsi_vals[length:]
    return rsi


def rolling_min(a: np.ndarray, n: int) -> np.ndarray:
    return pd.Series(a).rolling(n, min_periods=n).min().to_numpy()


def rolling_max(a: np.ndarray, n: int) -> np.ndarray:
    return pd.Series(a).rolling(n, min_periods=n).max().to_numpy()


def stoch_rsi_k(
    close: np.ndarray,
    rsi_length: int = 14,
    stoch_length: int = 14,
    smooth_k: int = 3,
) -> np.ndarray:
    rsi = wilder_rsi(close, rsi_length)
    lo = rolling_min(rsi, stoch_length)
    hi = rolling_max(rsi, stoch_length)
    span = hi - lo
    raw_stoch = np.where(span == 0, 0.0, (rsi - lo) / np.where(span == 0, 1, span) * 100)
    k = pd.Series(raw_stoch).rolling(smooth_k, min_periods=smooth_k).mean().to_numpy()
    return k


# --- Strategy replay -----------------------------------------------------------


@dataclass
class SignalState:
    in_position: bool
    bars_in_state: int
    entry_index: int | None
    entry_price: float | None
    bar_color: str  # one of the 6 Pine bar colors
    filter_up: bool
    close_vs_hband_pct: float  # (close - hband) / hband * 100
    stoch_k: float | None
    last_close: float
    last_filt: float
    last_hband: float
    last_lband: float


@dataclass
class TradeRecord:
    entry_ts: pd.Timestamp
    entry_price: float
    exit_ts: pd.Timestamp | None
    exit_price: float | None

    @property
    def closed(self) -> bool:
        return self.exit_price is not None

    def net_return(self, commission_per_side: float = 0.001) -> float | None:
        """Return after commission on both sides (Pine v3.1 default: 0.1%)."""
        if not self.closed:
            return None
        gross = self.exit_price / self.entry_price
        fee_factor = (1.0 - commission_per_side) ** 2
        return gross * fee_factor - 1.0


@dataclass
class BacktestStats:
    trades: int
    closed_trades: int
    net_pct: float                  # compounded total return after commissions
    win_pct: float | None           # win rate over closed trades
    avg_trade_pct: float | None
    period_days: int
    sharpe: float | None = None     # annualized per-trade Sharpe ratio
    max_drawdown_pct: float | None = None  # min equity drawdown (negative %)


def compute_stats(
    trades: list[TradeRecord],
    *,
    now: pd.Timestamp,
    lookback_days: int,
    commission_per_side: float = 0.001,
) -> BacktestStats:
    cutoff = now - pd.Timedelta(days=lookback_days)
    recent = [t for t in trades if t.entry_ts >= cutoff]
    closed = [t for t in recent if t.closed]
    rets = [t.net_return(commission_per_side) for t in closed]
    rets = [r for r in rets if r is not None]
    if not rets:
        return BacktestStats(
            trades=len(recent),
            closed_trades=0,
            net_pct=0.0,
            win_pct=None,
            avg_trade_pct=None,
            period_days=lookback_days,
            sharpe=None,
            max_drawdown_pct=None,
        )
    # Equity curve from compounded per-trade returns (entry-ordered).
    equity: list[float] = [1.0]
    for r in rets:
        equity.append(equity[-1] * (1 + r))
    cum = equity[-1]

    # Max drawdown: peak-to-trough min of the running drawdown series.
    eq_arr = np.asarray(equity, dtype=np.float64)
    peaks = np.maximum.accumulate(eq_arr)
    dd_series = (eq_arr - peaks) / peaks
    max_dd_pct = float(dd_series.min()) * 100.0  # negative number

    # Per-trade Sharpe, annualized by expected trades-per-year for this
    # lookback window. Conservative: subtracts no risk-free rate (we're
    # comparing strategies, not vs Treasuries).
    mean_r = sum(rets) / len(rets)
    n = len(rets)
    var = sum((r - mean_r) ** 2 for r in rets) / max(n - 1, 1) if n > 1 else 0.0
    std_r = var ** 0.5
    sharpe: float | None
    if std_r > 0 and lookback_days > 0:
        trades_per_year = n * 365.0 / lookback_days
        sharpe = (mean_r / std_r) * (trades_per_year ** 0.5)
    else:
        sharpe = None

    wins = sum(1 for r in rets if r > 0)
    return BacktestStats(
        trades=len(recent),
        closed_trades=n,
        net_pct=(cum - 1.0) * 100.0,
        win_pct=wins / n * 100.0,
        avg_trade_pct=mean_r * 100.0,
        period_days=lookback_days,
        sharpe=sharpe,
        max_drawdown_pct=max_dd_pct,
    )


# Pine bar colors, mapped to semantic labels.
# Re-exported from theme.py so the palette is editable in one place.
from theme import BAR_COLORS  # noqa: E402, F401


def classify_bar(src_now: float, src_prev: float, filt: float, hband: float, lband: float) -> str:
    up = src_now > src_prev
    down = src_now < src_prev
    if up and src_now > filt and src_now < hband:
        return "UP"
    if up and src_now >= hband:
        return "STRONG_UP"
    if not up and src_now > filt:
        return "WEAK_UP"
    if down and src_now < filt and src_now > lband:
        return "DOWN"
    if down and src_now <= lband:
        return "STRONG_DOWN"
    if not down and src_now < filt:
        return "WEAK_DOWN"
    return "NEUTRAL"


def replay_strategy(
    df: pd.DataFrame,
    channel: pd.DataFrame,
    stoch_k: np.ndarray,
) -> tuple[SignalState, pd.Series, list[TradeRecord]]:
    """Replay the v3.1 strategy bar by bar.

    Returns (current snapshot, long-vs-flat series for charting, list of all
    trades — closed and the still-open current one, if any).
    """
    close = df["close"].to_numpy(dtype=np.float64)
    filt = channel["filt"].to_numpy()
    hband = channel["hband"].to_numpy()
    lband = channel["lband"].to_numpy()
    index = df.index
    L = len(close)
    state = np.zeros(L, dtype=np.int8)
    trades: list[TradeRecord] = []
    in_pos = False
    entry_idx: int | None = None
    entry_price: float | None = None
    state_start = 0
    for t in range(1, L):
        if in_pos:
            crossunder = close[t] < hband[t] and close[t - 1] >= hband[t - 1]
            if crossunder:
                # Close the open trade
                trades[-1].exit_ts = index[t]
                trades[-1].exit_price = float(close[t])
                in_pos = False
                entry_idx = None
                entry_price = None
                state_start = t
        else:
            gaussian_green = filt[t] > filt[t - 1]
            k = stoch_k[t]
            stoch_ok = not np.isnan(k) and (k > 80 or k < 20)
            entry = gaussian_green and close[t] > hband[t] and stoch_ok
            if entry:
                in_pos = True
                entry_idx = t
                entry_price = float(close[t])
                state_start = t
                trades.append(TradeRecord(
                    entry_ts=index[t],
                    entry_price=entry_price,
                    exit_ts=None,
                    exit_price=None,
                ))
        state[t] = 1 if in_pos else 0
    bar_color = classify_bar(
        channel["src"].iloc[-1],
        channel["src"].iloc[-2] if L >= 2 else channel["src"].iloc[-1],
        filt[-1],
        hband[-1],
        lband[-1],
    )
    snapshot = SignalState(
        in_position=in_pos,
        bars_in_state=L - 1 - state_start,
        entry_index=entry_idx,
        entry_price=entry_price,
        bar_color=bar_color,
        filter_up=bool(filt[-1] > filt[-2]) if L >= 2 else False,
        close_vs_hband_pct=float((close[-1] - hband[-1]) / hband[-1] * 100) if hband[-1] != 0 else 0.0,
        stoch_k=float(stoch_k[-1]) if not np.isnan(stoch_k[-1]) else None,
        last_close=float(close[-1]),
        last_filt=float(filt[-1]),
        last_hband=float(hband[-1]),
        last_lband=float(lband[-1]),
    )
    return snapshot, pd.Series(state, index=index, name="long"), trades
