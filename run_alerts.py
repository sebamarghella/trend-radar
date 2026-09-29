"""Weekday Stocks Telegram alerts, sent after the US market close.

Replays the assigned daily Stocks strategy, compares filled positions against
the saved baseline, and sends OPEN/CLOSE messages for today's next-open fills.

Required env vars:
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID

The Stocks strategy and preset come from strategy_assignments.json and the
strategy registry. There are no per-param env vars.

Optional:
    TR_MAX_WORKERS         default 20
"""

from __future__ import annotations

import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import alerts
import live_log
import performance as perf
import strategies as strat_registry
from asset_classes import STOCKS, AssetClass
from sources import Resolver, SourceError, fetch_series


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def fetch_one(base: str, resolver: Resolver, interval_minutes: int, is_24_7: bool) -> dict | None:
    try:
        res = fetch_series(base, resolver, interval_minutes, is_24_7=is_24_7)
    except SourceError as e:
        print(f"  [warn] {base}: {e}", file=sys.stderr)
        return None
    if len(res.df) < 60:
        return None
    return {"symbol": base, "pair": res.pair, "exchange": res.source.name, "df": res.df}


def stock_fill(result, df) -> tuple[str, float | None, float | None, bool]:
    """Position and trade prices after the latest bar's open fill.

    The signal on the newest bar remains pending until the next trading bar.
    """
    state = result.state_series
    if len(state) < 3:
        return "FLAT", None, None, False
    position = bool(state.iloc[-2])
    prior_position = bool(state.iloc[-3])
    if position == prior_position:
        return ("LONG" if position else "FLAT"), None, None, False

    last_bar = df.index[-1]
    fills = perf.filled_trades(result.trades, df)
    if position:
        trade = next((t for t in fills if t.entry_ts == last_bar), None)
        return "LONG", float(trade.entry_price) if trade else None, None, trade is not None
    trade = next((t for t in fills if t.exit_ts == last_bar), None)
    if trade is None:
        return "FLAT", None, None, False
    return "FLAT", float(trade.entry_price), float(trade.exit_price), True


def should_scan(now_ny: datetime) -> bool:
    """Keep manual dispatches and delayed scheduled runs within US weekdays."""
    return now_ny.weekday() < 5 and now_ny.time() >= time(17, 30)


