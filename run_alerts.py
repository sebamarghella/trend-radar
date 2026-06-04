"""Headless alert engine — for cron / GitHub Actions / any scheduler.

Scans all asset classes (Crypto / Stocks / Metals / Commodities) in one run.
For each class, fetches → replays strategy → diffs state vs the saved baseline
→ sends a Telegram message per FLAT↔LONG flip.

Required env vars:
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID

Optional:
    TR_ASSET_CLASSES       comma-separated keys to scan; default: all
                           (e.g. "crypto,stocks")
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
from asset_classes import ASSET_CLASSES, AssetClass
from gaussian_channel import GCParams, gaussian_channel, replay_strategy, stoch_rsi_k
from sources import Resolver, SourceError


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def fetch_one(base: str, resolver: Resolver, interval_minutes: int) -> dict | None:
    hit = resolver.resolve(base)
    if hit is None:
        return None
    src, resolved = hit
    try:
        df = src.fetch_with_retry(resolved, interval_minutes=interval_minutes)
    except SourceError as e:
        print(f"  [warn] {base} ({src.name}:{resolved}): {e}", file=sys.stderr)
        return None
    if len(df) < 60:
        return None
    return {"symbol": base, "pair": resolved, "exchange": src.name, "df": df}


def scan_class(
    ac: AssetClass,
    gc_params: GCParams,
    rsi_len: int,
    stoch_len: int,
    sm_k: int,
    max_workers: int,
    prev_state: dict[str, str],
    bot_token: str,
    chat_id: str,
) -> dict[str, str]:
    """Scan one asset class, send alerts, return the updated state map."""
    interval = ac.interval_options[ac.default_interval_idx][1]
    print(f"\n-- {ac.label} (TF={interval}m) --")

    resolver = ac.resolver_factory()
    coverage = resolver.coverage()
    cov_str = " · ".join(f"{k}={v}" for k, v in coverage.items())
    print(f"  Sources: {cov_str}")
    print(f"  Universe: {len(ac.universe)} symbols")

    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(fetch_one, c["symbol"], resolver, interval): c for c in ac.universe}
        for fut in as_completed(futures):
            res = fut.result()
            if res is not None:
                rows.append(res)
    per_exchange: dict[str, int] = {}
    for r in rows:
        per_exchange[r["exchange"]] = per_exchange.get(r["exchange"], 0) + 1
    breakdown = ", ".join(f"{k}={v}" for k, v in sorted(per_exchange.items()))
    print(f"  Fetched: {len(rows)}/{len(ac.universe)} ({breakdown})")

    signals: list[dict] = []
    for r in rows:
        df = r["df"]
        channel = gaussian_channel(df, gc_params)
        k = stoch_rsi_k(df["close"].to_numpy(), rsi_len, stoch_len, sm_k)
        snap, _state, _trades = replay_strategy(df, channel, k)
        signals.append({
            "symbol": r["symbol"],
            "pair": r["pair"],
            "exchange": r["exchange"],
            "state": "LONG" if snap.in_position else "FLAT",
            "last_close": snap.last_close,
            "stoch_k": snap.stoch_k,
            "filter_up": snap.filter_up,
            "close_vs_hband_pct": snap.close_vs_hband_pct,
        })

    long_count = sum(1 for s in signals if s["state"] == "LONG")
    print(f"  State: {long_count} LONG / {len(signals) - long_count} FLAT")

    class_prefix = f"{ac.key}|"
    interval_suffix = f"|{interval}"
    had_baseline = any(
        k.startswith(class_prefix) and k.endswith(interval_suffix) for k in prev_state
    )
    flips, new_state = alerts.detect_flips(signals, interval, prev_state, asset_class=ac.key)

    if not had_baseline:
        print(f"  Seeded baseline ({len(signals)} symbols); no alerts sent.")
        return new_state

    if not flips:
        print("  No flips.")
        return new_state

    print(f"  Detected {len(flips)} flip(s):")
    for f in flips:
        print(f"    {f.symbol} {f.direction} @ {f.price:.6g}")

    sent, errs = alerts.fire_alerts(flips, bot_token, chat_id)
    print(f"  Sent {sent} alert(s); {len(errs)} error(s)")
    for e in errs:
        print(f"    [error] {e}", file=sys.stderr)

    return new_state


def main() -> int:
    bot_token = _env("TELEGRAM_BOT_TOKEN", "")
    chat_id = _env("TELEGRAM_CHAT_ID", "")
    if not bot_token or not chat_id:
        print("ERROR: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set", file=sys.stderr)
        return 2

    gc_params = GCParams(
        poles=int(_env("TR_GC_POLES", "4")),
        period=int(_env("TR_GC_PERIOD", "144")),
        multiplier=float(_env("TR_GC_MULT", "1.414")),
    )
    rsi_len = int(_env("TR_RSI_LEN", "14"))
    stoch_len = int(_env("TR_STOCH_LEN", "14"))
    sm_k = int(_env("TR_SMOOTH_K", "3"))
    max_workers = int(_env("TR_MAX_WORKERS", "20"))

    requested = _env("TR_ASSET_CLASSES", "").strip()
    if requested:
        keys = {k.strip() for k in requested.split(",")}
        classes = [ac for ac in ASSET_CLASSES if ac.key in keys]
        if not classes:
            print(f"No asset classes match TR_ASSET_CLASSES={requested}", file=sys.stderr)
            return 2
    else:
        classes = list(ASSET_CLASSES)

    print(f"== Trend Radar alerts ==")
    print(f"poles={gc_params.poles} period={gc_params.period} mult={gc_params.multiplier}")
    print(f"asset classes: {[ac.label for ac in classes]}")

    state = alerts.load_state()
    for ac in classes:
        state = scan_class(ac, gc_params, rsi_len, stoch_len, sm_k, max_workers,
                           state, bot_token, chat_id)
        alerts.save_state(state)

    return 0


if __name__ == "__main__":
    sys.exit(main())
