"""Telegram alerts on strategy state flips.

State is persisted in `.cache/alerts_state.json` keyed by `(symbol, interval)`.
The scheduled Stocks job diffs filled positions and sends a message only on
FLAT→LONG or LONG→FLAT transitions. First-ever run seeds silently.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import requests

STATE_FILE = Path(__file__).parent / ".cache" / "alerts_state.json"
HISTORY_FILE = Path(__file__).parent / ".cache" / "alerts_history.json"
OPEN_POSITIONS_FILE = Path(__file__).parent / ".cache" / "stocks_alert_positions.json"
STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

TELEGRAM_API = "https://api.telegram.org"
# The Saturday report needs every delivered Stocks event from its start date.
# Keep the complete history; the dashboard may limit how many rows it displays.


@dataclass
class Flip:
    symbol: str
    pair: str
    direction: str  # "ENTRY" or "EXIT"
    price: float
    stoch_k: float | None
    filter_up: bool
    close_vs_hband_pct: float
    interval_minutes: int
    exchange: str = ""
    asset_class: str = "crypto"
    entry_price: float | None = None
    exit_price: float | None = None
    fill_date: str | None = None
    late: bool = False
    strategy_name: str | None = None

    def format(self) -> str:
        if self.asset_class == "stocks":
            opening = self.entry_price if self.entry_price is not None else self.price
            late = (f"\nFill date: {self.fill_date} (late notice)"
                    if self.late and self.fill_date else "")
            if self.direction == "ENTRY":
                return (f"🟢 FlipGreen · OPEN LONG · {self.symbol}\n"
                        f"Open price: ${opening:,.2f}\n"
                        f"Close price: pending{late}")
            closing = self.exit_price if self.exit_price is not None else self.price
            return (f"🔴 FlipRed · CLOSE LONG · {self.symbol}\n"
                    f"Open price: ${opening:,.2f}\n"
                    f"Close price: ${closing:,.2f}{late}")
        tf = _tf_label(self.interval_minutes)
        emoji = "🟢" if self.direction == "ENTRY" else "🔴"
        verb = "LONG" if self.direction == "ENTRY" else "EXIT"
        venue = f" on {self.exchange}" if self.exchange else ""
        lines = [
            f"{emoji} {verb}: {self.symbol} ({self.pair}){venue} — {tf}",
            f"Price: {self.price:.6g}",
            f"Filter: {'rising' if self.filter_up else 'falling'} · "
            f"close vs HBand {self.close_vs_hband_pct:+.2f}%",
        ]
        if self.stoch_k is not None:
            lines.append(f"Stoch K: {self.stoch_k:.1f}")
        return "\n".join(lines)


def _tf_label(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes}m"
    if minutes < 1440:
        return f"{minutes // 60}h"
    return f"{minutes // 1440}d"


def load_state() -> dict[str, str]:
    if not STATE_FILE.exists():
        return {}
    data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("alerts_state.json must contain an object")
    return data


def save_state(state: dict[str, str]) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def load_open_positions() -> dict[str, dict]:
    """Stocks whose OPEN LONG Telegram alert succeeded and has not closed."""
    if not OPEN_POSITIONS_FILE.exists():
        raise FileNotFoundError("stocks_alert_positions.json is required for Stocks alerts")
    data = json.loads(OPEN_POSITIONS_FILE.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("stocks_alert_positions.json must contain an object")
    return data


def save_open_positions(positions: dict[str, dict]) -> None:
    OPEN_POSITIONS_FILE.write_text(json.dumps(positions, indent=2) + "\n", encoding="utf-8")


def _key(asset_class: str, symbol: str, interval_minutes: int) -> str:
    return f"{asset_class}|{symbol}|{interval_minutes}"


def reseed_on_strategy_change(
    prev_state: dict[str, str], asset_class: str, strategy_name: str, legacy_name: str,
) -> dict[str, str]:
    """Drop a class's saved states when its assigned strategy changed, so the
    next diff seeds silently instead of alerting every symbol whose state merely
    differs between the two strategies. States saved before this marker existed
    are assumed to come from `legacy_name`."""
    marker = f"__strategy__|{asset_class}"  # never matches the "{class}|" prefix
    state = dict(prev_state)
    if state.get(marker, legacy_name) != strategy_name:
        state = {k: v for k, v in state.items() if not k.startswith(f"{asset_class}|")}
    state[marker] = strategy_name
    return state


def detect_flips(
    signals: Iterable[dict],
    interval_minutes: int,
    prev_state: dict[str, str],
    asset_class: str = "crypto",
) -> tuple[list[Flip], dict[str, str]]:
    """Diff current signal states against the saved state.

    Returns (flips_to_alert, new_state_to_persist). On the very first run
    (empty prev_state), no flips are emitted — we seed silently.
    """
    new_state: dict[str, str] = dict(prev_state)  # preserve other-class keys
    flips: list[Flip] = []
    # First-run detection at the asset-class level: did we have ANY prior keys
    # for this asset_class + interval combo?
    class_prefix = f"{asset_class}|"
    interval_suffix = f"|{interval_minutes}"
    class_keys_existed = any(
        k.startswith(class_prefix) and k.endswith(interval_suffix)
        for k in prev_state
    )
    for s in signals:
        k = _key(asset_class, s["symbol"], interval_minutes)
        current = s["state"]  # "LONG" or "FLAT"
        new_state[k] = current
        if not class_keys_existed:
            continue
        prev = prev_state.get(k)
        if prev is None or prev == current:
            continue
        if not s.get("alertable", True):
            # A missed run must not turn an older fill into a new alert.
            continue
        # We have a flip — record it.
        direction = "ENTRY" if current == "LONG" else "EXIT"
        if asset_class == "stocks" and (
            s.get("entry_price") is None
            or (direction == "EXIT" and s.get("exit_price") is None)
        ):
            continue  # Do not send a trade alert without its actual fill price.
        flips.append(Flip(
            symbol=s["symbol"],
            pair=s["pair"],
            direction=direction,
            price=(s.get("entry_price") if direction == "ENTRY" else s.get("exit_price"))
                  or s["last_close"],
            stoch_k=s.get("stoch_k"),
            filter_up=bool(s.get("filter_up", False)),
            close_vs_hband_pct=float(s.get("close_vs_hband_pct", 0.0)),
            interval_minutes=interval_minutes,
            exchange=s.get("exchange", ""),
            asset_class=asset_class,
            entry_price=s.get("entry_price"),
            exit_price=s.get("exit_price"),
            fill_date=s.get("fill_date"),
            late=bool(s.get("late", False)),
            strategy_name=s.get("strategy_name"),
        ))
    return flips, new_state


def send_telegram(bot_token: str, chat_id: str, text: str, timeout: int = 8) -> tuple[bool, str]:
    """Post a message. Returns (ok, error_or_empty)."""
    if not bot_token or not chat_id:
        return False, "missing token or chat_id"
    url = f"{TELEGRAM_API}/bot{bot_token}/sendMessage"
    try:
        r = requests.post(
            url,
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=timeout,
        )
        if r.status_code == 200 and r.json().get("ok"):
            return True, ""
        return False, f"HTTP {r.status_code}: {r.text[:200]}"
    except requests.RequestException as e:
        return False, str(e)


def fire_alerts(
    flips: list[Flip],
    bot_token: str,
    chat_id: str,
) -> tuple[list[Flip], list[str]]:
    """Send one Telegram message per flip. Return delivered flips and errors."""
    sent: list[Flip] = []
    errors: list[str] = []
    for flip in flips:
        ok, err = send_telegram(bot_token, chat_id, flip.format())
        if ok:
            sent.append(flip)
        else:
            errors.append(f"{flip.symbol}: {err}")
    return sent, errors


# --- Flip history (sidebar feed) ----------------------------------------------


def load_history() -> list[dict]:
    if not HISTORY_FILE.exists():
        return []
    try:
        data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def save_history(entries: list[dict]) -> None:
    HISTORY_FILE.write_text(json.dumps(entries, indent=2), encoding="utf-8")


def record_flips(flips: list[Flip], asset_class: str, ts_iso: str | None = None) -> list[dict]:
    """Append flips to the history log, newest written last. Returns the updated list."""
    if not flips:
        return load_history()
    from datetime import datetime, timezone
    now = ts_iso or datetime.now(timezone.utc).isoformat(timespec="seconds")
    history = load_history()
    for f in flips:
        history.append({
            "ts": now,
            "asset_class": asset_class,
            "symbol": f.symbol,
            "pair": f.pair,
            "direction": f.direction,   # ENTRY / EXIT
            "price": f.price,
            "fill_date": f.fill_date,
            "entry_price": f.entry_price,
            "exit_price": f.exit_price,
            "late": f.late,
            "strategy_name": f.strategy_name,
            "interval_minutes": f.interval_minutes,
            "exchange": f.exchange,
        })
    save_history(history)
    return history


def clear_history() -> None:
    try:
        HISTORY_FILE.unlink(missing_ok=True)
    except OSError:
        pass
