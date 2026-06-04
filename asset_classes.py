"""Asset class registry — one entry per tab in the radar.

Each AssetClass bundles:
  - the universe to scan
  - which Resolver to instantiate for it
  - the default timeframe and the timeframes the user can switch between
  - the TradingView exchange prefix to use for "open in TV" links
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from coins import tradable_universe as crypto_universe
from sources import Resolver, default_resolver, yahoo_resolver


# --- Stocks (top US megacaps) --------------------------------------------------

STOCKS_UNIVERSE: list[dict] = [
    {"rank": 1, "symbol": "AAPL",  "name": "Apple"},
    {"rank": 2, "symbol": "MSFT",  "name": "Microsoft"},
    {"rank": 3, "symbol": "NVDA",  "name": "NVIDIA"},
    {"rank": 4, "symbol": "GOOGL", "name": "Alphabet"},
    {"rank": 5, "symbol": "AMZN",  "name": "Amazon"},
    {"rank": 6, "symbol": "META",  "name": "Meta Platforms"},
    {"rank": 7, "symbol": "TSLA",  "name": "Tesla"},
    {"rank": 8, "symbol": "BRK-B", "name": "Berkshire Hathaway"},
    {"rank": 9, "symbol": "AVGO",  "name": "Broadcom"},
    {"rank": 10, "symbol": "JPM",  "name": "JPMorgan Chase"},
    {"rank": 11, "symbol": "LLY",  "name": "Eli Lilly"},
    {"rank": 12, "symbol": "V",    "name": "Visa"},
    {"rank": 13, "symbol": "WMT",  "name": "Walmart"},
    {"rank": 14, "symbol": "XOM",  "name": "ExxonMobil"},
    {"rank": 15, "symbol": "MA",   "name": "Mastercard"},
    {"rank": 16, "symbol": "ORCL", "name": "Oracle"},
    {"rank": 17, "symbol": "COST", "name": "Costco"},
    {"rank": 18, "symbol": "JNJ",  "name": "Johnson & Johnson"},
    {"rank": 19, "symbol": "PG",   "name": "Procter & Gamble"},
    {"rank": 20, "symbol": "NFLX", "name": "Netflix"},
    {"rank": 21, "symbol": "HD",   "name": "Home Depot"},
    {"rank": 22, "symbol": "BAC",  "name": "Bank of America"},
    {"rank": 23, "symbol": "ABBV", "name": "AbbVie"},
    {"rank": 24, "symbol": "CRM",  "name": "Salesforce"},
    {"rank": 25, "symbol": "CVX",  "name": "Chevron"},
    {"rank": 26, "symbol": "KO",   "name": "Coca-Cola"},
    {"rank": 27, "symbol": "AMD",  "name": "AMD"},
    {"rank": 28, "symbol": "ADBE", "name": "Adobe"},
    {"rank": 29, "symbol": "MRK",  "name": "Merck"},
    {"rank": 30, "symbol": "PEP",  "name": "PepsiCo"},
]

# --- Metals (futures + ETFs) ---------------------------------------------------

METALS_UNIVERSE: list[dict] = [
    {"rank": 1, "symbol": "GC=F",  "name": "Gold (futures)"},
    {"rank": 2, "symbol": "SI=F",  "name": "Silver (futures)"},
    {"rank": 3, "symbol": "PL=F",  "name": "Platinum (futures)"},
    {"rank": 4, "symbol": "PA=F",  "name": "Palladium (futures)"},
    {"rank": 5, "symbol": "HG=F",  "name": "Copper (futures)"},
    {"rank": 6, "symbol": "GLD",   "name": "SPDR Gold Shares ETF"},
    {"rank": 7, "symbol": "SLV",   "name": "iShares Silver Trust ETF"},
    {"rank": 8, "symbol": "CPER",  "name": "United States Copper Index Fund"},
    {"rank": 9, "symbol": "LIT",   "name": "Global X Lithium ETF"},
    {"rank": 10, "symbol": "SLX",  "name": "VanEck Steel ETF"},
    {"rank": 11, "symbol": "URA",  "name": "Global X Uranium ETF"},
    {"rank": 12, "symbol": "PICK", "name": "iShares Metals & Mining ETF"},
]

# --- Commodities (energy / grains / softs / livestock) -------------------------

COMMODITIES_UNIVERSE: list[dict] = [
    # Energy
    {"rank": 1,  "symbol": "CL=F", "name": "WTI Crude Oil"},
    {"rank": 2,  "symbol": "BZ=F", "name": "Brent Crude Oil"},
    {"rank": 3,  "symbol": "NG=F", "name": "Natural Gas"},
    {"rank": 4,  "symbol": "RB=F", "name": "RBOB Gasoline"},
    {"rank": 5,  "symbol": "HO=F", "name": "Heating Oil"},
    # Grains
    {"rank": 6,  "symbol": "ZW=F", "name": "Wheat"},
    {"rank": 7,  "symbol": "ZC=F", "name": "Corn"},
    {"rank": 8,  "symbol": "ZS=F", "name": "Soybeans"},
    {"rank": 9,  "symbol": "ZL=F", "name": "Soybean Oil"},
    # Softs
    {"rank": 10, "symbol": "KC=F", "name": "Coffee"},
    {"rank": 11, "symbol": "SB=F", "name": "Sugar"},
    {"rank": 12, "symbol": "CT=F", "name": "Cotton"},
    {"rank": 13, "symbol": "CC=F", "name": "Cocoa"},
    {"rank": 14, "symbol": "OJ=F", "name": "Orange Juice"},
    # Livestock
    {"rank": 15, "symbol": "LE=F", "name": "Live Cattle"},
    {"rank": 16, "symbol": "HE=F", "name": "Lean Hogs"},
]


@dataclass
class AssetClass:
    key: str          # used in cache and state file names
    label: str        # tab label
    description: str
    universe: list[dict]
    resolver_factory: Callable[[], Resolver]
    interval_options: list[tuple[str, int]]
    default_interval_idx: int = 0
    tv_default_prefix: str = ""  # leaves TV to autoresolve when empty
    is_24_7: bool = True  # crypto is 24/7; stocks/futures aren't (affects cache freshness)


CRYPTO = AssetClass(
    key="crypto",
    label="Crypto",
    description="Top 100 by market cap on Binance / Gate.io / Kraken.",
    universe=crypto_universe(),
    resolver_factory=default_resolver,
    interval_options=[("1 day", 1440), ("4 hour", 240), ("1 hour", 60)],
    default_interval_idx=0,
    tv_default_prefix="BINANCE",
    is_24_7=True,
)

STOCKS = AssetClass(
    key="stocks",
    label="Stocks",
    description="Top 30 US megacap stocks via Yahoo Finance.",
    universe=STOCKS_UNIVERSE,
    resolver_factory=yahoo_resolver,
    interval_options=[("1 day", 1440), ("1 week", 10080)],
    default_interval_idx=0,
    tv_default_prefix="",  # TV auto-resolves common tickers
    is_24_7=False,
)

METALS = AssetClass(
    key="metals",
    label="Metals",
    description="Precious + industrial metals (futures + ETFs) via Yahoo Finance.",
    universe=METALS_UNIVERSE,
    resolver_factory=yahoo_resolver,
    interval_options=[("1 day", 1440), ("1 week", 10080)],
    default_interval_idx=0,
    tv_default_prefix="",
    is_24_7=False,
)

COMMODITIES = AssetClass(
    key="commodities",
    label="Commodities",
    description="Energy / grains / softs / livestock futures via Yahoo Finance.",
    universe=COMMODITIES_UNIVERSE,
    resolver_factory=yahoo_resolver,
    interval_options=[("1 day", 1440), ("1 week", 10080)],
    default_interval_idx=0,
    tv_default_prefix="",
    is_24_7=False,
)


ASSET_CLASSES: list[AssetClass] = [CRYPTO, STOCKS, METALS, COMMODITIES]
