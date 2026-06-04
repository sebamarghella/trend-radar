"""Telegram alerts on strategy state flips.

State is persisted in `.cache/alerts_state.json` keyed by `(symbol, interval)`.
On each refresh, we diff the saved state vs. the current state per coin and fire
a Telegram message only on FLAT→LONG or LONG→FLAT transitions. First-ever run
seeds the state file silently — no spam.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import requests

STATE_FILE = Path(__file__).parent / ".cache" / "alerts_state.json"
STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

TELEGRAM_API = "https://api.telegram.org"


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

    def format(self) -> str:
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
    try:
        return json.loads(STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(state: dict[str, str]) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2))
    except OSError:
        pass


def _key(asset_class: str, symbol: str, interval_minutes: int) -> str:
    return f"{asset_class}|{symbol}|{interval_minutes}"


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
        # We have a flip — record it.
        direction = "ENTRY" if current == "LONG" else "EXIT"
        flips.append(Flip(
            symbol=s["symbol"],
            pair=s["pair"],
            direction=direction,
            price=s["last_close"],
            stoch_k=s.get("stoch_k"),
            filter_up=bool(s.get("filter_up", False)),
            close_vs_hband_pct=float(s.get("close_vs_hband_pct", 0.0)),
            interval_minutes=interval_minutes,
            exchange=s.get("exchange", ""),
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
) -> tuple[int, list[str]]:
    """Send one Telegram message per flip. Returns (sent_count, errors)."""
    sent = 0
    errors: list[str] = []
    for flip in flips:
        ok, err = send_telegram(bot_token, chat_id, flip.format())
        if ok:
            sent += 1
        else:
            errors.append(f"{flip.symbol}: {err}")
    return sent, errors
