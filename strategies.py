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


# --- Built-in presets ----------------------------------------------------------


def _builtin_strategies() -> list[Strategy]:
    gc = LOGICS["gaussian_channel_v3_1"]
    base = gc.defaults()
    fast = dict(base, fast_response=True)
    slow = dict(base, period=200)
    return [
        Strategy("GaussianChannel v3.1 (default)", "gaussian_channel_v3_1", base),
        Strategy("GaussianChannel v3.1 — Fast response", "gaussian_channel_v3_1", fast),
        Strategy("GaussianChannel v3.1 — Slow (period 200)", "gaussian_channel_v3_1", slow),
    ]


DEFAULT_STRATEGY_NAME = "GaussianChannel v3.1 (default)"


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
