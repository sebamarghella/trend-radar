"""Headless alert engine — for cron / GitHub Actions / any scheduler.

Same fetch → replay → flip-detect → Telegram pipeline as the Streamlit app, just
without the UI. Designed to run once per bar close (e.g., 00:05 UTC on daily TF).

Required env vars:
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID

Optional env vars (with defaults matching the Pine v3.1 strategy):
    TR_INTERVAL_MINUTES    default 1440 (daily). Try 240 for 4h.
    TR_GC_POLES            default 4
    TR_GC_PERIOD           default 144
    TR_GC_MULT             default 1.414
    TR_RSI_LEN             default 14
    TR_STOCH_LEN           default 14
    TR_SMOOTH_K            default 3
    TR_MAX_WORKERS         default 20
"""

from __future__ import annotations

import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import alerts
from binance_client import BinanceError, fetch_with_retry, resolve_symbol, tradable_symbols
from coins import candidate_symbols, tradable_universe
from gaussian_channel import GCParams, gaussian_channel, replay_strategy, stoch_rsi_k


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def fetch_one(base: str, available: set, interval_minutes: int) -> dict | None:
    cands = candidate_symbols(base)
    resolved = resolve_symbol(cands, available)
    if resolved is None:
        return None
    try:
        df = fetch_with_retry(resolved, interval_minutes=interval_minutes)
    except BinanceError as e:
        print(f"  [warn] {base} ({resolved}): {e}", file=sys.stderr)
        return None
    if len(df) < 60:
        return None
    return {"symbol": base, "pair": resolved, "df": df}


def main() -> int:
    bot_token = _env("TELEGRAM_BOT_TOKEN", "")
    chat_id = _env("TELEGRAM_CHAT_ID", "")
    if not bot_token or not chat_id:
        print("ERROR: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set", file=sys.stderr)
        return 2

    interval = int(_env("TR_INTERVAL_MINUTES", "1440"))
    gc_params = GCParams(
        poles=int(_env("TR_GC_POLES", "4")),
        period=int(_env("TR_GC_PERIOD", "144")),
        multiplier=float(_env("TR_GC_MULT", "1.414")),
    )
    rsi_len = int(_env("TR_RSI_LEN", "14"))
    stoch_len = int(_env("TR_STOCH_LEN", "14"))
    sm_k = int(_env("TR_SMOOTH_K", "3"))
    max_workers = int(_env("TR_MAX_WORKERS", "20"))

    print(
        f"== Trend Radar alerts ==\n"
        f"interval={interval}m  poles={gc_params.poles}  period={gc_params.period}  "
        f"mult={gc_params.multiplier}"
    )

    available = tradable_symbols()
    universe = tradable_universe()
    print(f"Binance: {len(available)} TRADING symbols · universe: {len(universe)} coins")

    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(fetch_one, c["symbol"], available, interval): c for c in universe}
        for fut in as_completed(futures):
            res = fut.result()
            if res is not None:
                rows.append(res)
    print(f"Fetched OHLC for {len(rows)}/{len(universe)} coins")

    signals: list[dict] = []
    for r in rows:
        df = r["df"]
        channel = gaussian_channel(df, gc_params)
        k = stoch_rsi_k(df["close"].to_numpy(), rsi_len, stoch_len, sm_k)
        snap, _state, _trades = replay_strategy(df, channel, k)
        signals.append({
            "symbol": r["symbol"],
            "pair": r["pair"],
            "state": "LONG" if snap.in_position else "FLAT",
            "last_close": snap.last_close,
            "stoch_k": snap.stoch_k,
            "filter_up": snap.filter_up,
            "close_vs_hband_pct": snap.close_vs_hband_pct,
        })

    long_count = sum(1 for s in signals if s["state"] == "LONG")
    print(f"State: {long_count} LONG · {len(signals) - long_count} FLAT")

    prev_state = alerts.load_state()
    was_first_run = not prev_state
    flips, new_state = alerts.detect_flips(signals, interval, prev_state)
    alerts.save_state(new_state)

    if was_first_run:
        print(
            f"Seeded alert state for {len(new_state)} coins (first run; no alerts sent)."
        )
        return 0

    if not flips:
        print("No state flips this run.")
        return 0

    print(f"Detected {len(flips)} flip(s):")
    for f in flips:
        print(f"  {f.symbol} {f.direction}  @ {f.price:.6g}")

    sent, errs = alerts.fire_alerts(flips, bot_token, chat_id)
    print(f"Sent {sent} alert(s); {len(errs)} error(s)")
    for e in errs:
        print(f"  [error] {e}", file=sys.stderr)

    return 0 if not errs else 1


if __name__ == "__main__":
    sys.exit(main())
