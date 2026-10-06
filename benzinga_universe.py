"""Benzinga-backed low-float momentum universe for the Trend Radar UI.

Benzinga discovers candidates during the regular US session; Yahoo remains the
price-history source used by the strategy.  The movers endpoint currently
accepts a maximum of 500 results, so the result is checked locally and the UI
is told whenever that ceiling is reached.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import requests


API_URL = "https://api.benzinga.com/api/v1/market/movers"
CACHE_FILE = Path(__file__).parent / ".cache" / "benzinga_low_float_universe.json"
CACHE_TTL_S = 5 * 60
MAX_RESULTS = 500

# The public query language uses semicolon-separated clauses.  Keep the
# volume rule local: Benzinga currently accepts volume_gt but does not
# consistently apply it to the response's reported session volume.
SCREENER_QUERY = "close_gt_1;close_lt_20;sharefloat_gt_100k;sharefloat_lt_20m"

MIN_PRICE = 1.0
MAX_PRICE = 20.0
MIN_VOLUME = 10_000
MIN_FLOAT = 100_000
MAX_FLOAT = 20_000_000

_LAST_STATUS: dict[str, Any] = {}


class BenzingaUniverseError(RuntimeError):
    """The candidate universe could not be loaded safely."""


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _read_api_key() -> str:
    """Read the key locally, never from source control or a UI value."""
    try:
        import streamlit as st

        key = st.secrets.get("benzinga", {}).get("api_key", "")
    except Exception:
        key = ""
    # Keep command-line checks usable in a lean Python environment where the
    # Streamlit package itself is not installed. The file is already ignored by
    # git; this fallback never logs or serializes the key.
    if not key:
        try:
            import tomllib

            local_secrets = Path(__file__).parent / ".streamlit" / "secrets.toml"
            data = tomllib.loads(local_secrets.read_text(encoding="utf-8"))
            key = data.get("benzinga", {}).get("api_key", "")
        except (ImportError, OSError, ValueError, TypeError):
            key = ""
    key = str(key or os.environ.get("BENZINGA_API_KEY", "")).strip()
    if not key:
        raise BenzingaUniverseError(
            "Benzinga API key missing. Add [benzinga] api_key to Streamlit secrets."
        )
    return key


def select_candidates(gainers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate Benzinga's response using the exact TradingView-style bounds."""
    selected: list[dict[str, Any]] = []
    for item in gainers:
        symbol = str(item.get("symbol") or "").strip().upper()
        price = _as_float(item.get("price"))
        volume = _as_int(item.get("volume"))
        share_float = _as_int(item.get("shareFloat"))
        change_pct = _as_float(item.get("changePercent"))
        if (
            not symbol
            or price is None
            or volume is None
            or share_float is None
            or not (MIN_PRICE <= price <= MAX_PRICE)
            or volume <= MIN_VOLUME
            or not (MIN_FLOAT <= share_float <= MAX_FLOAT)
        ):
            continue
        selected.append(
            {
                "symbol": symbol,
                "name": str(item.get("companyName") or symbol).strip(),
                "scan_price": price,
                "scan_change_pct": change_pct,
                "scan_volume": volume,
                "scan_float": share_float,
            }
        )

    # Benzinga returns movers in change order, but make the ordering explicit
    # and deterministic before assigning the UI rank.
    selected.sort(key=lambda row: (row["scan_change_pct"] is None, -(row["scan_change_pct"] or 0), row["symbol"]))
    for rank, row in enumerate(selected, start=1):
        row["rank"] = rank
    return selected


def _parse_payload(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    result = payload.get("result")
    if not isinstance(result, dict):
        raise BenzingaUniverseError("Benzinga returned no market-movers result.")
    gainers = result.get("gainers")
    if not isinstance(gainers, list):
        raise BenzingaUniverseError("Benzinga returned no gainers list.")
    return select_candidates(gainers), {
        "source_count": len(gainers),
        "saturated": len(gainers) >= MAX_RESULTS,
        "from_date": result.get("fromDate"),
        "to_date": result.get("toDate"),
    }


def fetch_low_float_universe(api_key: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fetch one regular-session movers snapshot and validate it locally."""
    try:
        response = requests.get(
            API_URL,
            params={
                "token": api_key,
                "maxResults": MAX_RESULTS,
                "session": "REGULAR",
                "screenerQuery": SCREENER_QUERY,
            },
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise BenzingaUniverseError(f"Benzinga movers request failed: {exc}") from exc
    if not isinstance(payload, dict):
        raise BenzingaUniverseError("Benzinga returned an invalid movers payload.")
    return _parse_payload(payload)


def _load_cache() -> tuple[list[dict[str, Any]], dict[str, Any], float] | None:
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        fetched_at = float(data["fetched_at"])
        rows = data["rows"]
        metadata = data["metadata"]
        if not isinstance(rows, list) or not isinstance(metadata, dict):
            return None
        return rows, metadata, fetched_at
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _save_cache(rows: list[dict[str, Any]], metadata: dict[str, Any]) -> None:
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(
            json.dumps({"fetched_at": time.time(), "rows": rows, "metadata": metadata}),
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"[warn] could not cache Benzinga low-float universe: {exc}")


def _set_status(metadata: dict[str, Any], *, cache_state: str, fetched_at: float | None = None, error: str | None = None) -> None:
    global _LAST_STATUS
    _LAST_STATUS = {
        **metadata,
        "cache_state": cache_state,
        "fetched_at": fetched_at if fetched_at is not None else time.time(),
        "error": error,
    }


def live_low_float_universe(*, force_refresh: bool = False) -> list[dict[str, Any]]:
    """Use fresh/cached Benzinga data, with a stale-cache fallback on outages."""
    cached = _load_cache()
    now = time.time()
    if cached is not None and not force_refresh and now - cached[2] < CACHE_TTL_S:
        _set_status(cached[1], cache_state="cached", fetched_at=cached[2])
        return cached[0]

    try:
        rows, metadata = fetch_low_float_universe(_read_api_key())
    except BenzingaUniverseError as exc:
        if cached is not None:
            _set_status(cached[1], cache_state="stale", fetched_at=cached[2], error=str(exc))
            return cached[0]
        _set_status({}, cache_state="unavailable", error=str(exc))
        raise

    _save_cache(rows, metadata)
    _set_status(metadata, cache_state="fresh")
    return rows


def last_status() -> dict[str, Any]:
    """Metadata for the most recent universe load, for a transparent UI note."""
    return dict(_LAST_STATUS)
