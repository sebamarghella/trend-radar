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


def test_stock_alerts_use_next_open_fills():
    """No entry on the signal bar; fills and Telegram prices match the next open."""
    from datetime import datetime
    from types import SimpleNamespace
    from zoneinfo import ZoneInfo

    import alerts
    import run_alerts
    from gaussian_channel import TradeRecord

    dates = pd.date_range("2026-09-21", periods=5, freq="B", tz="UTC")
    df = pd.DataFrame({"open": [95., 97., 101., 105., 112.],
                       "close": [96., 98., 103., 106., 111.]}, index=dates)
    trade = TradeRecord(entry_ts=dates[1], entry_price=98.,
                        exit_ts=dates[3], exit_price=106.)
    states = pd.Series([0, 1, 1, 0, 0], index=dates)

    def filled_at(n):
        result = SimpleNamespace(state_series=states.iloc[:n], trades=[trade])
        return run_alerts.stock_fill(result, df.iloc[:n])

    assert filled_at(2) == ("FLAT", None, None, False)
    assert filled_at(3) == ("LONG", 101., None, True)
    assert filled_at(4) == ("LONG", None, None, False)
    assert filled_at(5) == ("FLAT", 101., 112., True)

    baseline = {"stocks|AAPL|1440": "FLAT"}
    entry = {"symbol": "AAPL", "pair": "AAPL", "state": "LONG",
             "entry_price": 101., "exit_price": None, "last_close": 103., "alertable": True}
    flips, opened = alerts.detect_flips([entry], 1440, baseline, asset_class="stocks")
    assert len(flips) == 1
    assert "FlipGreen · OPEN LONG · AAPL" in flips[0].format()
    assert "Open price: $101.00" in flips[0].format()
    assert "Close price: pending" in flips[0].format()

    exit_ = {**entry, "state": "FLAT", "entry_price": 101.,
             "exit_price": 112., "last_close": 111.}
    flips, _ = alerts.detect_flips([exit_], 1440, opened, asset_class="stocks")
    assert len(flips) == 1
    assert "FlipRed · CLOSE LONG · AAPL" in flips[0].format()
    assert "Open price: $101.00" in flips[0].format()
    assert "Close price: $112.00" in flips[0].format()

    flips, _ = alerts.detect_flips([{**exit_, "alertable": False}], 1440,
                                    opened, asset_class="stocks")
    assert flips == []

    ny = ZoneInfo("America/New_York")
    assert run_alerts.should_scan(datetime(2026, 9, 21, 18, tzinfo=ny))
    assert not run_alerts.should_scan(datetime(2026, 9, 21, 9, 40, tzinfo=ny))
    assert run_alerts.should_scan(datetime(2026, 9, 21, 9, 50, tzinfo=ny))
    assert not run_alerts.should_scan(datetime(2026, 9, 26, 18, tzinfo=ny))
    seeded = run_alerts.seed_prior_filled_states([entry], {}, "stocks", 1440)
    assert seeded == {"stocks|AAPL|1440": "FLAT"}
    print("Stocks alert fills, prices, and weekday gate: ok")


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


