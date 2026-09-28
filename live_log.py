"""Persistent forward track record for the basket (written by the daily cron).

Two committed files per asset class in `.cache/`:
  live_{class}_equity.json  meta + one row per completed session:
                            {date, ret, equity, n_open, strategy}
  live_{class}_trades.json  every trade since tracking began:
                            {key, symbol, entry_date, entry_price, exit_date,
                             exit_price, carried, strategy}

Design rules:
- Idempotent by date: re-running never duplicates a day, and a skipped cron
  run is healed on the next one (all missing sessions are appended).
- Only sessions strictly before today (UTC) are logged, so a manual run during
  market hours never records a partial bar that could not be revised later.
- Tracking starts on the first run. Positions already open that day are logged
  as `carried` trades entered at that day's close, so equity and trade stats
  describe the same book.
- Each row records the strategy that produced it; switching presets keeps the
  history and the UI flags the change.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Iterable

import pandas as pd

import performance as perf

CACHE_DIR = Path(__file__).parent / ".cache"
LIVE_CLASSES = {"stocks"}


def _paths(class_key: str) -> tuple[Path, Path]:
    return CACHE_DIR / f"live_{class_key}_equity.json", CACHE_DIR / f"live_{class_key}_trades.json"


def _read(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def load_equity(class_key: str) -> dict:
    return _read(_paths(class_key)[0], {"meta": {}, "days": []})


def load_trades(class_key: str) -> list[dict]:
    data = _read(_paths(class_key)[1], [])
    return data if isinstance(data, list) else []


def _write(path: Path, payload) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1), encoding="utf-8")


def _d(ts) -> str:
    return pd.Timestamp(ts).strftime("%Y-%m-%d")


def _norm(ts) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
    return t.normalize()


def update(
    class_key: str,
    strategy_name: str,
    items: Iterable[tuple[str, pd.DataFrame, object]],
    rerun: Callable[[pd.DataFrame], object],
    *,
    slots: int = perf.DEFAULT_SLOTS,
    commission: float = perf.DEFAULT_COMMISSION,
    today: pd.Timestamp | None = None,
) -> dict:
    """Append newly completed sessions + trade changes. Returns a small summary.

    items : (symbol, ohlc_df, StrategyResult) per symbol.
    rerun : df -> StrategyResult; used only for symbols whose last bar is
            today's (still-forming) session, re-evaluated without it.
    """
    today = _norm(today if today is not None else pd.Timestamp.now(tz="UTC"))

    resolved: list[tuple[str, pd.DataFrame, object]] = []
    for sym, df, result in items:
        if _norm(df.index[-1]) >= today:
            df = df[[_norm(t) < today for t in df.index]]
            if len(df) < 60:
                continue
            result = rerun(df)
        resolved.append((sym, df, result))
    if not resolved:
        return {"appended": 0, "reason": "no complete data"}

    daily, info = perf.basket_daily(
        [(s, df, r.state_series) for s, df, r in resolved], slots=slots, commission=commission,
    )
    if daily.empty:
        return {"appended": 0, "reason": "no positions ever"}

    eq_path, tr_path = _paths(class_key)
    eq = load_equity(class_key)
    trades = load_trades(class_key)
    days: list[dict] = eq.get("days", [])

    if not days:
        start = daily.index[-1]
        eq["meta"] = {"class": class_key, "start": _d(start), "slots": slots, "commission": commission}
        days.append({"date": _d(start), "ret": 0.0, "equity": 1.0, "hodl_ret": 0.0,
                     "n_open": int(daily["n_open"].iloc[-1]), "strategy": strategy_name})
        start_ts = start
        appended = 1
    else:
        start_ts = _norm(eq["meta"]["start"])
        last = _norm(days[-1]["date"])
        equity = float(days[-1]["equity"])
        appended = 0
        for ts, row in daily[daily.index > last].iterrows():
            equity *= 1.0 + float(row["ret"])
            days.append({"date": _d(ts), "ret": float(row["ret"]), "equity": equity,
                         "hodl_ret": float(row["hodl_ret"]), "n_open": int(row["n_open"]), "strategy": strategy_name})
            appended += 1
    eq["days"] = days

    by_key = {t["key"]: t for t in trades}
    for sym, df, result in resolved:
        closes = df["close"].astype(float)
        closes.index = [_norm(t) for t in closes.index]
        for tr in result.trades:
            entry = _norm(tr.entry_ts)
            exit_ = _norm(tr.exit_ts) if tr.exit_ts is not None else None
            if entry <= start_ts:
                if exit_ is not None and exit_ <= start_ts:
                    continue                       # closed before tracking began
                key = f"{sym}|carried"
                if key not in by_key:
                    if start_ts not in closes.index:
                        continue
                    by_key[key] = {"key": key, "symbol": sym, "entry_date": _d(start_ts),
                                   "entry_price": float(closes.loc[start_ts]),
                                   "exit_date": None, "exit_price": None,
                                   "carried": True, "strategy": strategy_name}
            else:
                key = f"{sym}|{_d(entry)}"
                if key not in by_key:
                    by_key[key] = {"key": key, "symbol": sym, "entry_date": _d(entry),
                                   "entry_price": float(tr.entry_price),
                                   "exit_date": None, "exit_price": None,
                                   "carried": False, "strategy": strategy_name}
            rec = by_key[key]
            if exit_ is not None and rec["exit_date"] is None:
                rec["exit_date"] = _d(exit_)
                rec["exit_price"] = float(tr.exit_price)

    # Raw prices: keep a running split multiplier on every logged trade so its
    # return is comparable with the (split-adjusted) equity curve.
    facs: dict[str, pd.Series] = {}
    for sym, df, _ in resolved:
        c = df["close"].astype(float).copy()
        c.index = pd.DatetimeIndex([_norm(t) for t in c.index])
        f = perf.split_factors(c.to_frame("x"))["x"]
        facs[sym] = f[f != 1.0]
    for rec in by_key.values():
        rec["split_mult"] = perf._split_multiplier(
            facs.get(rec["symbol"]), _norm(rec["entry_date"]),
            _norm(rec["exit_date"]) if rec["exit_date"] else None,
        )

    _write(eq_path, eq)
    _write(tr_path, sorted(by_key.values(), key=lambda t: (t["entry_date"], t["symbol"])))
    return {"appended": appended, "days": len(days), "trades": len(by_key),
            "suspect_bars": info.get("suspect_bars", 0)}


def live_trades_frame(class_key: str, last_close: dict[str, float] | None = None,
                      commission: float = perf.DEFAULT_COMMISSION) -> pd.DataFrame:
    """Live trades as the `performance.trades_frame` schema. Open trades are
    marked to `last_close[symbol]` when supplied (else excluded from returns)."""
    rows = []
    for t in load_trades(class_key):
        closed = t["exit_price"] is not None
        mult = t.get("split_mult", 1.0)
        if closed:
            ret = t["exit_price"] * mult / t["entry_price"] * (1.0 - commission) ** 2 - 1.0
        elif last_close and t["symbol"] in last_close:
            ret = last_close[t["symbol"]] * mult / t["entry_price"] * (1.0 - commission) ** 2 - 1.0
        else:
            ret = None
        rows.append({"symbol": t["symbol"], "entry": pd.Timestamp(t["entry_date"]),
                     "exit": pd.Timestamp(t["exit_date"]) if t["exit_date"] else pd.NaT,
                     "entry_price": t["entry_price"], "exit_price": t["exit_price"],
                     "ret": ret, "closed": closed, "carried": t.get("carried", False)})
    cols = ["symbol", "entry", "exit", "entry_price", "exit_price", "ret", "closed", "carried"]
    return pd.DataFrame(rows, columns=cols)


def live_daily_frame(class_key: str) -> pd.DataFrame:
    days = load_equity(class_key).get("days", [])
    if not days:
        return pd.DataFrame(columns=["ret", "equity", "n_open", "drawdown", "strategy"])
    d = pd.DataFrame(days)
    d.index = pd.DatetimeIndex(pd.to_datetime(d.pop("date")))
    d["drawdown"] = (d["equity"] / d["equity"].cummax() - 1.0) * 100.0
    if "hodl_ret" not in d:
        d["hodl_ret"] = 0.0
    d["hodl_ret"] = d["hodl_ret"].fillna(0.0)          # rows logged before HODL existed
    d["hodl_equity"] = (1.0 + d["hodl_ret"]).cumprod()
    return d
