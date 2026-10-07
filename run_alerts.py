"""Weekday equity Telegram alerts from confirmed daily closing signals.

Replays the assigned daily Stocks strategy after the New York close, compares
the confirmed signal against the saved baseline, and sends action plans for
the next regular-session open.

Required env vars (supplied by the relevant GitHub Actions workflow):
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID

`TR_ALERT_CLASS` selects `stocks` (the default) or `low_float`. Each class
uses an independent bot, state/history files, and open-position ledger.

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
from asset_classes import LOW_FLOAT, STOCKS, AssetClass
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


def confirmed_daily_signal(result, df) -> tuple[str, float | None, bool]:
    """Return the latest completed daily signal and whether it just flipped.

    An after-close alert is actionable at the following regular-session open,
    so it intentionally uses the newest closed candle instead of waiting for
    the model's simulated next-open fill price.
    """
    state = result.state_series
    if len(state) < 2 or len(df) < 2:
        return "FLAT", None, False
    position = bool(state.iloc[-1])
    prior_position = bool(state.iloc[-2])
    return "LONG" if position else "FLAT", float(df["close"].iloc[-1]), position != prior_position


def should_scan(now_ny: datetime) -> bool:
    """Both routine and manual scans require the regular session to have closed."""
    return now_ny.weekday() < 5 and now_ny.time() >= time(16, 15)


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


def missed_exit_signal(result, df, position: dict) -> tuple[float, str] | None:
    """Recover an unreported FlipRed for a delivered FlipGreen position."""
    entry_date = str(position.get("entry_date") or "")
    state = result.state_series
    for i in range(1, len(state)):
        if not bool(state.iloc[i - 1]) or bool(state.iloc[i]):
            continue
        signal_date = df.index[i].date().isoformat()
        if entry_date and signal_date < entry_date:
            continue
        return float(df["close"].iloc[i]), signal_date
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
    recover_sent_symbols: set[str] | None = None,
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
        issues.append(f"Could not fetch {len(missing_held)} open {ac.label} position(s): "
                      + ", ".join(missing_held))
    # Holidays have no new bars for any symbol. If other symbols have today's
    # bar, an open position without one cannot be checked for its exit fill.
    if any(r["df"].index[-1].date() == today_ny for r in rows):
        stale_held = sorted(
            r["symbol"] for r in rows
            if r["symbol"] in held_symbols and r["df"].index[-1].date() != today_ny
        )
        if stale_held:
            issues.append(f"No current trading bar for {len(stale_held)} open "
                          f"{ac.label} position(s): " + ", ".join(stale_held))
    for issue in issues:
        print(f"  [error] {issue}", file=sys.stderr)

    signals: list[dict] = []
    live_items: list[tuple] = []
    for r in rows:
        df = r["df"]
        current_result = strat_registry.run_strategy(strategy, df)
        live_items.append((r["symbol"], df, current_result))
        result = current_result
        signal_strategy_name = assigned_name
        if r["symbol"] in open_positions and "strategy" in open_positions[r["symbol"]]:
            try:
                opened_strategy = strat_registry.parse_strategy_dict(
                    open_positions[r["symbol"]]["strategy"]
                )
                signal_strategy_name = opened_strategy.name
                if opened_strategy.to_dict() != strategy.to_dict():
                    result = strat_registry.run_strategy(opened_strategy, df)
            except (KeyError, TypeError, ValueError) as exc:
                issue = f"Could not replay tracked {ac.label} strategy for {r['symbol']}: {exc}"
                issues.append(issue)
                print(f"  [error] {issue}", file=sys.stderr)
                continue
        snap = result.snapshot
        state, signal_close, flipped_today = confirmed_daily_signal(result, df)
        bar_today = df.index[-1].date() == today_ny
        signal_date = df.index[-1].date().isoformat() if flipped_today else None
        alertable = flipped_today and bar_today
        prior_alert = open_positions.get(r["symbol"], {})
        entry_price = signal_close if state == "LONG" else prior_alert.get("entry_price")
        exit_price = signal_close if state == "FLAT" else None
        if state == "FLAT" and prior_alert and not alertable:
            missed = missed_exit_signal(result, df, prior_alert)
            if missed is not None:
                exit_price, signal_date = missed
                alertable = True
        signals.append({
            "symbol": r["symbol"],
            "pair": r["pair"],
            "exchange": r["exchange"],
            "state": state,
            "last_close": snap.last_close,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "alertable": alertable,
            "fill_date": signal_date,
            "late": bool(signal_date and signal_date != today_ny.isoformat()),
            "strategy_name": signal_strategy_name,
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
        prev_state, ac.key, f"{assigned_name}|confirmed-close-next-open-v2",
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
    recovered_symbols = recover_sent_symbols or set()
    recovered = [f for f in flips if f.symbol.upper() in recovered_symbols]
    outbound = [f for f in flips if f.symbol.upper() not in recovered_symbols]
    sent, errs = alerts.fire_alerts(outbound, bot_token, chat_id)
    sent = [*recovered, *sent]
    if recovered:
        print(f"  Recovered {len(recovered)} already-delivered alert(s) without re-sending.")
    print(f"  Sent {len(outbound) - len(errs)} alert(s); {len(errs)} error(s)")
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
    recover_sent_symbols = {
        symbol.strip().upper()
        for symbol in _env("TR_RECOVER_SENT_SYMBOLS", "").split(",")
        if symbol.strip()
    }
    asset_key = _env("TR_ALERT_CLASS", "stocks").strip().lower()
    alert_classes = {"stocks": STOCKS, "low_float": LOW_FLOAT}
    ac = alert_classes.get(asset_key)
    if ac is None:
        print("ERROR: TR_ALERT_CLASS must be 'stocks' or 'low_float'", file=sys.stderr)
        return 2
    if not should_scan(now_ny):
        print(f"No alert scan: {ac.label} closing signals are checked Monday-Friday after 16:15 New York time.")
        return 0

    bot_token = _env("TELEGRAM_BOT_TOKEN", "")
    chat_id = _env("TELEGRAM_CHAT_ID", "")
    if not bot_token or not chat_id:
        print("ERROR: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set", file=sys.stderr)
        return 2

    max_workers = int(_env("TR_MAX_WORKERS", "20"))

    print("== Trend Radar alerts ==")
    print(f"asset class: {ac.label}")
    print(f"strategy assignments: {strat_registry.load_assignments()}")

    state = alerts.load_state(ac.key)
    open_positions = alerts.load_open_positions(ac.key)
    state, open_positions, issues = scan_class(
        ac, max_workers, state, bot_token, chat_id,
        now_ny.date(), open_positions, send_todays_fills, recover_sent_symbols,
    )
    alerts.save_state(state, ac.key)
    alerts.save_open_positions(open_positions, ac.key)

    return 1 if issues else 0


if __name__ == "__main__":
    sys.exit(main())