def test_fetch_series_staleness_and_renames():
    """Offline: frozen candles fall through to the next source; renamed tickers
    get their predecessor's history stitched in front."""
    import sources

    def ohlc(start, end):
        idx = pd.date_range(start, end, freq="1D", tz="UTC")
        v = np.arange(len(idx), dtype=float) + 1
        return pd.DataFrame({"open": v, "high": v, "low": v, "close": v, "volume": v}, index=idx)

    class Fake(sources.DataSource):
        short = tv_prefix = "X"

        def __init__(self, name, frames):
            self.name, self.frames = name, frames

        def tradable_symbols(self):
            return set(self.frames)

        def candidate_symbols(self, base):
            return [base]

        def fetch_klines(self, symbol, interval_minutes):
            return self.frames[symbol]

    now = pd.Timestamp("2026-09-27T12:00:00Z")
    frozen = Fake("A", {"OLD": ohlc("2025-01-01", "2026-06-30"), "NEW": ohlc("2026-07-01", "2026-09-27")})
    live = Fake("B", {"OLD": ohlc("2025-01-01", "2026-09-27")})
    r = sources.Resolver([frozen, live])

    # Source A's OLD is frozen in June -> falls through to B.
    res = sources.fetch_series("OLD", r, 1440, now=now)
    assert res.source.name == "B"

    # Only frozen data anywhere -> StaleDataError.
    try:
        sources.fetch_series("OLD", sources.Resolver([frozen]), 1440, now=now)
        raise AssertionError("expected StaleDataError")
    except sources.StaleDataError:
        pass

    # Stocks: a 4-day-old daily bar (long weekend) is not stale.
    res = sources.fetch_series("OLD", r, 1440, is_24_7=False, now=pd.Timestamp("2026-10-01T00:00:00Z"))
    assert res.source.name == "B"

    # Rename stitching: NEW gets OLD's pre-cutoff history, no duplicates, no gap.
    orig = dict(sources.RENAMES)
    sources.RENAMES["NEW"] = [sources.Predecessor("OLD", until="2026-07-01")]
    try:
        res = sources.fetch_series("NEW", sources.Resolver([frozen]), 1440, now=now)
    finally:
        sources.RENAMES.clear()
        sources.RENAMES.update(orig)
    assert res.stitched_from == "OLD"
    assert res.df.index[0] == pd.Timestamp("2025-01-01", tz="UTC")
    assert res.df.index.is_unique and res.df.index.is_monotonic_increasing
    assert (res.df.index.to_series().diff().dropna() == pd.Timedelta(days=1)).all()
    print("fetch_series staleness + renames: ok")


def test_stocks_universe_ranking():
    """Offline: share classes collapse to one row, preferreds drop, ranks are dense."""
    from stocks_universe import clean_name, rank_rows

    rows = [
        {"symbol": "GOOG", "name": "Alphabet Inc. Class C Capital Stock", "marketCap": "3900"},
        {"symbol": "GOOGL", "name": "Alphabet Inc. Class A Common Stock", "marketCap": "4000"},
        {"symbol": "BRK/B", "name": "Berkshire Hathaway Inc. Class B", "marketCap": "1000"},
        {"symbol": "BRK/A", "name": "Berkshire Hathaway Inc. Class A", "marketCap": "999"},
        {"symbol": "JPM^C", "name": "JPMorgan Chase & Co. Depositary Shares", "marketCap": "5000"},
        {"symbol": "NVO", "name": "Novo Nordisk A/S American Depositary Shares", "marketCap": "170"},
        {"symbol": "ZERO", "name": "No Cap Corp", "marketCap": ""},
    ]
    out = rank_rows(rows, target=10)
    assert [r["symbol"] for r in out] == ["GOOGL", "BRK-B", "NVO"], out
    assert [r["rank"] for r in out] == [1, 2, 3]
    assert clean_name("Novo Nordisk A/S American Depositary Shares") == "Novo Nordisk A/S"
    assert len(rank_rows(rows, target=1)) == 1
    print("stocks universe ranking: ok")


def test_run_strategy_cached():
    import strategies as S

    idx = pd.date_range("2025-01-01", periods=300, freq="1D", tz="UTC")
    c = pd.Series(np.cumsum(np.random.default_rng(7).normal(0, 1, 300)) + 100, index=idx)
    df = pd.DataFrame({"open": c, "high": c + 1, "low": c - 1, "close": c, "volume": 1.0})
    st = S.load_strategies()[S.DEFAULT_STRATEGY_NAME]
    a = S.run_strategy_cached(st, df)
    assert S.run_strategy_cached(st, df.copy()) is a          # same data -> cache hit
    df2 = df.copy(); df2.iloc[-1, df2.columns.get_loc("close")] += 1
    assert S.run_strategy_cached(st, df2) is not a            # changed bar -> recompute
    assert a.snapshot.last_close == S.run_strategy(st, df).snapshot.last_close
    print("run_strategy_cached: ok")


