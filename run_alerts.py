"""Weekday Stocks Telegram alerts, sent shortly after the US market opens.

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
    """Both routine and manual scans require the next-open fill to exist."""
    return now_ny.weekday() < 5 and now_ny.time() >= time(9, 45)


def seed_prior_filled_states(signals: list[dict], previous: dict[str, str],
                             class_key: str, interval: int) -> dict[str, str]:
    """For an explicit first-run dispatch, alert today's fills only."""
    seeded = dict(previous)
    for signal in signals:
        if signal["alertable"]:
            key = f"{class_key}|{signal['symbol']}|{interval}"
            seeded[key] = "FLAT" if signal["state"] == "LONG" else "LONG"
    return seeded


def include_open_positions(universe: list[dict],
                           open_positions: dict[str, dict]) -> tuple[list[dict], set[str]]:
    """Keep scanning delivered OPEN alerts after they leave the ranked universe."""
    held = set(open_positions)
    symbols = {item["symbol"] for item in universe}
    extra = sorted(held - symbols)
    return [*universe, *({"symbol": symbol} for symbol in extra)], held


def tracked_exit(result, df, position: dict) -> tuple[float, float, str] | None:
    """Find the close fill for the exact trade whose OPEN alert was delivered."""
    entry_date = position["entry_date"]
    for trade in perf.filled_trades(result.trades, df):
        if trade.entry_ts.date().isoformat() != entry_date:
            continue
        if trade.exit_ts is not None and trade.exit_price is not None:
            return float(trade.entry_price), float(trade.exit_price), trade.exit_ts.date().isoformat()
        return None
    return None


