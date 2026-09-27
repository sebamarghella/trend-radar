"""Stocks universe: live top-N US-listed stocks by market cap, with a static fallback.

Source is NASDAQ's public screener (every NYSE / NASDAQ / AMEX listing incl.
ADRs, no API key). Ranked by market cap, preferreds dropped, one row per
company (GOOGL/GOOG, BRK-A/BRK-B ... keep the larger listing), top `target`
kept. Signum's Pro stocks radar scans ~566 names built the same way, so its
ranks line up with ours to within a few places.

Cached daily in .cache/stocks_universe.json; if the screener is unreachable
we fall back to the stale cache, then to the hand-picked megacap list.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import requests

SCREENER_URL = "https://api.nasdaq.com/api/screener/stocks"
UNIVERSE_CACHE = Path(__file__).parent / ".cache" / "stocks_universe.json"
UNIVERSE_TTL_S = 24 * 3600
UNIVERSE_TARGET = 570

# Everything after these markers is share-class / listing noise, not the company.
_NAME_NOISE = re.compile(
    r"\s+(Class [A-Z]\b|Common Stock|Common Shares|Capital Stock|Ordinary Shares|"
    r"American Depositary|Depositary Shares|Sponsored ADR|ADR\b|ADS\b|Series [A-Z]\b|"
    r"Subordinate Voting|Registered Shares|New\b).*$",
    re.IGNORECASE,
)


def clean_name(name: str) -> str:
    return _NAME_NOISE.sub("", name or "").strip(" .,-") or name


def company_key(name: str) -> str:
    """Normalised company identity used to collapse share classes."""
    base = clean_name(name).lower()
    base = re.sub(r"[^a-z0-9 ]", "", base)
    return re.sub(r"\b(inc|corp|corporation|co|plc|ltd|limited|holdings?|group|the|sa|nv|ag|se)\b",
                  "", base).strip()


def yahoo_symbol(symbol: str) -> str:
    return symbol.replace("/", "-").strip()  # BRK/B -> BRK-B


def rank_rows(rows: list[dict], target: int = UNIVERSE_TARGET) -> list[dict]:
    """Screener rows -> ranked universe entries (pure; unit-tested offline)."""
    def mcap(r: dict) -> float:
        try:
            return float(r.get("marketCap") or 0)
        except (TypeError, ValueError):
            return 0.0

    out: list[dict] = []
    seen: set[str] = set()
    for r in sorted(rows, key=mcap, reverse=True):
        sym = str(r.get("symbol") or "")
        if not sym or "^" in sym or mcap(r) <= 0:  # ^ = preferred shares
            continue
        key = company_key(r.get("name", sym))
        if key in seen:
            continue
        seen.add(key)
        out.append({"rank": len(out) + 1, "symbol": yahoo_symbol(sym),
                    "name": clean_name(r.get("name", sym))})
        if len(out) >= target:
            break
    return out


def _fetch_screener(attempts: int = 3) -> list[dict]:
    last: Exception | None = None
    for i in range(attempts):
        try:
            r = requests.get(
                SCREENER_URL,
                params={"tableonly": "true", "limit": "10000", "download": "true"},
                headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
                timeout=30,
            )
            r.raise_for_status()
            rows = (r.json().get("data") or {}).get("rows") or []
            if rows:
                return rows
            raise requests.HTTPError("empty screener payload")
        except (requests.RequestException, ValueError) as e:
            last = e
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"NASDAQ screener failed: {last}")


def _read_cache() -> tuple[float, list[dict]] | None:
    try:
        payload = json.loads(UNIVERSE_CACHE.read_text(encoding="utf-8"))
        return float(payload["fetched_at"]), list(payload["stocks"])
    except Exception:
        return None


def live_stocks_universe(fallback: list[dict], target: int = UNIVERSE_TARGET) -> list[dict]:
    """Top `target` US stocks by market cap. Fresh cache -> screener -> stale cache -> fallback."""
    cached = _read_cache()
    if cached and time.time() - cached[0] < UNIVERSE_TTL_S:
        stocks, provenance = cached[1], "nasdaq screener (cached)"
    else:
        try:
            stocks = rank_rows(_fetch_screener(), target=max(target, 1))
            provenance = "nasdaq screener"
            try:
                UNIVERSE_CACHE.parent.mkdir(parents=True, exist_ok=True)
                UNIVERSE_CACHE.write_text(
                    json.dumps({"fetched_at": time.time(), "stocks": stocks}), encoding="utf-8",
                )
            except OSError:
                pass
        except RuntimeError as e:
            print(f"[warn] {e}")
            if cached:
                stocks, provenance = cached[1], "nasdaq screener (stale cache)"
            else:
                stocks, provenance = fallback, "static megacap list"
    picked = stocks[:target]
    print(f"[universe] {len(picked)} stocks from {provenance}")
    return picked