def test_breakouts():
    """Offline: range -> breakout -> validated / invalidated; early escape drops the range."""
    import breakouts as B

    def frame(closes):
        idx = pd.date_range("2026-01-01", periods=len(closes), freq="1D", tz="UTC")
        c = pd.Series(closes, index=idx, dtype=float)
        return pd.DataFrame({"open": c, "high": c + 0.5, "low": c - 0.5, "close": c, "volume": 1.0})

    # rally into a swing high at 100, 30 days of 92-98 chop (swing low ~92), then a breakout
    chop = [95, 97, 93, 96, 92, 94, 97, 95, 93, 96] * 3
    base = list(range(80, 100)) + [100] + [98, 97] + chop
    up = lambda df, k: pd.Series(df.index >= df.index[k], index=df.index)

    df = frame(base + [103, 105, 106])
    st = B.detect(df, up(df, len(base) + 1))           # trend turns up the day after the breakout
    assert st.last is not None and st.last.date == df.index[len(base)], st
    assert abs(st.last.resistance - 100.5) < 1e-9 and st.last.support < 95
    assert st.last.status == "validated" and st.last.status_date == df.index[len(base) + 1]

    df2 = frame(base + [103, 90, 89])                  # breaks out, then closes below support
    st2 = B.detect(df2, pd.Series(False, index=df2.index))
    assert st2.last.status == "invalidated", st2.last

    early = list(range(80, 100)) + [100] + [98, 97, 96, 95, 103, 104, 105]  # above 100.5 after 5 days
    st3 = B.detect(frame(early), None)
    assert st3.last is None, st3.last
    print("breakouts: ok")


def test_performance_entry_exit_alignment():
    """A signal-close fill pays its fee now and the benchmark starts there."""
    import performance as perf

    idx = pd.date_range("2024-01-01", periods=4, freq="D", tz="UTC")
    close = pd.Series([100.0, 110.0, 121.0, 133.1], index=idx)
    frame = pd.DataFrame({"close": close})
    state = pd.Series([0, 1, 1, 0], index=idx)
    daily, _ = perf.basket_daily([("TEST", frame, state)])
    assert np.allclose(daily["ret"], [-0.001, 0.1, 0.099])
    assert np.allclose(daily["hodl_ret"], [0.0, 0.1, 0.1])
    assert daily["n_open"].tolist() == [0, 1, 1]
    capped, _ = perf.basket_daily([("TEST", frame, state)], slots=1)
    assert np.allclose(capped["ret"], daily["ret"])
    sized, _ = perf.basket_daily(
        [("TEST", frame, state)], position_fraction=perf.GC_STOCKS_POSITION_FRACTION,
    )
    assert np.allclose(sized["ret"], [-0.00095, 0.095, 0.09405])
    assert np.allclose(sized["hodl_ret"], daily["hodl_ret"])
    sized_capped, _ = perf.basket_daily(
        [("TEST", frame, state)], slots=1,
        position_fraction=perf.GC_STOCKS_POSITION_FRACTION,
    )
    assert np.allclose(sized_capped["ret"], sized["ret"])

    late_state = pd.Series([0, 0, 0, 1], index=idx)
    late, _ = perf.basket_daily([("TEST", frame, late_state)])
    assert np.isclose(late["ret"].iloc[0], -0.001)
    assert np.isclose(late["hodl_ret"].iloc[0], 0.0)
    print("performance entry/exit alignment: ok")