def scan_class(
    ac: AssetClass,
    max_workers: int,
    prev_state: dict[str, str],
    bot_token: str,
    chat_id: str,
    today_ny: date,
) -> dict[str, str]:
    """Scan one asset class with its assigned strategy, send alerts, return state."""
    interval = ac.interval_options[ac.default_interval_idx][1]

    all_strategies = strat_registry.load_strategies()
    assigned_name = strat_registry.get_assignment(ac.key)
    strategy = all_strategies.get(assigned_name)
    if strategy is None:
        strategy = all_strategies[strat_registry.DEFAULT_STRATEGY_NAME]
        assigned_name = strategy.name
    print(f"\n-- {ac.label} (TF={interval}m · strategy='{assigned_name}') --")

    resolver = ac.resolver_factory()
    coverage = resolver.coverage()
    cov_str = " · ".join(f"{k}={v}" for k, v in coverage.items())
    print(f"  Sources: {cov_str}")
    universe = ac.get_universe(resolver)
    print(f"  Universe: {len(universe)} symbols")

    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {
            ex.submit(fetch_one, c["symbol"], resolver, interval, ac.is_24_7): c
            for c in universe
        }
        for fut in as_completed(futures):
            res = fut.result()
            if res is not None:
                rows.append(res)
    per_exchange: dict[str, int] = {}
    for r in rows:
        per_exchange[r["exchange"]] = per_exchange.get(r["exchange"], 0) + 1
    breakdown = ", ".join(f"{k}={v}" for k, v in sorted(per_exchange.items()))
    print(f"  Fetched: {len(rows)}/{len(universe)} ({breakdown})")

    signals: list[dict] = []
    live_items: list[tuple] = []
    for r in rows:
        df = r["df"]
        result = strat_registry.run_strategy(strategy, df)
        live_items.append((r["symbol"], df, result))
        snap = result.snapshot
        state, entry_price, exit_price, filled_today = stock_fill(result, df)
        bar_today = df.index[-1].date() == today_ny
        signals.append({
            "symbol": r["symbol"],
            "pair": r["pair"],
            "exchange": r["exchange"],
            "state": state,
            "last_close": snap.last_close,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "alertable": filled_today and bar_today,
            "stoch_k": snap.stoch_k,
            "filter_up": snap.filter_up,
            "close_vs_hband_pct": snap.close_vs_hband_pct,
        })

    long_count = sum(1 for s in signals if s["state"] == "LONG")
    print(f"  State: {long_count} LONG / {len(signals) - long_count} FLAT")

    if ac.key in live_log.LIVE_CLASSES and interval == 1440:
        # Forward track record; must never block the alerts below.
        try:
            summary = live_log.update(
                ac.key, assigned_name, live_items,
                rerun=lambda d: strat_registry.run_strategy(strategy, d),
                position_fraction=(perf.GC_STOCKS_POSITION_FRACTION
                                   if strategy.logic_key == "gaussian_channel_stocks_v1" else 1.0),
                fill_mode=("next_open" if strategy.logic_key == "gaussian_channel_stocks_v1"
                           else "signal_close"),
            )
            print(f"  Live log: {summary}")
        except Exception as e:  # noqa: BLE001
            print(f"  [warn] live log failed: {e!r}", file=sys.stderr)

    prev_state = alerts.reseed_on_strategy_change(
        prev_state, ac.key, f"{assigned_name}|next-open-fills-v1",
        strat_registry.DEFAULT_STRATEGY_NAME,
    )
    class_prefix = f"{ac.key}|"
    interval_suffix = f"|{interval}"
    had_baseline = any(
        k.startswith(class_prefix) and k.endswith(interval_suffix) for k in prev_state
    )
    flips, new_state = alerts.detect_flips(signals, interval, prev_state, asset_class=ac.key)

    if not ac.alerts_enabled:
        # Keep the baseline current so re-enabling later doesn't fire a burst of stale flips.
        print(f"  Alerts are turned off for {ac.label}: {len(flips)} flip(s) not sent or recorded; baseline updated silently.")
        return new_state

    if not had_baseline:
        print(f"  Seeded baseline ({len(signals)} symbols); no alerts sent.")
        return new_state

    if not flips:
        print("  No flips.")
        return new_state

    print(f"  Detected {len(flips)} flip(s):")
    for f in flips:
        print(f"    {f.symbol} {f.direction} @ {f.price:.6g}")
    alerts.record_flips(flips, asset_class=ac.key)

    sent, errs = alerts.fire_alerts(flips, bot_token, chat_id)
    print(f"  Sent {sent} alert(s); {len(errs)} error(s)")
    for e in errs:
        print(f"    [error] {e}", file=sys.stderr)

    return new_state


def main() -> int:
    now_ny = datetime.now(ZoneInfo("America/New_York"))
    if not should_scan(now_ny):
        print("No alert scan: Stocks alerts run Monday-Friday after 17:30 New York time.")
        return 0

    bot_token = _env("TELEGRAM_BOT_TOKEN", "")
    chat_id = _env("TELEGRAM_CHAT_ID", "")
    if not bot_token or not chat_id:
        print("ERROR: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set", file=sys.stderr)
        return 2

    max_workers = int(_env("TR_MAX_WORKERS", "20"))

    print("== Trend Radar alerts ==")
    print("asset class: Stocks")
    print(f"strategy assignments: {strat_registry.load_assignments()}")

    state = alerts.load_state()
    state = scan_class(STOCKS, max_workers, state, bot_token, chat_id, now_ny.date())
    alerts.save_state(state)

    return 0


if __name__ == "__main__":
    sys.exit(main())
