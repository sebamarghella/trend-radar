"""Strategy registry + presets.

A *logic* is a Python implementation of a trading strategy (e.g. GaussianChannel
v3.1). A *strategy* is a named bundle of (logic + parameter values) — a preset.
Multiple presets can share the same logic with different params.

Design so the four asset-class tabs can each use a different strategy:
  - LOGICS:        registry of available logic implementations
  - Strategy:      a named preset (logic_key + params), JSON-serializable
  - load/save:     presets persist as JSON in strategies/
  - assignments:   which strategy each asset class uses (strategy_assignments.json)

Porting a new Pine strategy = add a LogicSpec here. Everything else (UI dropdowns,
presets, alerts) works automatically.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from gaussian_channel import (
    GCParams,
    SignalState,
    TradeRecord,
    compute_stats,  # re-exported for convenience
    gaussian_channel,
    replay_strategy,
    stoch_rsi_k,
)

STRATEGIES_DIR = Path(__file__).parent / "strategies"
STRATEGIES_DIR.mkdir(exist_ok=True)
ASSIGNMENTS_FILE = Path(__file__).parent / "strategy_assignments.json"


# --- Parameter schema ----------------------------------------------------------


@dataclass
class ParamSpec:
    key: str
    label: str
    kind: str  # "int" | "float" | "bool"
    default: Any
    min: float | None = None
    max: float | None = None
    step: float | None = None


@dataclass
class StrategyResult:
    snapshot: SignalState
    state_series: pd.Series
    trades: list[TradeRecord]
    overlays: pd.DataFrame | None  # price-overlay columns to chart (e.g. filt/hband/lband)


@dataclass
class LogicSpec:
    key: str
    label: str
    description: str
    param_schema: list[ParamSpec]
    run: Callable[[pd.DataFrame, dict], StrategyResult]

    def defaults(self) -> dict:
        return {p.key: p.default for p in self.param_schema}

    def coerce(self, params: dict) -> dict:
        """Fill missing keys with defaults and coerce types."""
        out = self.defaults()
        for p in self.param_schema:
            if p.key in params and params[p.key] is not None:
                v = params[p.key]
                if p.kind == "int":
                    v = int(v)
                elif p.kind == "float":
                    v = float(v)
                elif p.kind == "bool":
                    v = bool(v)
                out[p.key] = v
        return out


# --- Strategy (a named preset) -------------------------------------------------


@dataclass
class Strategy:
    name: str
    logic_key: str
    params: dict

    def to_dict(self) -> dict:
        return {"name": self.name, "logic_key": self.logic_key, "params": self.params}


# --- Logic registry ------------------------------------------------------------

LOGICS: dict[str, LogicSpec] = {}


def register(spec: LogicSpec) -> None:
    LOGICS[spec.key] = spec


# --- GaussianChannel v3.1 ------------------------------------------------------

_GC_V31_SCHEMA = [
    ParamSpec("poles", "Poles (N)", "int", 4, 1, 9, 1),
    ParamSpec("period", "Sampling period", "int", 144, 2, 1000, 1),
    ParamSpec("multiplier", "TR multiplier", "float", 1.414, 0.0, 10.0, 0.1),
    ParamSpec("reduced_lag", "Reduced lag mode", "bool", False),
    ParamSpec("fast_response", "Fast response mode", "bool", False),
    ParamSpec("rsi_length", "Stoch RSI — RSI length", "int", 14, 2, 100, 1),
    ParamSpec("stoch_length", "Stoch RSI — Stoch length", "int", 14, 2, 100, 1),
    ParamSpec("smooth_k", "Stoch RSI — Smooth K", "int", 3, 1, 50, 1),
]


def _run_gc_v31(df: pd.DataFrame, params: dict) -> StrategyResult:
    gc = GCParams(
        poles=params["poles"],
        period=params["period"],
        multiplier=params["multiplier"],
        reduced_lag=params["reduced_lag"],
        fast_response=params["fast_response"],
    )
    channel = gaussian_channel(df, gc)
    k = stoch_rsi_k(
        df["close"].to_numpy(),
        params["rsi_length"],
        params["stoch_length"],
        params["smooth_k"],
    )
    snap, state, trades = replay_strategy(df, channel, k)
    return StrategyResult(snapshot=snap, state_series=state, trades=trades, overlays=channel)


register(LogicSpec(
    key="gaussian_channel_v3_1",
    label="GaussianChannel v3.1",
    description=(
        "Donovan Wall Gaussian filter + Stoch RSI confluence. Long when filter "
        "rising AND close > upper band AND stoch K > 80 or < 20; exit on close "
        "crossunder upper band."
    ),
    param_schema=_GC_V31_SCHEMA,
    run=_run_gc_v31,
))


# --- Donchian Breakout v1.0 -------------------------------------------------

_DONCHIAN_V10_SCHEMA = [
    ParamSpec("entry_len", "Entry High Length", "int", 20, 2, 500, 1),
    ParamSpec("exit_len", "Exit Low Length", "int", 10, 2, 500, 1),
]


def _classify_donchian_bar(
    close_now: float,
    close_prev: float,
    upper: float | None,
    lower: float | None,
) -> str:
    if upper is not None and not pd.isna(upper) and close_now > upper:
        return "STRONG_UP"
    if lower is not None and not pd.isna(lower) and close_now < lower:
        return "STRONG_DOWN"
    if close_now > close_prev:
        return "UP"
    if close_now < close_prev:
        return "DOWN"
    return "NEUTRAL"


def _run_donchian_v10(df: pd.DataFrame, params: dict) -> StrategyResult:
    high_band = df["high"].rolling(params["entry_len"], min_periods=params["entry_len"]).max().shift(1)
    low_band = df["low"].rolling(params["exit_len"], min_periods=params["exit_len"]).min().shift(1)
    mid_band = (high_band + low_band) / 2.0

    close = df["close"]
    index = df.index
    state = pd.Series(0, index=index, name="long", dtype="int8")
    trades: list[TradeRecord] = []
    in_pos = False
    entry_idx: int | None = None
    entry_price: float | None = None
    state_start = 0

    for t in range(1, len(df)):
        upper = high_band.iloc[t]
        lower = low_band.iloc[t]
        if pd.isna(upper) or pd.isna(lower):
            state.iloc[t] = 1 if in_pos else 0
            continue

        if in_pos:
            if close.iloc[t] < lower:
                trades[-1].exit_ts = index[t]
                trades[-1].exit_price = float(close.iloc[t])
                in_pos = False
                entry_idx = None
                entry_price = None
                state_start = t
        else:
            if close.iloc[t] > upper:
                in_pos = True
                entry_idx = t
                entry_price = float(close.iloc[t])
                state_start = t
                trades.append(
                    TradeRecord(
                        entry_ts=index[t],
                        entry_price=entry_price,
                        exit_ts=None,
                        exit_price=None,
                    )
                )

        state.iloc[t] = 1 if in_pos else 0

    upper_last = high_band.iloc[-1] if len(high_band) else float("nan")
    lower_last = low_band.iloc[-1] if len(low_band) else float("nan")
    mid_last = mid_band.iloc[-1] if len(mid_band) else float("nan")
    close_last = float(close.iloc[-1])
    close_prev = float(close.iloc[-2]) if len(close) >= 2 else close_last
    bar_color = _classify_donchian_bar(
        close_last,
        close_prev,
        None if pd.isna(upper_last) else float(upper_last),
        None if pd.isna(lower_last) else float(lower_last),
    )
    upper_prev = high_band.iloc[-2] if len(high_band) >= 2 else float("nan")
    filter_up = (
        not pd.isna(upper_last)
        and not pd.isna(upper_prev)
        and float(upper_last) > float(upper_prev)
    )
    close_vs_hband_pct = (
        float((close_last - float(upper_last)) / float(upper_last) * 100.0)
        if not pd.isna(upper_last) and float(upper_last) != 0.0
        else 0.0
    )

    snapshot = SignalState(
        in_position=in_pos,
        bars_in_state=max(len(df) - 1 - state_start, 0),
        entry_index=entry_idx,
        entry_price=entry_price,
        bar_color=bar_color,
        filter_up=filter_up,
        close_vs_hband_pct=close_vs_hband_pct,
        stoch_k=None,
        last_close=close_last,
        last_filter=float(mid_last) if not pd.isna(mid_last) else close_last,
        last_hband=float(upper_last) if not pd.isna(upper_last) else close_last,
        last_lband=float(lower_last) if not pd.isna(lower_last) else close_last,
    )
    overlays = pd.DataFrame(
        {
            "filt": mid_band,
            "hband": high_band,
            "lband": low_band,
        },
        index=index,
    )
    return StrategyResult(snapshot=snapshot, state_series=state, trades=trades, overlays=overlays)


register(LogicSpec(
    key="donchian_breakout_v1_0",
    label="Donchian Breakout v1.0",
    description=(
        "Classic Turtle-style long breakout. Enter on a close above the previous N-bar "
        "high and exit on a close below the previous M-bar low."
    ),
    param_schema=_DONCHIAN_V10_SCHEMA,
    run=_run_donchian_v10,
))


# --- Built-in presets ----------------------------------------------------------


def _builtin_strategies() -> list[Strategy]:
    gc = LOGICS["gaussian_channel_v3_1"]
    base = gc.defaults()
    fast = dict(base, fast_response=True)
    slow = dict(base, period=200)
    donchian = LOGICS["donchian_breakout_v1_0"]
    donchian_base = donchian.defaults()
    return [
        Strategy("GaussianChannel v3.1 (default)", "gaussian_channel_v3_1", base),
        Strategy("GaussianChannel v3.1 — Fast response", "gaussian_channel_v3_1", fast),
        Strategy("GaussianChannel v3.1 — Slow (period 200)", "gaussian_channel_v3_1", slow),
        Strategy("Donchian Breakout v1.0 (20/10)", "donchian_breakout_v1_0", donchian_base),
    ]


DEFAULT_STRATEGY_NAME = "GaussianChannel v3.1 (default)"
DEFAULT_LOGIC_KEY = "gaussian_channel_v3_1"


def _builtin_names() -> set[str]:
    return {s.name for s in _builtin_strategies()}


def is_builtin(name: str) -> bool:
    return name in _builtin_names()


def list_logics() -> list[tuple[str, str]]:
    """(key, label) for every registered logic — drives the Strategy dropdown."""
    return [(spec.key, spec.label) for spec in LOGICS.values()]


# --- Persistence ---------------------------------------------------------------


def _safe_filename(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_")
    return f"{slug or 'strategy'}.json"


def parse_strategy_dict(d: dict) -> Strategy:
    """Validate a strategy dict (from uploaded JSON). Raises ValueError if bad."""
    if not isinstance(d, dict):
        raise ValueError("strategy JSON must be an object")
    name = str(d.get("name", "")).strip()
    if not name:
        raise ValueError("missing 'name'")
    logic_key = str(d.get("logic_key", "")).strip()
    if logic_key not in LOGICS:
        known = ", ".join(LOGICS.keys())
        raise ValueError(f"unknown logic_key '{logic_key}'. Known: {known}")
    params = d.get("params", {})
    if not isinstance(params, dict):
        raise ValueError("'params' must be an object")
    coerced = LOGICS[logic_key].coerce(params)
    return Strategy(name=name, logic_key=logic_key, params=coerced)


def save_strategy(strategy: Strategy) -> Path:
    path = STRATEGIES_DIR / _safe_filename(strategy.name)
    path.write_text(json.dumps(strategy.to_dict(), indent=2))
    return path


def load_strategies() -> dict[str, Strategy]:
    """Built-in presets plus any JSON files in strategies/. Keyed by name."""
    out: dict[str, Strategy] = {s.name: s for s in _builtin_strategies()}
    for p in sorted(STRATEGIES_DIR.glob("*.json")):
        try:
            d = json.loads(p.read_text())
            strat = parse_strategy_dict(d)
            out[strat.name] = strat  # user file overrides builtin of same name
        except (json.JSONDecodeError, ValueError, OSError) as e:
            print(f"[warn] skipping bad strategy file {p.name}: {e}")
    return out


def presets_for_logic(logic_key: str, strategies: dict[str, Strategy] | None = None) -> dict[str, Strategy]:
    """Presets that belong to one logic — drives the Preset dropdown."""
    strategies = strategies if strategies is not None else load_strategies()
    return {name: s for name, s in strategies.items() if s.logic_key == logic_key}


def delete_strategy(name: str) -> bool:
    """Delete a user preset's JSON file. Built-ins can't be deleted. Returns True
    if a file was removed. Matches by the strategy's `name` field, not filename."""
    if is_builtin(name):
        return False
    for p in STRATEGIES_DIR.glob("*.json"):
        try:
            d = json.loads(p.read_text())
            if d.get("name") == name:
                p.unlink()
                return True
        except (json.JSONDecodeError, OSError):
            continue
    return False


# --- Per-asset-class assignment ------------------------------------------------


def load_assignments() -> dict[str, str]:
    if not ASSIGNMENTS_FILE.exists():
        return {}
    try:
        return json.loads(ASSIGNMENTS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_assignment(asset_key: str, strategy_name: str) -> None:
    state = load_assignments()
    state[asset_key] = strategy_name
    try:
        ASSIGNMENTS_FILE.write_text(json.dumps(state, indent=2))
    except OSError:
        pass


def get_assignment(asset_key: str, fallback: str = DEFAULT_STRATEGY_NAME) -> str:
    return load_assignments().get(asset_key, fallback)


# --- Run -----------------------------------------------------------------------


def run_strategy(strategy: Strategy, df: pd.DataFrame) -> StrategyResult:
    logic = LOGICS[strategy.logic_key]
    params = logic.coerce(strategy.params)
    return logic.run(df, params)