def test_next_open_fills():
    """A signal waits for the next open; exits retain the overnight gap."""
    import performance as perf
    from gaussian_channel import TradeRecord

    idx = pd.date_range("2024-01-01", periods=5, freq="D", tz="UTC")
    frame = pd.DataFrame({"open": [100., 105., 120., 140., 150.],
                          "close": [100., 110., 132., 154., 165.]}, index=idx)
    state = pd.Series([0, 1, 1, 0, 0], index=idx)
    signal_trade = TradeRecord(idx[1], 110., idx[3], 154.)
    fills = perf.filled_trades([signal_trade], frame)
    assert len(fills) == 1
    assert (fills[0].entry_ts, fills[0].entry_price) == (idx[2], 120.)
    assert (fills[0].exit_ts, fills[0].exit_price) == (idx[4], 150.)
    daily, _ = perf.basket_daily([("TEST", frame, state)],
                                 position_fraction=.95, fill_mode="next_open")
    assert daily.index[0] == idx[2]
    assert daily["n_open"].tolist() == [1, 1, 0]
    assert np.isclose(daily["ret"].iloc[0], .95 * .10 - .001 * .95)
    assert np.isclose(daily["ret"].iloc[1],
                      (1 + .95 * (140 / 132 - 1)) * (1 + .95 * .10) - 1)
    gap = .95 * (150 / 154 - 1)
    assert np.isclose(daily["ret"].iloc[2], gap - .001 * .95 * (1 + gap))
    assert np.isclose(daily["hodl_ret"].iloc[0], .10)

    pending = TradeRecord(idx[1], 110., idx[4], 165.)
    assert perf.filled_trades([pending], frame)[0].exit_ts is None
    latest_signal = TradeRecord(idx[4], 165., None, None)
    assert perf.filled_trades([latest_signal], frame) == []

    # A missing bar for one symbol must not fill its order on another
    # symbol's next date.
    other = frame.iloc[[0, 2, 4]]
    other_state = pd.Series([1, 1, 1], index=other.index)
    _, _, executed = perf._next_open_frame([
        ("TEST", frame, state), ("OTHER", other, other_state),
    ])
    assert executed.loc[idx[1], "OTHER"] == 0
    assert executed.loc[idx[2], "OTHER"] == 1
    print("next-open fills: ok")


def test_next_open_forward_log():
    """A pending signal does not enter the forward trade log before its open."""
    import tempfile
    from pathlib import Path
    from types import SimpleNamespace

    import live_log
    from gaussian_channel import TradeRecord

    idx = pd.date_range("2024-01-01", periods=4, freq="D", tz="UTC")
    frame = pd.DataFrame({"open": [100., 100., 100., 110.],
                          "close": [100., 100., 110., 121.]}, index=idx)
    trade = TradeRecord(idx[2], 110., None, None)
    original_cache = live_log.CACHE_DIR
    with tempfile.TemporaryDirectory() as tmp:
        live_log.CACHE_DIR = Path(tmp)
        try:
            first = SimpleNamespace(state_series=pd.Series([0, 0, 1], index=idx[:3]),
                                    trades=[trade])
            live_log.update("stocks", "GC stocks", [("TEST", frame.iloc[:3], first)],
                            rerun=lambda _: first, position_fraction=.95,
                            fill_mode="next_open", today=idx[3])
            assert live_log.load_trades("stocks") == []
            second = SimpleNamespace(state_series=pd.Series([0, 0, 1, 1], index=idx),
                                     trades=[trade])
            live_log.update("stocks", "GC stocks", [("TEST", frame, second)],
                            rerun=lambda _: second, position_fraction=.95,
                            fill_mode="next_open", today=idx[3] + pd.Timedelta(days=1))
            logged = live_log.load_trades("stocks")
            assert len(logged) == 1
            assert logged[0]["entry_date"] == "2024-01-04"
            assert logged[0]["entry_price"] == 110.
            assert logged[0]["fill_mode"] == "next_open"
            days = live_log.live_daily_frame("stocks")
            assert days["n_open"].tolist() == [0, 1]
        finally:
            live_log.CACHE_DIR = original_cache
    print("next-open forward log: ok")


