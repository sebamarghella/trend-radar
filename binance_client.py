"""Minimal Binance public REST client (exchangeInfo + klines)."""

from __future__ import annotations

import time
from typing import Iterable

import pandas as pd
import requests

# api.binance.com is the primary endpoint; data-api.binance.vision is a
# CDN-fronted public mirror used as a fallback when the primary 451s
# (geo-blocked regions).
PRIMARY_URL = "https://api.binance.com/api/v3"
FALLBACK_URL = "https://data-api.binance.vision/api/v3"
DEFAULT_TIMEOUT = 15

# Our internal unit is minutes; Binance expects a string code.
INTERVAL_MAP: dict[int, str] = {
    1: "1m", 5: "5m", 15: "15m", 30: "30m",
    60: "1h", 120: "2h", 240: "4h", 360: "6h", 720: "12h",
    1440: "1d", 4320: "3d", 10080: "1w",
}


class BinanceError(RuntimeError):
    pass


def _get(path: str, params: dict | None = None) -> dict | list:
    last_err: Exception | None = None
    for base in (PRIMARY_URL, FALLBACK_URL):
        try:
            r = requests.get(f"{base}/{path}", params=params, timeout=DEFAULT_TIMEOUT)
            if r.status_code == 451:
                last_err = BinanceError(f"{base} geo-blocked (HTTP 451)")
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            last_err = e
            continue
    raise BinanceError(f"Both Binance endpoints failed: {last_err}")


def tradable_symbols() -> set[str]:
    """Set of TRADING spot symbols on Binance.

    Binance moved spot indication out of the flat `permissions` array and into
    `isSpotTradingAllowed` (bool) + the nested `permissionSets`. Accept either.
    """
    info = _get("exchangeInfo")
    out: set[str] = set()
    for s in info["symbols"]:
        if s.get("status") != "TRADING":
            continue
        if s.get("isSpotTradingAllowed") is True:
            out.add(s["symbol"])
            continue
        flat = s.get("permissions") or []
        nested = [p for grp in s.get("permissionSets") or [] for p in grp]
        if "SPOT" in flat or "SPOT" in nested:
            out.add(s["symbol"])
    return out


def resolve_symbol(candidates: Iterable[str], available: set[str]) -> str | None:
    for c in candidates:
        if c in available:
            return c
    return None


def klines(symbol: str, interval_minutes: int = 240, limit: int = 1000) -> pd.DataFrame:
    """Fetch up to 1000 candles ending at the current time."""
    interval = INTERVAL_MAP.get(interval_minutes)
    if interval is None:
        raise BinanceError(f"Unsupported interval {interval_minutes} minutes")
    data = _get("klines", {"symbol": symbol, "interval": interval, "limit": limit})
    if not data:
        raise BinanceError(f"No data returned for {symbol}")
    df = pd.DataFrame(
        data,
        columns=[
            "openTime", "open", "high", "low", "close", "volume",
            "closeTime", "qav", "trades", "tbbav", "tbqav", "ignore",
        ],
    )
    df["ts"] = pd.to_datetime(df["openTime"].astype("int64"), unit="ms", utc=True)
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col])
    return df.set_index("ts")[["open", "high", "low", "close", "volume"]].sort_index()


def fetch_with_retry(symbol: str, interval_minutes: int = 240, attempts: int = 3) -> pd.DataFrame:
    last_err: Exception | None = None
    for i in range(attempts):
        try:
            return klines(symbol, interval_minutes)
        except (requests.RequestException, BinanceError) as e:
            last_err = e
            time.sleep(0.3 * (i + 1))
    raise BinanceError(f"Failed to fetch {symbol}: {last_err}")
