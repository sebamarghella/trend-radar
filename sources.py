"""Multi-exchange data source layer.

Each source implements the same interface: list its tradable symbols, propose
candidate symbol names for a given base (e.g. "BTC"), and fetch OHLC klines.

A resolver walks sources in priority order and returns the first one that has
the coin. This lets us cover the long tail of the top 100 — Binance doesn't
list every coin (notably OKB, KCS, MNT, HYPE, XMR), but Gate.io and Kraken fill
most of the gap.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Iterable

import pandas as pd
import requests

DEFAULT_TIMEOUT = 15


class SourceError(RuntimeError):
    pass


# --- Base class ----------------------------------------------------------------


class DataSource(ABC):
    name: str        # human-friendly, e.g. "Binance"
    short: str       # 3-letter badge, e.g. "BIN"
    tv_prefix: str   # TradingView exchange prefix, e.g. "BINANCE"

    @abstractmethod
    def tradable_symbols(self) -> set[str]:
        """All symbols this exchange currently trades on the spot market."""

    @abstractmethod
    def candidate_symbols(self, base: str) -> list[str]:
        """Candidate symbol names to try for a base coin, in priority order."""

    @abstractmethod
    def fetch_klines(self, symbol: str, interval_minutes: int) -> pd.DataFrame:
        """Fetch OHLC. DataFrame has UTC tz-aware index and columns open/high/low/close/volume."""

    def fetch_with_retry(self, symbol: str, interval_minutes: int, attempts: int = 3) -> pd.DataFrame:
        last_err: Exception | None = None
        for i in range(attempts):
            try:
                return self.fetch_klines(symbol, interval_minutes)
            except (requests.RequestException, SourceError) as e:
                last_err = e
                time.sleep(0.3 * (i + 1))
        raise SourceError(f"{self.name}: failed to fetch {symbol}: {last_err}")


def _get_json(url: str, params: dict | None = None) -> dict | list:
    r = requests.get(url, params=params, timeout=DEFAULT_TIMEOUT)
    r.raise_for_status()
    return r.json()


# --- Binance -------------------------------------------------------------------


class BinanceSource(DataSource):
    name = "Binance"
    short = "BIN"
    tv_prefix = "BINANCE"

    PRIMARY_URL = "https://api.binance.com/api/v3"
    FALLBACK_URL = "https://data-api.binance.vision/api/v3"
    INTERVAL_MAP: dict[int, str] = {
        1: "1m", 5: "5m", 15: "15m", 30: "30m",
        60: "1h", 120: "2h", 240: "4h", 360: "6h", 720: "12h",
        1440: "1d", 4320: "3d", 10080: "1w",
    }

    def _get(self, path: str, params: dict | None = None) -> dict | list:
        last_err: Exception | None = None
        for base in (self.PRIMARY_URL, self.FALLBACK_URL):
            try:
                r = requests.get(f"{base}/{path}", params=params, timeout=DEFAULT_TIMEOUT)
                if r.status_code == 451:
                    last_err = SourceError(f"{base} geo-blocked")
                    continue
                r.raise_for_status()
                return r.json()
            except requests.RequestException as e:
                last_err = e
                continue
        raise SourceError(f"Binance endpoints failed: {last_err}")

    def tradable_symbols(self) -> set[str]:
        info = self._get("exchangeInfo")
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

    def candidate_symbols(self, base: str) -> list[str]:
        return [f"{base}USDT", f"{base}USDC", f"{base}FDUSD"]

    def fetch_klines(self, symbol: str, interval_minutes: int) -> pd.DataFrame:
        interval = self.INTERVAL_MAP.get(interval_minutes)
        if interval is None:
            raise SourceError(f"unsupported interval {interval_minutes}m for Binance")
        data = self._get("klines", {"symbol": symbol, "interval": interval, "limit": 1000})
        if not data:
            raise SourceError(f"no klines for {symbol}")
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


# --- Gate.io -------------------------------------------------------------------


class GateIOSource(DataSource):
    name = "Gate.io"
    short = "GIO"
    tv_prefix = "GATEIO"

    BASE_URL = "https://api.gateio.ws/api/v4"
    INTERVAL_MAP: dict[int, str] = {
        1: "1m", 5: "5m", 15: "15m", 30: "30m",
        60: "1h", 240: "4h", 480: "8h",
        1440: "1d", 10080: "7d", 43200: "30d",
    }

    def tradable_symbols(self) -> set[str]:
        data = _get_json(f"{self.BASE_URL}/spot/currency_pairs")
        # Each entry has id like "BTC_USDT", trade_status: "tradable", etc.
        return {
            d["id"] for d in data
            if d.get("trade_status") == "tradable"
        }

    def candidate_symbols(self, base: str) -> list[str]:
        return [f"{base}_USDT", f"{base}_USDC"]

    def fetch_klines(self, symbol: str, interval_minutes: int) -> pd.DataFrame:
        interval = self.INTERVAL_MAP.get(interval_minutes)
        if interval is None:
            raise SourceError(f"unsupported interval {interval_minutes}m for Gate.io")
        data = _get_json(
            f"{self.BASE_URL}/spot/candlesticks",
            {"currency_pair": symbol, "interval": interval, "limit": 1000},
        )
        if not data:
            raise SourceError(f"no klines for {symbol}")
        # Gate.io candle format: [timestamp_s, quote_vol, close, high, low, open, base_vol, finished_bool]
        df = pd.DataFrame(
            data,
            columns=["ts_s", "quote_volume", "close", "high", "low", "open", "volume", "finished"],
        )
        df["ts"] = pd.to_datetime(df["ts_s"].astype("int64"), unit="s", utc=True)
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = pd.to_numeric(df[col])
        return df.set_index("ts")[["open", "high", "low", "close", "volume"]].sort_index()


# --- Kraken --------------------------------------------------------------------


class KrakenSource(DataSource):
    name = "Kraken"
    short = "KRK"
    tv_prefix = "KRAKEN"

    BASE_URL = "https://api.kraken.com/0/public"
    # Kraken intervals are in minutes — pass directly.
    SUPPORTED = {1, 5, 15, 30, 60, 240, 1440, 10080, 21600}
    SYMBOL_OVERRIDES = {"BTC": "XBT", "DOGE": "XDG"}

    def _get(self, path: str, params: dict | None = None) -> dict:
        r = requests.get(f"{self.BASE_URL}/{path}", params=params, timeout=DEFAULT_TIMEOUT)
        r.raise_for_status()
        payload = r.json()
        if payload.get("error"):
            raise SourceError(", ".join(payload["error"]))
        return payload["result"]

    def tradable_symbols(self) -> set[str]:
        """Return Kraken altnames (the short forms like XBTUSD, ETHUSD)."""
        pairs = self._get("AssetPairs", {"info": "info"})
        out: set[str] = set()
        for canonical, info in pairs.items():
            out.add(canonical.upper())
            if "altname" in info:
                out.add(info["altname"].upper())
            if "wsname" in info:
                out.add(info["wsname"].replace("/", "").upper())
        return out

    def candidate_symbols(self, base: str) -> list[str]:
        ksym = self.SYMBOL_OVERRIDES.get(base, base)
        cands = [f"{ksym}USD", f"{ksym}USDT", f"{ksym}USDC"]
        if ksym != base:
            cands.extend([f"{base}USD", f"{base}USDT", f"{base}USDC"])
        # Dedup, preserve order
        seen: set[str] = set()
        return [c for c in cands if not (c in seen or seen.add(c))]

    def fetch_klines(self, symbol: str, interval_minutes: int) -> pd.DataFrame:
        if interval_minutes not in self.SUPPORTED:
            raise SourceError(f"unsupported interval {interval_minutes}m for Kraken")
        result = self._get("OHLC", {"pair": symbol, "interval": interval_minutes})
        candle_key = next(k for k in result.keys() if k != "last")
        rows = result[candle_key]
        df = pd.DataFrame(
            rows,
            columns=["ts_s", "open", "high", "low", "close", "vwap", "volume", "count"],
        )
        df["ts"] = pd.to_datetime(df["ts_s"].astype("int64"), unit="s", utc=True)
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = pd.to_numeric(df[col])
        return df.set_index("ts")[["open", "high", "low", "close", "volume"]].sort_index()


# --- Yahoo Finance -------------------------------------------------------------


class YahooSource(DataSource):
    """For stocks / metals / commodities. Yahoo has no enumerate-all endpoint;
    used in trust mode (the universe provides valid tickers directly)."""

    name = "Yahoo"
    short = "YHO"
    tv_prefix = ""  # filled in per-asset by the universe entry

    # Yahoo uses string codes. Intraday is rate-limited to 60 days of history.
    INTERVAL_MAP: dict[int, str] = {
        60: "1h", 1440: "1d", 10080: "1wk", 43200: "1mo",
    }
    PERIOD_FOR_INTERVAL: dict[str, str] = {
        "1h": "60d", "1d": "5y", "1wk": "10y", "1mo": "max",
    }

    def __init__(self) -> None:
        try:
            import yfinance  # noqa: F401
        except ImportError as e:
            raise SourceError(f"yfinance required for YahooSource: {e}")

    def tradable_symbols(self) -> set[str] | None:
        # None = trust mode: the resolver should accept any candidate.
        return None

    def candidate_symbols(self, base: str) -> list[str]:
        # The asset universe provides the exact Yahoo ticker; no transformation.
        return [base]

    def fetch_klines(self, symbol: str, interval_minutes: int) -> pd.DataFrame:
        import yfinance as yf
        interval = self.INTERVAL_MAP.get(interval_minutes)
        if interval is None:
            raise SourceError(f"unsupported interval {interval_minutes}m for Yahoo")
        period = self.PERIOD_FOR_INTERVAL[interval]
        df = yf.download(
            symbol, period=period, interval=interval,
            auto_adjust=True, progress=False, threads=False,
        )
        if df is None or df.empty:
            raise SourceError(f"no data for {symbol}")
        # yfinance returns multi-level columns when downloading even a single
        # ticker (level 0 = "Open"/"High"/..., level 1 = ticker name).
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        # Drop the residual columns.name ("Price") — Altair/Vega-Lite trips on
        # a named columns Index and silently renders an empty chart.
        df.columns = pd.Index(list(df.columns), name=None)
        df = df.rename(columns={
            "Open": "open", "High": "high", "Low": "low",
            "Close": "close", "Volume": "volume",
        })
        idx = pd.to_datetime(df.index)
        if idx.tz is None:
            idx = idx.tz_localize("UTC")
        else:
            idx = idx.tz_convert("UTC")
        # Normalize precision to ns to match the other sources (Binance is ms,
        # Kraken/Gate are s — all upcast cleanly to ns).
        df.index = pd.DatetimeIndex(idx).astype("datetime64[ns, UTC]")
        df.index.name = "ts"
        keep = [c for c in ("open", "high", "low", "close", "volume") if c in df.columns]
        return df[keep].sort_index().dropna()


# --- Multi-source resolver -----------------------------------------------------


DEFAULT_PRIORITY: list[type[DataSource]] = [BinanceSource, GateIOSource, KrakenSource]


class Resolver:
    """Holds a cached universe-of-symbols per source and resolves a coin to the first hit.

    Sources that return None from tradable_symbols() are in 'trust mode': the
    resolver accepts the first candidate without checking availability (used
    for Yahoo, which has no list-all endpoint).
    """

    def __init__(self, sources: Iterable[DataSource]):
        self.sources = list(sources)
        self._available: dict[str, set[str] | None] = {}
        for src in self.sources:
            try:
                self._available[src.name] = src.tradable_symbols()
            except Exception as e:
                print(f"[warn] {src.name} symbol list failed: {e}")
                self._available[src.name] = set()

    def coverage(self) -> dict[str, int | str]:
        out: dict[str, int | str] = {}
        for src in self.sources:
            avail = self._available[src.name]
            out[src.name] = "trust" if avail is None else len(avail)
        return out

    def resolve(self, base: str) -> tuple[DataSource, str] | None:
        for src in self.sources:
            avail = self._available[src.name]
            cands = src.candidate_symbols(base)
            if avail is None:
                # Trust mode: take the first candidate without checking.
                if cands:
                    return (src, cands[0])
                continue
            for cand in cands:
                if cand in avail:
                    return (src, cand)
        return None


def default_resolver() -> Resolver:
    """Resolver with Binance → Gate.io → Kraken for crypto."""
    return Resolver([cls() for cls in DEFAULT_PRIORITY])


def yahoo_resolver() -> Resolver:
    """Resolver wrapping a single YahooSource for stocks/metals/commodities."""
    return Resolver([YahooSource()])