def test_gc_stocks_green_red_flips():
    """The stock strategy trades only a red→green or green→red filter flip."""
    from unittest.mock import patch
    import strategies

    idx = pd.date_range("2024-01-01", periods=8, freq="W-MON", tz="UTC")
    frame = pd.DataFrame({"close": np.arange(100.0, 108.0)}, index=idx)
    filt = np.array([10.0, 9.0, 8.0, 9.0, 10.0, 9.0, 8.0, 9.0])
    channel = pd.DataFrame({"src": frame["close"], "filt": filt,
                            "hband": filt + 1, "lband": filt - 1}, index=idx)
    with patch.object(strategies, "gaussian_channel", return_value=channel):
        result = strategies.LOGICS["gaussian_channel_stocks_v1"].run(
            frame, strategies.LOGICS["gaussian_channel_stocks_v1"].defaults(),
        )
    assert result.state_series.tolist() == [0, 0, 0, 1, 1, 0, 0, 1]
    assert [(t.entry_ts, t.exit_ts) for t in result.trades] == [
        (idx[3], idx[5]), (idx[7], None),
    ]
    print("GC stocks green/red flips: ok")


def test_open_stock_kept_after_leaving_ranked_universe():
    """A saved LONG still gets its closing alert after leaving the top 570."""
    from datetime import date
    from types import SimpleNamespace
    from unittest.mock import patch

    import run_alerts
    from gaussian_channel import TradeRecord

    dates = pd.date_range("2026-09-21", periods=5, freq="B", tz="UTC")
    df = pd.DataFrame({"open": [95., 97., 101., 105., 112.],
                       "close": [96., 98., 103., 106., 111.]}, index=dates)
    trade = TradeRecord(entry_ts=dates[1], entry_price=98.,
                        exit_ts=dates[3], exit_price=106.)
    result = SimpleNamespace(
        state_series=pd.Series([0, 1, 1, 0, 0], index=dates),
        trades=[trade],
        snapshot=SimpleNamespace(last_close=111., stoch_k=None,
                                 filter_up=False, close_vs_hband_pct=0.),
    )
    strategy = SimpleNamespace(name="GC stocks",
                               to_dict=lambda: {"name": "GC stocks",
                                                "logic_key": "gaussian_channel_stocks_v1",
                                                "params": {}})
    resolver = SimpleNamespace(coverage=lambda: {"Yahoo": 1})
    ac = SimpleNamespace(
        key="stocks", label="Stocks", interval_options=[("1 day", 1440)],
        default_interval_idx=0, resolver_factory=lambda: resolver,
        get_universe=lambda _: [{"symbol": "MSFT"}], is_24_7=False,
        alerts_enabled=True,
    )
    previous = {"__strategy__|stocks": "GC stocks|next-open-fills-v1",
                "stocks|AAPL|1440": "LONG",
                "stocks|MSFT|1440": "LONG"}  # historic model LONG, no delivered entry
    alerted_positions = {"AAPL": {"entry_date": "2026-09-23", "entry_price": 101.}}
    scanned = []

    def fetch(symbol, *_args):
        scanned.append(symbol)
        if symbol == "AAPL":
            return {"symbol": symbol, "pair": symbol, "exchange": "Yahoo", "df": df}
        return None

    with (patch.object(run_alerts.strat_registry, "load_strategies",
                       return_value={strategy.name: strategy}),
          patch.object(run_alerts.strat_registry, "get_assignment",
                       return_value=strategy.name),
          patch.object(run_alerts.strat_registry, "run_strategy", return_value=result),
          patch.object(run_alerts, "fetch_one", side_effect=fetch),
          patch.object(run_alerts.live_log, "LIVE_CLASSES", set()),
          patch.object(run_alerts.alerts, "record_flips"),
          patch.object(run_alerts.alerts, "fire_alerts",
                       side_effect=lambda flips, *_: (flips, [])) as sent):
        state, positions, issues = run_alerts.scan_class(
            ac, 2, previous, "token", "chat", date(2026, 9, 25), alerted_positions.copy(),
        )
    assert sorted(scanned) == ["AAPL", "MSFT"]
    assert state["stocks|AAPL|1440"] == "FLAT" and not issues
    assert positions == {}
    flips = sent.call_args.args[0]
    assert len(flips) == 1 and flips[0].symbol == "AAPL"
    assert flips[0].direction == "EXIT" and flips[0].exit_price == 112.

    with (patch.object(run_alerts.strat_registry, "load_strategies",
                       return_value={strategy.name: strategy}),
          patch.object(run_alerts.strat_registry, "get_assignment",
                       return_value=strategy.name),
          patch.object(run_alerts, "fetch_one", return_value=None),
          patch.object(run_alerts.live_log, "LIVE_CLASSES", set())):
        state, positions, issues = run_alerts.scan_class(
            ac, 2, previous, "token", "chat", date(2026, 9, 25), alerted_positions.copy(),
        )
    assert state["stocks|AAPL|1440"] == "LONG"
    assert "AAPL" in positions
    assert len(issues) == 1 and "AAPL" in issues[0]

    # A historic model LONG with no delivered OPEN alert must not emit a
    # confusing CLOSE alert, even when it exits today.
    ac.get_universe = lambda _: [{"symbol": "AAPL"}]
    with (patch.object(run_alerts.strat_registry, "load_strategies",
                       return_value={strategy.name: strategy}),
          patch.object(run_alerts.strat_registry, "get_assignment",
                       return_value=strategy.name),
          patch.object(run_alerts.strat_registry, "run_strategy", return_value=result),
          patch.object(run_alerts, "fetch_one", side_effect=fetch),
          patch.object(run_alerts.live_log, "LIVE_CLASSES", set()),
          patch.object(run_alerts.alerts, "fire_alerts") as send_unpaired):
        _, positions, issues = run_alerts.scan_class(
            ac, 2, previous, "token", "chat", date(2026, 9, 25), {},
        )
    assert positions == {} and not issues
    send_unpaired.assert_not_called()

    # If Friday's scan was missed, the next scan still delivers the tracked
    # CLOSE with Friday's fill date and price.
    later = dates[-1] + pd.Timedelta(days=3)
    late_df = pd.concat([df, pd.DataFrame({"open": [113.], "close": [114.]},
                                           index=pd.DatetimeIndex([later]))])
    late_result = SimpleNamespace(
        state_series=pd.Series([0, 1, 1, 0, 0, 0], index=late_df.index),
        trades=[trade], snapshot=result.snapshot,
    )
    ac.get_universe = lambda _: [{"symbol": "MSFT"}]
    with (patch.object(run_alerts.strat_registry, "load_strategies",
                       return_value={strategy.name: strategy}),
          patch.object(run_alerts.strat_registry, "get_assignment",
                       return_value=strategy.name),
          patch.object(run_alerts.strat_registry, "run_strategy", return_value=late_result),
          patch.object(run_alerts, "fetch_one",
                       side_effect=lambda symbol, *_: {"symbol": symbol, "pair": symbol,
                                                        "exchange": "Yahoo", "df": late_df}
                       if symbol == "AAPL" else None),
          patch.object(run_alerts.live_log, "LIVE_CLASSES", set()),
          patch.object(run_alerts.alerts, "record_flips"),
          patch.object(run_alerts.alerts, "fire_alerts",
                       side_effect=lambda flips, *_: (flips, [])) as sent_late):
        _, positions, issues = run_alerts.scan_class(
            ac, 2, previous, "token", "chat", date(2026, 9, 28),
            alerted_positions.copy(),
        )
    late_flip = sent_late.call_args.args[0][0]
    assert late_flip.late and late_flip.fill_date == "2026-09-25"
    assert "late notice" in late_flip.format()
    assert positions == {} and not issues

    # Only a successfully delivered new OPEN becomes a tracked position.
    new_trade = TradeRecord(entry_ts=dates[-2], entry_price=106.,
                            exit_ts=None, exit_price=None)
    entry_result = SimpleNamespace(
        state_series=pd.Series([0, 0, 0, 1, 1], index=dates),
        trades=[new_trade], snapshot=result.snapshot,
    )
    ac.get_universe = lambda _: [{"symbol": "AAPL"}]
    with (patch.object(run_alerts.strat_registry, "load_strategies",
                       return_value={strategy.name: strategy}),
          patch.object(run_alerts.strat_registry, "get_assignment",
                       return_value=strategy.name),
          patch.object(run_alerts.strat_registry, "run_strategy", return_value=entry_result),
          patch.object(run_alerts, "fetch_one", side_effect=fetch),
          patch.object(run_alerts.live_log, "LIVE_CLASSES", set()),
          patch.object(run_alerts.alerts, "record_flips"),
          patch.object(run_alerts.alerts, "fire_alerts",
                       side_effect=lambda flips, *_: (flips, []))):
        _, positions, issues = run_alerts.scan_class(
            ac, 2, {**previous, "stocks|AAPL|1440": "FLAT"}, "token", "chat",
            date(2026, 9, 25), {},
        )
    assert not issues and positions["AAPL"]["entry_date"] == "2026-09-25"
    assert positions["AAPL"]["entry_price"] == 112.
    assert positions["AAPL"]["strategy"] == strategy.to_dict()
    with (patch.object(run_alerts.strat_registry, "load_strategies",
                       return_value={strategy.name: strategy}),
          patch.object(run_alerts.strat_registry, "get_assignment",
                       return_value=strategy.name),
          patch.object(run_alerts.strat_registry, "run_strategy", return_value=entry_result),
          patch.object(run_alerts, "fetch_one", side_effect=fetch),
          patch.object(run_alerts.live_log, "LIVE_CLASSES", set()),
          patch.object(run_alerts.alerts, "record_flips") as record_failed,
          patch.object(run_alerts.alerts, "fire_alerts",
                       return_value=([], ["AAPL: Telegram unavailable"]))):
        state, positions, issues = run_alerts.scan_class(
            ac, 2, {**previous, "stocks|AAPL|1440": "FLAT"}, "token", "chat",
            date(2026, 9, 25), {},
        )
    assert positions == {} and state["stocks|AAPL|1440"] == "FLAT"
    assert issues == ["AAPL: Telegram unavailable"]
    record_failed.assert_not_called()
    print("open stock remains tracked after ranking exit: ok")


