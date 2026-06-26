"""Export daily crypto strategy signals for external automation.

This is a headless producer for `data/crypto_signals.json`. It intentionally
does not depend on Streamlit or Telegram secrets, so GitHub Actions can refresh
the JSON even when nobody opens the app.
"""

from __future__ import annotations

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd
import requests

import strategies as strat_registry
from asset_classes import CRYPTO
from gaussian_channel import stoch_rsi_k as _gc_stoch
from sources import Resolver, SourceError


OUTPUT_PATH = Path(__file__).parent / "data" / "crypto_signals.json"
HL_INFO_URL = "https://api.hyperliquid.xyz/info"
GC_LOGIC_KEY = "gaussian_channel_v3_1"


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _iso_z(ts: pd.Timestamp) -> str:
    ts = pd.Timestamp(ts)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def _timeframe_label(interval_minutes: int) -> str:
    if interval_minutes % 1440 == 0:
        return f"{interval_minutes // 1440}d"
    if interval_minutes % 60 == 0:
        return f"{interval_minutes // 60}h"
    return f"{interval_minutes}m"


def _completed_ohlc(
    df: pd.DataFrame,
    interval_minutes: int,
    now: pd.Timestamp | None = None,
) -> pd.DataFrame:
    """Keep only bars whose close time is at or before `now`.

    Exchange OHLC rows are indexed by bar open time. A daily run shortly after
    00:00 UTC often includes the newly opened, incomplete candle; this drops it.
    """
    if df.empty:
        return df

    now = now or pd.Timestamp.now(tz="UTC")
    now = pd.Timestamp(now)
    if now.tzinfo is None:
        now = now.tz_localize("UTC")
    else:
        now = now.tz_convert("UTC")

    out = df.copy()
    out.index = pd.to_datetime(out.index, utc=True)
    close_times = out.index + pd.Timedelta(minutes=interval_minutes)
    return out.loc[close_times <= now]


def _bar_close_utc(df: pd.DataFrame, interval_minutes: int) -> pd.Timestamp:
    return pd.Timestamp(df.index[-1]).tz_convert("UTC") + pd.Timedelta(minutes=interval_minutes)


def _fetch_one(base: dict, resolver: Resolver, interval_minutes: int) -> dict | None:
    symbol = base["symbol"]
    hit = resolver.resolve(symbol)
    if hit is None:
        return None

    src, resolved = hit
    try:
        df = src.fetch_with_retry(resolved, interval_minutes=interval_minutes)
    except SourceError as e:
        print(f"  [warn] {symbol} ({src.name}:{resolved}): {e}", file=sys.stderr)
        return None

    df = _completed_ohlc(df, interval_minutes)
    if len(df) < 60:
        return None

    return {
        "rank": base.get("rank"),
        "symbol": symbol,
        "name": base.get("name"),
        "pair": resolved,
        "exchange": src.name,
        "bar_close_utc": _bar_close_utc(df, interval_minutes),
        "df": df,
    }


def _load_strategy():
    all_strategies = strat_registry.load_strategies()
    assigned_name = strat_registry.get_assignment(CRYPTO.key)
    strategy = all_strategies.get(assigned_name)
    if strategy is None:
        strategy = all_strategies[strat_registry.DEFAULT_STRATEGY_NAME]
        assigned_name = strategy.name
    logic_label = strat_registry.LOGICS[strategy.logic_key].label
    return strategy, assigned_name, logic_label


def _load_hl_perp_coins(attempts: int = 3) -> set[str]:
    """Return HyperLiquid perpetual coin names, preserving exchange casing.

    Retries on transient failures. Returns an empty set only if every attempt
    failed; callers MUST treat an empty result as fatal (see build_payload) —
    writing a file where nothing is tradable would make the downstream routine
    read it as "close every position".
    """
    last_err: Exception | None = None
    for i in range(attempts):
        try:
            resp = requests.post(HL_INFO_URL, json={"type": "meta"}, timeout=15)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as e:  # noqa: BLE001 - transient network/HTTP/JSON
            last_err = e
            print(f"  [warn] HyperLiquid meta fetch failed (attempt {i + 1}/{attempts}): {e}", file=sys.stderr)
            continue

        coins: set[str] = set()
        for item in payload.get("universe", []):
            coin = item.get("name") or item.get("coin")
            if coin:
                coins.add(str(coin))
        if coins:
            return coins
        print(f"  [warn] HyperLiquid meta returned an empty universe (attempt {i + 1}/{attempts})", file=sys.stderr)

    if last_err is not None:
        print(f"  [error] HyperLiquid meta fetch exhausted retries: {last_err}", file=sys.stderr)
    return set()


def _hl_symbol(symbol: str, hl_coins: set[str]) -> str | None:
    """Map a spot symbol to a HyperLiquid perp coin when one is listed."""
    by_upper = {coin.upper(): coin for coin in hl_coins}
    sym = symbol.upper()
    for candidate in (sym, f"K{sym}"):
        if candidate in by_upper:
            return by_upper[candidate]
    return None


