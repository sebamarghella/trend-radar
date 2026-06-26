"""Smoke test the Gaussian filter, RSI, and strategy replay on synthetic data.

Run: python smoke_test.py
"""

import math

import numpy as np
import pandas as pd

from gaussian_channel import (
    GCParams,
    classify_bar,
    gaussian_channel,
    replay_strategy,
    stoch_rsi_k,
    true_range,
    wilder_rsi,
)


def synth_ohlc(n: int = 1000, seed: int = 42) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    drift = np.linspace(0, 0.5, n)
    noise = rng.standard_normal(n) * 0.01
    cycle = 0.05 * np.sin(np.arange(n) * 2 * math.pi / 200)
    log_close = drift + cycle + np.cumsum(noise)
    close = 100 * np.exp(log_close)
    high = close * (1 + np.abs(rng.standard_normal(n)) * 0.005)
    low = close * (1 - np.abs(rng.standard_normal(n)) * 0.005)
    open_ = np.roll(close, 1)
    open_[0] = close[0]
    idx = pd.date_range("2020-01-01", periods=n, freq="4h", tz="UTC")
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close}, index=idx)


def test_true_range():
    df = synth_ohlc(50)
    tr = true_range(df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy())
    assert tr.shape == (50,)
    assert tr[0] == df["high"].iloc[0] - df["low"].iloc[0]
    assert (tr >= 0).all()
    print(f"true_range: ok, range {tr.min():.4f}–{tr.max():.4f}")


def test_gaussian_channel_step():
    """A constant input should make the filter converge to that constant."""
    n = 2000
    const = 100.0
    idx = pd.date_range("2020-01-01", periods=n, freq="4h", tz="UTC")
    df = pd.DataFrame({"open": const, "high": const, "low": const, "close": const}, index=idx)
    params = GCParams(poles=4, period=144, multiplier=1.414)
    ch = gaussian_channel(df, params)
    # After 5*period, the filter must have converged to within 0.1% of the constant.
    assert abs(ch["filt"].iloc[-1] - const) / const < 1e-3
    # TR is zero on a flat series after bar 0, so hband/lband should equal filt.
    assert abs(ch["hband"].iloc[-1] - ch["filt"].iloc[-1]) < 1e-3
    assert 0 < params.alpha < 1
    print(f"gaussian_channel (step): ok, alpha={params.alpha:.5f}, "
          f"converged to {ch['filt'].iloc[-1]:.4f} vs {const}")


def test_gaussian_channel_ramp():
    """A monotone-up ramp should make the filter rise monotonically (after settle)."""
    n = 2000
    idx = pd.date_range("2020-01-01", periods=n, freq="4h", tz="UTC")
    close = np.linspace(100, 200, n)
    df = pd.DataFrame({"open": close, "high": close + 0.1, "low": close - 0.1, "close": close}, index=idx)
    params = GCParams(poles=4, period=144, multiplier=1.414)
    ch = gaussian_channel(df, params)
    settle = 5 * 144
    filt_post = ch["filt"].iloc[settle:]
    src_post = ch["src"].iloc[settle:]
    # Filter must rise monotonically across the settled region.
    assert (filt_post.diff().dropna() > 0).all()
    # And track the ramp in direction.
    corr = src_post.corr(filt_post)
    assert corr > 0.99
    # Filter is smoother than source (trivially true on a ramp, lag dominates).
    print(f"gaussian_channel (ramp): ok, corr={corr:.4f}, "
          f"final filt={filt_post.iloc[-1]:.2f} vs src={src_post.iloc[-1]:.2f}")


def test_wilder_rsi():
    df = synth_ohlc(200)
    rsi = wilder_rsi(df["close"].to_numpy(), 14)
    assert np.isnan(rsi[:14]).all()
    valid = rsi[14:]
    assert ((valid >= 0) & (valid <= 100)).all()
    print(f"wilder_rsi: ok, sample={valid[-1]:.2f}")