def scan_class(
    ac: AssetClass,
    max_workers: int,
    prev_state: dict[str, str],
    bot_token: str,
    chat_id: str,
    today_ny: date,
    open_positions: dict[str, dict],
    send_todays_fills: bool = False,
) -> tuple[dict[str, str], dict[str, dict], list[str]]:
    """Scan one asset class, maintain alerted trades, and report failures."""
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
    ranked_universe = ac.get_universe(resolver)
    universe, held_symbols = include_open_positions(ranked_universe, open_positions)
    print(f"  Universe: {len(ranked_universe)} ranked + "
          f"{len(universe) - len(ranked_universe)} previously open = {len(universe)} symbols")

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

    fetched = {r["symbol"] for r in rows}
    issues = []
    missing_held = sorted(held_symbols - fetched)
    if missing_held:
        issues.append(f"Could not fetch {len(missing_held)} open Stocks position(s): "
                      + ", ".join(missing_held))
    # Holidays have no new bars for any symbol. If other stocks have today's
    # bar, an open position without one cannot be checked for its exit fill.
    if any(r["df"].index[-1].date() == today_ny for r in rows):
        stale_held = sorted(
            r["symbol"] for r in rows
            if r["symbol"] in held_symbols and r["df"].index[-1].date() != today_ny
        )
        if stale_held:
            issues.append(f"No current trading bar for {len(stale_held)} open "
                          f"Stocks position(s): " + ", ".join(stale_held))
    for issue in issues:
        print(f"  [error] {issue}", file=sys.stderr)

    signals: list[dict] = []
    live_items: list[tuple] = []
    for r in rows:
        df = r["df"]
        current_result = strat_registry.run_strategy(strategy, df)
        live_items.append((r["symbol"], df, current_result))
        result = current_result
        if r["symbol"] in open_positions and "strategy" in open_positions[r["symbol"]]:
            try:
                opened_strategy = strat_registry.parse_strategy_dict(
                    open_positions[r["symbol"]]["strategy"]
                )
                if opened_strategy.to_dict() != strategy.to_dict():
                    result = strat_registry.run_strategy(opened_strategy, df)
            except (KeyError, TypeError, ValueError) as exc:
                issue = f"Could not replay tracked Stocks strategy for {r['symbol']}: {exc}"
                issues.append(issue)
                print(f"  [error] {issue}", file=sys.stderr)
                continue
        snap = result.snapshot
        state, entry_price, exit_price, filled_today = stock_fill(result, df)
        bar_today = df.index[-1].date() == today_ny
        fill_date = df.index[-1].date().isoformat() if filled_today else None
        alertable = filled_today and bar_today
        if r["symbol"] in open_positions:
            closed = tracked_exit(result, df, open_positions[r["symbol"]])
            if closed is not None:
                if state == "LONG":
                    issue = (f"{r['symbol']} closed its alerted trade and later re-entered "
                             "before this scan; review the newer entry")
                    issues.append(issue)
                    print(f"  [error] {issue}", file=sys.stderr)
                state = "FLAT"  # deliver the tracked close before any newer trade
                entry_price, exit_price, fill_date = closed
                alertable = True  # deliver a late close even after a missed run
            elif state == "FLAT":
                issue = f"Could not match a closing fill for tracked Stocks position: {r['symbol']}"
                issues.append(issue)
                print(f"  [error] {issue}", file=sys.stderr)
        signals.append({
            "symbol": r["symbol"],
            "pair": r["pair"],
            "exchange": r["exchange"],
            "state": state,
            "last_close": snap.last_close,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "alertable": alertable,
            "fill_date": fill_date,
            "late": bool(fill_date and fill_date != today_ny.isoformat()),
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
    if send_todays_fills and not any(k.startswith(f"{ac.key}|") for k in prev_state):
        prev_state = seed_prior_filled_states(signals, prev_state, ac.key, interval)
    class_prefix = f"{ac.key}|"
    interval_suffix = f"|{interval}"
    had_baseline = bool(open_positions) or any(
        k.startswith(class_prefix) and k.endswith(interval_suffix) for k in prev_state
    )
    # The delivered-alert ledger is authoritative. Historic model LONGs were
    # seeded before alerting began and must not generate unpaired CLOSE alerts.
    for symbol in open_positions:
        prev_state[f"{ac.key}|{symbol}|{interval}"] = "LONG"
    for signal in signals:
        if signal["state"] == "LONG" and signal["alertable"] and signal["symbol"] not in open_positions:
            prev_state[f"{ac.key}|{signal['symbol']}|{interval}"] = "FLAT"
    flips, new_state = alerts.detect_flips(signals, interval, prev_state, asset_class=ac.key)

    if not ac.alerts_enabled:
        # Keep the baseline current so re-enabling later doesn't fire a burst of stale flips.
        print(f"  Alerts are turned off for {ac.label}: {len(flips)} flip(s) not sent or recorded; baseline updated silently.")
        return new_state, open_positions, issues

    if not had_baseline:
        print(f"  Seeded baseline ({len(signals)} symbols); no alerts sent.")
        return new_state, open_positions, issues

    flips = [f for f in flips
             if (f.direction == "ENTRY" and f.symbol not in open_positions)
             or (f.direction == "EXIT" and f.symbol in open_positions)]
    if not flips:
        print("  No flips.")
        return new_state, open_positions, issues

    print(f"  Detected {len(flips)} flip(s):")
    for f in flips:
        print(f"    {f.symbol} {f.direction} @ {f.price:.6g}")
    sent, errs = alerts.fire_alerts(flips, bot_token, chat_id)
    print(f"  Sent {len(sent)} alert(s); {len(errs)} error(s)")
    delivered = {id(f) for f in sent}
    for f in flips:
        key = f"{ac.key}|{f.symbol}|{interval}"
        if id(f) not in delivered:
            new_state[key] = prev_state[key]  # allow same-day manual retry
        elif f.direction == "ENTRY":
            open_positions[f.symbol] = {
                "entry_date": f.fill_date,
                "entry_price": f.entry_price,
                "strategy": strategy.to_dict(),
            }
        else:
            open_positions.pop(f.symbol)
    if sent:
        alerts.record_flips(sent, asset_class=ac.key)
    for e in errs:
        print(f"    [error] {e}", file=sys.stderr)
    issues.extend(errs)

    return new_state, open_positions, issues


def main() -> int:
    now_ny = datetime.now(ZoneInfo("America/New_York"))
    send_todays_fills = _env("TR_SEND_TODAYS_FILLS", "").lower() in {"true", "1", "yes"}
    if not should_scan(now_ny):
        print("No alert scan: Stocks fills are checked Monday-Friday after 09:45 New York time.")
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
    open_positions = alerts.load_open_positions()
    state, open_positions, issues = scan_class(
        STOCKS, max_workers, state, bot_token, chat_id,
        now_ny.date(), open_positions, send_todays_fills,
    )
    alerts.save_state(state)
    alerts.save_open_positions(open_positions)

    return 1 if issues else 0


if __name__ == "__main__":
    sys.exit(main())