def _gc_short_state(df: pd.DataFrame, channel: pd.DataFrame, params: dict) -> pd.Series:
    """SHORT-side replay: a faithful mirror of GC v3.1's long rules.

    Long  (gaussian_channel.replay_strategy): enter when the filter is RISING and
    close is ABOVE the upper band and stoch K is in extremes; exit on close
    crossing UNDER the upper band.
    Short (here): enter when the filter is FALLING and close is BELOW the lower
    band and stoch K is in extremes; exit on close crossing OVER the lower band.

    Returns a 0/1 Series (1 = currently short) aligned to df.index. NaNs during
    the channel warm-up make every comparison False, so no spurious entries.
    """
    close = df["close"].to_numpy(dtype=float)
    filt = channel["filt"].to_numpy(dtype=float)
    lband = channel["lband"].to_numpy(dtype=float)
    k = _gc_stoch(
        close,
        int(params["rsi_length"]),
        int(params["stoch_length"]),
        int(params["smooth_k"]),
    )
    n = len(close)
    state = np.zeros(n, dtype=np.int8)
    in_pos = False
    for t in range(1, n):
        if in_pos:
            if close[t] > lband[t] and close[t - 1] <= lband[t - 1]:  # crossover up
                in_pos = False
        else:
            gaussian_red = filt[t] < filt[t - 1]
            kt = k[t]
            stoch_ok = not np.isnan(kt) and (kt > 80 or kt < 20)
            if gaussian_red and close[t] < lband[t] and stoch_ok:
                in_pos = True
        state[t] = 1 if in_pos else 0
    return pd.Series(state, index=df.index, name="short")


def _current_breakout_date(state_series: pd.Series) -> str | None:
    state = state_series.fillna(0).astype("int8")
    if state.empty or int(state.iloc[-1]) != 1:
        return None
    entries = state[(state == 1) & (state.shift(1).fillna(0).astype("int8") == 0)]
    if entries.empty:
        return None
    ts = pd.Timestamp(entries.index[-1])
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return ts.date().isoformat()


def build_payload(interval_minutes: int = 1440, max_workers: int = 20) -> dict:
    strategy, assigned_name, logic_label = _load_strategy()
    print(f"Strategy: {logic_label} (preset: {assigned_name})")

    resolver = CRYPTO.resolver_factory()
    print(f"Sources: {resolver.coverage()}")
    print(f"Universe: {len(CRYPTO.universe)} symbols")

    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {
            ex.submit(_fetch_one, coin, resolver, interval_minutes): coin
            for coin in CRYPTO.universe
        }
        for fut in as_completed(futures):
            row = fut.result()
            if row is not None:
                rows.append(row)

    if not rows:
        raise RuntimeError("No crypto OHLC rows were fetched")

    target_bar_close = max(row["bar_close_utc"] for row in rows)
    rows = [row for row in rows if row["bar_close_utc"] == target_bar_close]
    rows.sort(key=lambda r: (r["rank"] is None, r["rank"] or 999999, r["symbol"]))
    print(f"Fetched: {len(rows)} symbols at bar close {_iso_z(target_bar_close)}")

    hl_coins = _load_hl_perp_coins()
    if not hl_coins:
        raise RuntimeError(
            "HyperLiquid perp universe came back empty after retries — refusing to "
            "write an all-untradable signals file (downstream would read it as "
            "'close every position'). Leaving the previous file in place."
        )
    is_gc = strategy.logic_key == GC_LOGIC_KEY
    gc_params = strat_registry.LOGICS[GC_LOGIC_KEY].coerce(strategy.params) if is_gc else None
    signals: list[dict] = []
    for row in rows:
        result = strat_registry.run_strategy(strategy, row["df"])
        snap = result.snapshot
        hl_symbol = _hl_symbol(row["symbol"], hl_coins)

        long_on = bool(snap.in_position)
        short_series = None
        short_on = False
        # A bar can't be above the upper band and below the lower band at once,
        # so the short side is only evaluated when the long side is flat.
        if is_gc and not long_on and result.overlays is not None:
            short_series = _gc_short_state(row["df"], result.overlays, gc_params)
            short_on = bool(int(short_series.iloc[-1]))

        state = "long" if long_on else "short" if short_on else "neutral"

        signal = {
            "symbol": row["symbol"],
            "rank": row["rank"],
            "state": state,
            "hl_symbol": hl_symbol,
            "tradable_on_hl": hl_symbol is not None,
            "close": snap.last_close,
        }
        if is_gc:
            signal["gc_filter"] = snap.last_filt
            signal["gc_upper"] = snap.last_hband
            signal["gc_lower"] = snap.last_lband

        active_state_series = (
            result.state_series if long_on else short_series if short_on else None
        )
        if active_state_series is not None:
            breakout_date = _current_breakout_date(active_state_series)
            if breakout_date is not None:
                signal["breakout_date"] = breakout_date
        signals.append(signal)

    return {
        "generated_at": _iso_z(pd.Timestamp.now(tz="UTC")),
        "bar_close_utc": _iso_z(target_bar_close),
        "timeframe": _timeframe_label(interval_minutes),
        "strategy": logic_label,
        "asset_class": "crypto",
        "signals": signals,
    }


def write_payload(payload: dict, output_path: Path = OUTPUT_PATH) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    interval_minutes = int(_env("TR_INTERVAL_MINUTES", "1440"))
    max_workers = int(_env("TR_MAX_WORKERS", "20"))
    output_path = Path(_env("TR_SIGNALS_OUTPUT", str(OUTPUT_PATH)))

    print("== Trend Radar crypto signals export ==")
    print(f"Timeframe: {_timeframe_label(interval_minutes)}")
    payload = build_payload(interval_minutes=interval_minutes, max_workers=max_workers)
    write_payload(payload, output_path)
    print(f"Wrote {output_path} ({len(payload['signals'])} signals)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
