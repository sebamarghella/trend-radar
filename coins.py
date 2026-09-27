"""Crypto universe: live top-100 by market cap (CoinGecko), with a static fallback.

`live_universe()` is what the radar scans. It pulls the current CoinGecko
ranking once a day (disk-cached), drops stablecoins / tokenized funds, and keeps
the first `target` coins that at least one of our exchanges can supply — the
same "top N tradable" rule Signum's radar uses. `TOP_100` below is only the
fallback for when CoinGecko is unreachable and there's no cached copy.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable

import requests

# Top 100 by market cap (snapshot from CoinGecko, ranks may shift over time).
# The Streamlit app filters this list against Binance's live tradable symbols at runtime.
TOP_100: list[dict] = [
    {"rank": 1, "symbol": "BTC", "name": "Bitcoin"},
    {"rank": 2, "symbol": "ETH", "name": "Ethereum"},
    {"rank": 3, "symbol": "USDT", "name": "Tether"},
    {"rank": 4, "symbol": "BNB", "name": "BNB"},
    {"rank": 5, "symbol": "XRP", "name": "XRP"},
    {"rank": 6, "symbol": "USDC", "name": "USDC"},
    {"rank": 7, "symbol": "SOL", "name": "Solana"},
    {"rank": 8, "symbol": "TRX", "name": "TRON"},
    {"rank": 9, "symbol": "FIGR_HELOC", "name": "Figure Heloc"},
    {"rank": 10, "symbol": "HYPE", "name": "Hyperliquid"},
    {"rank": 11, "symbol": "DOGE", "name": "Dogecoin"},
    {"rank": 12, "symbol": "USDS", "name": "USDS"},
    {"rank": 13, "symbol": "ZEC", "name": "Zcash"},
    {"rank": 14, "symbol": "LEO", "name": "LEO Token"},
    {"rank": 15, "symbol": "RAIN", "name": "Rain"},
    {"rank": 16, "symbol": "ADA", "name": "Cardano"},
    {"rank": 17, "symbol": "XLM", "name": "Stellar"},
    {"rank": 18, "symbol": "XMR", "name": "Monero"},
    {"rank": 19, "symbol": "LINK", "name": "Chainlink"},
    {"rank": 20, "symbol": "LAB", "name": "LAB"},
    {"rank": 21, "symbol": "WBT", "name": "WhiteBIT Coin"},
    {"rank": 22, "symbol": "CC", "name": "Canton"},
    {"rank": 23, "symbol": "BCH", "name": "Bitcoin Cash"},
    {"rank": 24, "symbol": "GRAM", "name": "Gram (prev. Toncoin)"},
    {"rank": 25, "symbol": "USD1", "name": "USD1"},
    {"rank": 26, "symbol": "USDE", "name": "Ethena USDe"},
    {"rank": 27, "symbol": "DAI", "name": "Dai"},
    {"rank": 28, "symbol": "M", "name": "MemeCore"},
    {"rank": 29, "symbol": "HBAR", "name": "Hedera"},
    {"rank": 30, "symbol": "LTC", "name": "Litecoin"},
    {"rank": 31, "symbol": "AVAX", "name": "Avalanche"},
    {"rank": 32, "symbol": "NEAR", "name": "NEAR Protocol"},
    {"rank": 33, "symbol": "SUI", "name": "Sui"},
    {"rank": 34, "symbol": "SHIB", "name": "Shiba Inu"},
    {"rank": 35, "symbol": "PYUSD", "name": "PayPal USD"},
    {"rank": 36, "symbol": "USYC", "name": "Circle USYC"},
    {"rank": 37, "symbol": "CRO", "name": "Cronos"},
    {"rank": 38, "symbol": "XAUT", "name": "Tether Gold"},
    {"rank": 39, "symbol": "USDG", "name": "Global Dollar"},
    {"rank": 40, "symbol": "BUIDL", "name": "BlackRock USD Institutional Digital Liquidity"},
    {"rank": 41, "symbol": "TAO", "name": "Bittensor"},
    {"rank": 42, "symbol": "USDY", "name": "Ondo US Dollar Yield"},
    {"rank": 43, "symbol": "PAXG", "name": "PAX Gold"},
    {"rank": 44, "symbol": "MNT", "name": "Mantle"},
    {"rank": 45, "symbol": "WLFI", "name": "World Liberty Financial"},
    {"rank": 46, "symbol": "DOT", "name": "Polkadot"},
    {"rank": 47, "symbol": "RLUSD", "name": "Ripple USD"},
    {"rank": 48, "symbol": "OKB", "name": "OKB"},
    {"rank": 49, "symbol": "UNI", "name": "Uniswap"},
    {"rank": 50, "symbol": "ONDO", "name": "Ondo"},
    {"rank": 51, "symbol": "ASTER", "name": "Aster"},
    {"rank": 52, "symbol": "USDF", "name": "Falcon USD"},
    {"rank": 53, "symbol": "ICP", "name": "Internet Computer"},
    {"rank": 54, "symbol": "HTX", "name": "HTX DAO"},
    {"rank": 55, "symbol": "SKY", "name": "Sky"},
    {"rank": 56, "symbol": "PI", "name": "Pi Network"},
    {"rank": 57, "symbol": "USDD", "name": "USDD"},
    {"rank": 58, "symbol": "BGB", "name": "Bitget Token"},
    {"rank": 59, "symbol": "WLD", "name": "Worldcoin"},
    {"rank": 60, "symbol": "PEPE", "name": "Pepe"},
    {"rank": 61, "symbol": "BFUSD", "name": "BFUSD"},
    {"rank": 62, "symbol": "MORPHO", "name": "Morpho"},
    {"rank": 63, "symbol": "ETC", "name": "Ethereum Classic"},
    {"rank": 64, "symbol": "H", "name": "Humanity"},
    {"rank": 65, "symbol": "AAVE", "name": "Aave"},
    {"rank": 66, "symbol": "RENDER", "name": "Render"},
    {"rank": 67, "symbol": "QNT", "name": "Quant"},
    {"rank": 68, "symbol": "USDTB", "name": "USDtb"},
    {"rank": 69, "symbol": "ALGO", "name": "Algorand"},
    {"rank": 70, "symbol": "KCS", "name": "KuCoin"},
    {"rank": 71, "symbol": "EUTBL", "name": "Spiko EU T-Bills"},
    {"rank": 72, "symbol": "POL", "name": "POL (ex-MATIC)"},
    {"rank": 73, "symbol": "BCAP", "name": "Blockchain Capital"},
    {"rank": 74, "symbol": "U", "name": "United Stables"},
    {"rank": 75, "symbol": "ATOM", "name": "Cosmos Hub"},
    {"rank": 76, "symbol": "USTB", "name": "Superstate Short Duration"},
    {"rank": 77, "symbol": "JTRSY", "name": "Janus Henderson Anemoy Treasury"},
    {"rank": 78, "symbol": "VVV", "name": "Venice Token"},
    {"rank": 79, "symbol": "DEXE", "name": "DeXe"},
    {"rank": 80, "symbol": "STABLE", "name": "Stable"},
    {"rank": 81, "symbol": "JST", "name": "JUST"},
    {"rank": 82, "symbol": "NEXO", "name": "NEXO"},
    {"rank": 83, "symbol": "KAS", "name": "Kaspa"},
    {"rank": 84, "symbol": "ENA", "name": "Ethena"},
    {"rank": 85, "symbol": "GT", "name": "Gate"},
    {"rank": 86, "symbol": "FIL", "name": "Filecoin"},
    {"rank": 87, "symbol": "APT", "name": "Aptos"},
    {"rank": 88, "symbol": "INJ", "name": "Injective"},
    {"rank": 89, "symbol": "JUP", "name": "Jupiter"},
    {"rank": 90, "symbol": "XDC", "name": "XDC Network"},
    {"rank": 91, "symbol": "NIGHT", "name": "Midnight"},
    {"rank": 92, "symbol": "FLR", "name": "Flare"},
    {"rank": 93, "symbol": "BNLIFE", "name": "Binance Life"},
    {"rank": 94, "symbol": "BDX", "name": "Beldex"},
    {"rank": 95, "symbol": "PUMP", "name": "Pump.fun"},
    {"rank": 96, "symbol": "FET", "name": "Artificial Superintelligence Alliance"},
    {"rank": 97, "symbol": "ARB", "name": "Arbitrum"},
    {"rank": 98, "symbol": "GHO", "name": "GHO"},
    {"rank": 99, "symbol": "HASH", "name": "Provenance Blockchain"},
    {"rank": 100, "symbol": "USD0", "name": "Usual USD"},
]

# Stablecoins + tokenized RWAs + wrapped fiat — no meaningful trend signal.
# NOTE: gold tokens (PAXG, XAUT) are intentionally NOT excluded — they trend and
# Signum lists them. Only pegged/peg-like assets are dropped here. CoinGecko's
# "stablecoins" category catches most new ones automatically; this list is the
# belt-and-braces for tokenized funds, which have no category of their own.
EXCLUDED_SYMBOLS = {
    "USDT", "USDC", "DAI", "USDS", "PYUSD", "USDE", "USD1", "USDG", "USDY",
    "USDF", "USDD", "BFUSD", "USDTB", "USTB", "RLUSD", "GHO", "USD0",
    "USDGO", "TUSD", "YLDS", "EURC", "A7A5",
    "BUIDL", "EUTBL", "JTRSY", "USYC", "BCAP", "U", "EURSAFO", "JAAA",
    "FIGR_HELOC", "HASH",
}
# Pegged-looking assets we still want (price near $1 is coincidence, not a peg).
PEG_HEURISTIC_KEEP = {"PAXG", "XAUT"}

COINGECKO_MARKETS_URL = "https://api.coingecko.com/api/v3/coins/markets"
UNIVERSE_CACHE = Path(__file__).parent / ".cache" / "universe.json"
UNIVERSE_TTL_S = 24 * 3600
# 120 rather than 100: CoinGecko ranks mid-caps differently from Signum's source
# (JTO is #114 there, #141 here). 120 tradable ≈ covers their whole top 100.
UNIVERSE_TARGET = 120


def tradable_universe() -> list[dict]:
    """Static fallback: snapshot top 100 minus stablecoins/RWAs."""
    return [c for c in TOP_100 if c["symbol"] not in EXCLUDED_SYMBOLS]


def _coingecko(params: dict, attempts: int = 3) -> list[dict]:
    """GET /coins/markets with backoff — the free tier 429s under burst load."""
    last: Exception | None = None
    for i in range(attempts):
        try:
            r = requests.get(COINGECKO_MARKETS_URL, params=params, timeout=20)
            if r.status_code == 429:
                raise requests.HTTPError("429 rate limited")
            r.raise_for_status()
            data = r.json()
            if isinstance(data, list):
                return data
            raise requests.HTTPError(f"unexpected payload: {str(data)[:120]}")
        except requests.RequestException as e:
            last = e
            time.sleep(4 * (i + 1))
    raise RuntimeError(f"CoinGecko failed: {last}")


def _looks_pegged(c: dict) -> bool:
    price = c.get("current_price") or 0
    chg = abs(c.get("price_change_percentage_24h") or 0)
    return 0.98 <= price <= 1.02 and chg < 0.3


def fetch_ranked_coins(pool: int = 250) -> list[dict]:
    """Current market-cap ranking from CoinGecko, stablecoins removed."""
    base = {"vs_currency": "usd", "order": "market_cap_desc", "per_page": pool, "page": 1}
    markets = _coingecko(base)
    try:
        stable_ids = {c["id"] for c in _coingecko({**base, "category": "stablecoins"})}
    except RuntimeError as e:
        print(f"[warn] CoinGecko stablecoin category unavailable ({e}); using symbol list + peg heuristic")
        stable_ids = set()
    out: list[dict] = []
    seen: set[str] = set()
    for c in markets:
        sym = str(c.get("symbol", "")).upper()
        rank = c.get("market_cap_rank")
        if not sym or rank is None or sym in seen:
            continue
        seen.add(sym)  # duplicate tickers: keep the higher-ranked coin
        if c["id"] in stable_ids or sym in EXCLUDED_SYMBOLS:
            continue
        if sym not in PEG_HEURISTIC_KEEP and _looks_pegged(c):
            continue
        out.append({"rank": int(rank), "symbol": sym, "name": c.get("name", sym)})
    return sorted(out, key=lambda c: c["rank"])


def _read_cache() -> tuple[float, list[dict]] | None:
    try:
        payload = json.loads(UNIVERSE_CACHE.read_text(encoding="utf-8"))
        return float(payload["fetched_at"]), list(payload["coins"])
    except Exception:
        return None


def ranked_coins() -> tuple[list[dict], str]:
    """(coins, provenance). Fresh cache → CoinGecko → stale cache → static snapshot."""
    cached = _read_cache()
    if cached and time.time() - cached[0] < UNIVERSE_TTL_S:
        return cached[1], "coingecko (cached)"
    try:
        coins = fetch_ranked_coins()
        try:
            UNIVERSE_CACHE.parent.mkdir(parents=True, exist_ok=True)
            UNIVERSE_CACHE.write_text(
                json.dumps({"fetched_at": time.time(), "coins": coins}), encoding="utf-8",
            )
        except OSError:
            pass
        return coins, "coingecko"
    except RuntimeError as e:
        print(f"[warn] {e}")
        if cached:
            return cached[1], "coingecko (stale cache)"
        return tradable_universe(), "static snapshot"


def live_universe(
    is_resolvable: Callable[[str], bool] | None = None,
    target: int = UNIVERSE_TARGET,
) -> list[dict]:
    """Top `target` coins by market cap that at least one exchange can supply."""
    coins, provenance = ranked_coins()
    if is_resolvable is not None:
        coins = [c for c in coins if is_resolvable(c["symbol"])]
    picked = coins[:target]
    print(f"[universe] {len(picked)} coins from {provenance}"
          + (f" (down to rank {picked[-1]['rank']})" if picked else ""))
    return picked