def test_stoch_k():
    df = synth_ohlc(200)
    k = stoch_rsi_k(df["close"].to_numpy())
    valid = k[~np.isnan(k)]
    assert ((valid >= 0) & (valid <= 100)).all()
    print(f"stoch_rsi_k: ok, sample={k[-1]:.2f}")


def test_classify_bar():
    # Strong up: rising and at/above hband
    assert classify_bar(110, 105, 100, 108, 92) == "STRONG_UP"
    # Up: rising, above filt, below hband
    assert classify_bar(102, 100, 100, 108, 92) == "UP"
    # Strong down: falling and at/below lband
    assert classify_bar(90, 95, 100, 108, 92) == "STRONG_DOWN"
    # Down: falling, below filt, above lband
    assert classify_bar(95, 100, 100, 108, 92) == "DOWN"
    # Weak up: not falling (flat) AND above filt
    assert classify_bar(105, 105, 100, 108, 92) == "WEAK_UP"
    # Weak down: not rising (flat) AND below filt
    assert classify_bar(95, 95, 100, 108, 92) == "WEAK_DOWN"
    # Truly neutral: src == filt and flat
    assert classify_bar(100, 100, 100, 108, 92) == "NEUTRAL"
    print("classify_bar: ok")


def test_replay():
    df = synth_ohlc(800)
    params = GCParams(poles=4, period=144, multiplier=1.414)
    ch = gaussian_channel(df, params)
    k = stoch_rsi_k(df["close"].to_numpy())
    snap, state, trades = replay_strategy(df, ch, k)
    assert len(state) == len(df)
    # Replay shouldn't crash on settle window where filt is still warming up.
    print(f"replay: ok, current state={'LONG' if snap.in_position else 'FLAT'}, "
          f"bars_in_state={snap.bars_in_state}, "
          f"close_vs_hband={snap.close_vs_hband_pct:+.2f}%, "
          f"stoch_k={snap.stoch_k:.1f}, trades_recorded={len(trades)}")


def test_backtest_stats():
    from gaussian_channel import compute_stats
    df = synth_ohlc(800)
    params = GCParams(poles=4, period=144, multiplier=1.414)
    ch = gaussian_channel(df, params)
    k = stoch_rsi_k(df["close"].to_numpy())
    _snap, _state, trades = replay_strategy(df, ch, k)
    stats = compute_stats(trades, now=df.index[-1], lookback_days=180)
    assert stats.trades >= 0
    if stats.closed_trades > 0:
        assert stats.win_pct is not None and 0 <= stats.win_pct <= 100
    print(f"backtest_stats: ok, trades={stats.trades} ({stats.closed_trades} closed), "
          f"net={stats.net_pct:+.2f}%, "
          f"win={stats.win_pct if stats.win_pct is None else f'{stats.win_pct:.0f}%'}")


def test_alerts_flip_detection():
    import alerts
    sigs = [
        {"symbol": "BTC", "pair": "BTCUSDT", "state": "LONG", "last_close": 67000.0,
         "stoch_k": 85.0, "filter_up": True, "close_vs_hband_pct": 1.2},
        {"symbol": "ETH", "pair": "ETHUSDT", "state": "FLAT", "last_close": 1900.0,
         "stoch_k": 50.0, "filter_up": False, "close_vs_hband_pct": -3.4},
    ]
    # First run: silent seed
    flips, new_state = alerts.detect_flips(sigs, 240, prev_state={}, asset_class="crypto")
    assert flips == []
    assert "crypto|BTC|240" in new_state and new_state["crypto|BTC|240"] == "LONG"

    # No change: no flips
    flips, _ = alerts.detect_flips(sigs, 240, prev_state=new_state, asset_class="crypto")
    assert flips == []

    # BTC flips to FLAT: should fire EXIT
    sigs[0]["state"] = "FLAT"
    btc_flips, _ = alerts.detect_flips(sigs, 240, prev_state=new_state, asset_class="crypto")
    assert len(btc_flips) == 1 and btc_flips[0].symbol == "BTC" and btc_flips[0].direction == "EXIT"

    # Different asset class with same symbol shouldn't pollute
    stock_sigs = [{"symbol": "AAPL", "pair": "AAPL", "state": "LONG", "last_close": 200.0,
                   "stoch_k": 50.0, "filter_up": True, "close_vs_hband_pct": 1.0}]
    stock_flips, post = alerts.detect_flips(stock_sigs, 1440, prev_state=new_state, asset_class="stocks")
    assert stock_flips == [], "stocks first-run should be silent even when crypto baseline exists"
    assert "crypto|BTC|240" in post, "must preserve other-class state"
    assert "stocks|AAPL|1440" in post, "must add new-class state"
    msg = btc_flips[0].format()
    assert "EXIT" in msg and "BTC" in msg and "BTCUSDT" in msg
    # Encode-safe preview (the actual Telegram message is unicode over HTTP).
    preview = msg.encode("ascii", "replace").decode("ascii")
    print(f"alerts: ok, sample message:\n  {preview.replace(chr(10), chr(10)+'  ')}")