def test_alert_history_keeps_reporting_fields():
    """The cumulative report must retain fills even beyond the old 500-row cap."""
    from pathlib import Path
    from tempfile import TemporaryDirectory
    from unittest.mock import patch

    import alerts

    flip = alerts.Flip(
        symbol="AMD", pair="AMD", direction="ENTRY", price=616.96,
        stoch_k=None, filter_up=True, close_vs_hband_pct=0.,
        interval_minutes=1440, asset_class="stocks", entry_price=616.96,
        fill_date="2026-09-29", strategy_name="GC stocks",
    )
    with TemporaryDirectory() as folder:
        with patch.object(alerts, "HISTORY_FILE", Path(folder) / "history.json"):
            alerts.save_history([{"n": n} for n in range(500)])
            history = alerts.record_flips([flip], "stocks", "2026-09-29T19:33:54Z")
            assert len(history) == len(alerts.load_history()) == 501
            assert history[-1]["fill_date"] == "2026-09-29"
            assert history[-1]["entry_price"] == 616.96
            assert history[-1]["strategy_name"] == "GC stocks"
    print("uncapped Stocks alert history with fill dates: ok")


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
    test_stock_alerts_use_next_open_fills()
    test_gc_short_state()
    test_crypto_signal_export_helpers()
    test_fetch_series_staleness_and_renames()
    test_stocks_universe_ranking()
    test_run_strategy_cached()
    test_breakouts()
    test_performance_entry_exit_alignment()
    test_next_open_fills()
    test_next_open_forward_log()
    test_gc_stocks_green_red_flips()
    test_open_stock_kept_after_leaving_ranked_universe()
    test_alert_history_keeps_reporting_fields()
    print("\nAll smoke tests passed.")
