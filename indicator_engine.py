"""Optional indicator engine backed by pandas-ta.

This keeps third-party indicator usage in one place so strategy registrations
can stay small and the rest of the app can gracefully degrade if the package is
not installed in the runtime.
"""

from __future__ import annotations

import pandas as pd

try:
    import pandas_ta as _pta

    PANDAS_TA_AVAILABLE = True
    PANDAS_TA_IMPORT_ERROR = ""
except Exception as exc:  # noqa: BLE001
    _pta = None
    PANDAS_TA_AVAILABLE = False
    PANDAS_TA_IMPORT_ERROR = str(exc)


def require_pandasta() -> None:
    if not PANDAS_TA_AVAILABLE or _pta is None:
        detail = f": {PANDAS_TA_IMPORT_ERROR}" if PANDAS_TA_IMPORT_ERROR else ""
        raise RuntimeError(
            "pandas-ta is not available in this environment. Install it to use "
            f"the pandas-ta-backed strategy logics{detail}"
        )


def ema(close: pd.Series, *, length: int) -> pd.Series:
    require_pandasta()
    out = _pta.ema(close, length=length)
    if out is None:
        raise RuntimeError("pandas-ta ema() returned no data")
    return pd.Series(out, index=close.index, name=f"ema_{length}")


def supertrend(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    *,
    length: int,
    multiplier: float,
) -> pd.DataFrame:
    require_pandasta()
    out = _pta.supertrend(high=high, low=low, close=close, length=length, multiplier=multiplier)
    if out is None or out.empty:
        raise RuntimeError("pandas-ta supertrend() returned no data")
    return out