def test_gc_short_state():
    """The SHORT mirror engages on a steep crash and stays flat on a flat tape."""
    import export_crypto_signals as exporter
    from strategies import LOGICS

    params = LOGICS["gaussian_channel_v3_1"].coerce({})
    gc = GCParams(
        poles=params["poles"],
        period=params["period"],
        multiplier=params["multiplier"],
        reduced_lag=params["reduced_lag"],
        fast_response=params["fast_response"],
    )
    n = 1000
    idx = pd.date_range("2020-01-01", periods=n, freq="4h", tz="UTC")

    # 600 flat bars then a steep, sustained crash -> filter falls, close breaks
    # below the lower band, stoch pins to extremes: the short should engage.
    crash = np.concatenate([np.full(600, 100.0), np.linspace(100.0, 40.0, n - 600)])
    cdf = pd.DataFrame(
        {"open": crash, "high": crash * 1.001, "low": crash * 0.999, "close": crash},
        index=idx,
    )
    cch = gaussian_channel(cdf, gc)
    s = exporter._gc_short_state(cdf, cch, params)
    assert len(s) == n
    assert set(np.unique(s.to_numpy())).issubset({0, 1})
    assert int(s.to_numpy().sum()) > 0, "steep crash should trigger the short"

    # A perfectly flat tape can never satisfy filter-falling + close<lband.
    flat = np.full(n, 100.0)
    fdf = pd.DataFrame({"open": flat, "high": flat, "low": flat, "close": flat}, index=idx)
    fch = gaussian_channel(fdf, gc)
    sf = exporter._gc_short_state(fdf, fch, params)
    assert int(sf.to_numpy().sum()) == 0, "flat tape must produce no shorts"
    print(f"gc_short_state: ok, short bars on crash={int(s.to_numpy().sum())}, flat=0")


def test_crypto_signal_export_helpers():
    import export_crypto_signals as exporter

    idx = pd.date_range("2026-06-24", periods=3, freq="1D", tz="UTC")
    df = pd.DataFrame(
        {"open": [1, 2, 3], "high": [1, 2, 3], "low": [1, 2, 3], "close": [1, 2, 3]},
        index=idx,
    )
    completed = exporter._completed_ohlc(df, 1440, now=pd.Timestamp("2026-06-26T00:30:00Z"))
    assert list(completed.index) == list(idx[:2])
    assert exporter._bar_close_utc(completed, 1440) == pd.Timestamp("2026-06-26T00:00:00Z")
    assert exporter._hl_symbol("BTC", {"BTC", "kPEPE"}) == "BTC"
    assert exporter._hl_symbol("PEPE", {"BTC", "kPEPE"}) == "kPEPE"
    assert exporter._hl_symbol("NOTLISTED", {"BTC", "kPEPE"}) is None
    print("crypto signals export helpers: ok")


if __name__ == "__main__":
    test_true_range()
    test_gaussian_channel_step()
    test_gaussian_channel_ramp()
    test_wilder_rsi()
    test_stoch_k()
    test_classify_bar()
    test_replay()
    test_backtest_stats()
    test_alerts_flip_detection()
    test_gc_short_state()
    test_crypto_signal_export_helpers()
    print("\nAll smoke tests passed.")
