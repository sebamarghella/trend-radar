"""Breakout detector: swing-high resistance, higher-low support, trend validation.

Modelled on the *semantics* of Signum's Trend Radar breakouts (their exact
rules aren't published and couldn't be recovered from their outputs; see
LEARNINGS.md). Rules:

  * Swing high / low: a bar whose high (low) is the extreme of the PIVOT_LEN
    bars on each side. It is only known PIVOT_LEN bars later (confirmation).
  * A range starts at a confirmed swing high; its resistance is that high
    and stays anchored (later swing highs inside the range don't move it).
  * Support = low of the latest confirmed swing low inside the range (the
    "higher low" Signum reports, not the range extreme). The range itself
    only ends on a close below its *lowest* swing low (a real breakdown);
    the next swing high then starts a new one.
  * Breakout = a close crossing above resistance once the range has held for
    at least MIN_RANGE_BARS bars (Signum's ranges ran 29-73 days in our
    sample; only real consolidations count, not every small swing).
  * A close above resistance (by more than EARLY_ESCAPE) before that means it
    wasn't a consolidation: the range is dropped.
  * Tuned against Signum's crypto breakouts (2026-09-27): statuses agree
    when dates line up, but dates match within a week on only ~1 in 5 coins -
    their range-selection rule differs and isn't recoverable.
  * Validated  = the trend (Gaussian filter rising) is up on the breakout bar,
    or turns up later before any close below support.
  * Invalidated = a close below support before validation.

Only the latest event is kept as "the" breakout (like Signum), plus the
current box so the chart can show levels before any breakout.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass

import numpy as np
import pandas as pd

PIVOT_LEN = 3
MIN_RANGE_BARS = 20
EARLY_ESCAPE = 0.0


@dataclass(frozen=True)
class Breakout:
    date: pd.Timestamp
    resistance: float
    support: float
    resistance_start: pd.Timestamp
    support_start: pd.Timestamp
    status: str                        # "validated" | "pending" | "invalidated"
    status_date: pd.Timestamp | None   # when it became validated / invalidated


@dataclass(frozen=True)
class BreakoutState:
    last: Breakout | None              # most recent breakout event
    resistance: float | None           # current (unbroken) box, if any
    support: float | None
    resistance_start: pd.Timestamp | None
    support_start: pd.Timestamp | None


STATUS_GLYPH = {"validated": "✓", "pending": "…", "invalidated": "✗"}


def _pivots(values: np.ndarray, L: int, kind: str) -> np.ndarray:
    """Boolean mask of swing points (ties allowed), vectorised via rolling extremes."""
    s = pd.Series(values)
    win = s.rolling(2 * L + 1, center=True)
    ext = win.max() if kind == "high" else win.min()
    return (s == ext).to_numpy() & ext.notna().to_numpy()


def detect(df: pd.DataFrame, trend_up: pd.Series | None = None, L: int = PIVOT_LEN) -> BreakoutState:
    """Run the detector over the full history. `trend_up` aligns to df.index."""
    n = len(df)
    if n < 2 * L + 2:
        return BreakoutState(None, None, None, None, None)
    hi, lo, cl = (df[c].to_numpy(dtype=float) for c in ("high", "low", "close"))
    up = (trend_up.reindex(df.index).fillna(False).to_numpy(dtype=bool)
          if trend_up is not None else np.zeros(n, bool))
    ph, pl = _pivots(hi, L, "high"), _pivots(lo, L, "low")
    idx = df.index

    box: dict | None = None   # {"R", "r_i", "S", "s_i"}
    last: dict | None = None

    for t in range(L, n):
        p = t - L  # pivot at p becomes known at bar t
        if box is None:
            if ph[p]:
                box = {"R": hi[p], "r_i": p, "S": None, "s_i": None, "floor": None}
        elif pl[p] and p > box["r_i"]:
            box["S"], box["s_i"] = lo[p], p
            box["floor"] = lo[p] if box["floor"] is None else min(box["floor"], lo[p])

        # resolve the pending event first (uses today's close)
        if last is not None and last["status"] == "pending" and t > last["i"]:
            if cl[t] < last["support"]:
                last.update(status="invalidated", status_i=t)
            elif up[t]:
                last.update(status="validated", status_i=t)

        if box is None:
            continue
        if box["floor"] is not None and cl[t] < box["floor"]:
            box = None                         # range broke down
            continue
        age = t - box["r_i"]
        if age < MIN_RANGE_BARS:
            if cl[t] > box["R"] * (1 + EARLY_ESCAPE):
                box = None                     # ran away: not a consolidation
            continue
        if box["S"] is not None and cl[t] > box["R"] and cl[t - 1] <= box["R"]:
            last = {"i": t, "resistance": box["R"], "support": box["S"],
                    "r_i": box["r_i"], "s_i": box["s_i"],
                    "status": "validated" if up[t] else "pending",
                    "status_i": t if up[t] else None}
            box = None

    ev = None
    if last is not None:
        ev = Breakout(
            date=idx[last["i"]], resistance=float(last["resistance"]), support=float(last["support"]),
            resistance_start=idx[last["r_i"]], support_start=idx[last["s_i"]],
            status=last["status"],
            status_date=idx[last["status_i"]] if last["status_i"] is not None else None,
        )
    return BreakoutState(
        last=ev,
        resistance=float(box["R"]) if box else None,
        support=float(box["S"]) if box and box["S"] is not None else None,
        resistance_start=idx[box["r_i"]] if box else None,
        support_start=idx[box["s_i"]] if box and box["s_i"] is not None else None,
    )


# --- cache (Streamlit reruns the script on every click) -----------------------
_CACHE: "OrderedDict[tuple, BreakoutState]" = OrderedDict()
_CACHE_MAX = 3000
_LOCK = threading.Lock()


def detect_cached(df: pd.DataFrame, trend_up: pd.Series | None, fingerprint: tuple) -> BreakoutState:
    tkey = (b"" if trend_up is None else
            np.packbits(trend_up.reindex(df.index).fillna(False).to_numpy(dtype=bool)).tobytes())
    key = (fingerprint, tkey)
    with _LOCK:
        hit = _CACHE.get(key)
        if hit is not None:
            _CACHE.move_to_end(key)
            return hit
    state = detect(df, trend_up)
    with _LOCK:
        _CACHE[key] = state
        while len(_CACHE) > _CACHE_MAX:
            _CACHE.popitem(last=False)
    return state
