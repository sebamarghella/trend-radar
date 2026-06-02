"""On-disk OHLC cache keyed by (pair, interval).

Freshness rule: a cached frame is fresh until the next candle is expected to
open (last_ts + interval). After that, we refetch — but if Kraken errors out
we fall back to the stale copy rather than blanking the coin from the radar.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

CACHE_DIR = Path(__file__).parent / ".cache" / "ohlc"
CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _path(pair: str, interval_minutes: int) -> Path:
    safe_pair = pair.replace("/", "_").replace(":", "_")
    return CACHE_DIR / f"{safe_pair}__{interval_minutes}m.pkl"


def load(pair: str, interval_minutes: int) -> pd.DataFrame | None:
    p = _path(pair, interval_minutes)
    if not p.exists():
        return None
    try:
        return pd.read_pickle(p)
    except Exception:
        return None


def save(pair: str, interval_minutes: int, df: pd.DataFrame) -> None:
    try:
        df.to_pickle(_path(pair, interval_minutes))
    except Exception:
        # Cache failure should never break the app.
        pass


def is_fresh(df: pd.DataFrame, interval_minutes: int) -> bool:
    """Cache is fresh until the next candle is expected to start."""
    if df is None or df.empty:
        return False
    last_ts = df.index[-1]
    now = pd.Timestamp.now(tz="UTC")
    next_open = last_ts + pd.Timedelta(minutes=interval_minutes)
    return now < next_open


def clear() -> int:
    """Wipe the cache. Returns count of files removed."""
    n = 0
    for p in CACHE_DIR.glob("*.pkl"):
        try:
            p.unlink()
            n += 1
        except OSError:
            pass
    return n
