"""Trend Radar — GaussianChannel Strategy v3.1 across multiple asset classes.

Run:
    streamlit run app.py
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import altair as alt
import pandas as pd
import streamlit as st
from st_aggrid import AgGrid, GridOptionsBuilder, GridUpdateMode
from st_aggrid.shared import JsCode

import json

import alerts
import cache as ohlc_cache
import strategies as strat_registry
from asset_classes import ASSET_CLASSES, AssetClass
from gaussian_channel import BAR_COLORS, compute_stats
from sources import Resolver, SourceError
from strategies import Strategy


st.set_page_config(
    page_title="Trend Radar — GaussianChannel v3.1",
    layout="wide",
    initial_sidebar_state="expanded",
)


# --- Global sidebar (shared across all tabs) -----------------------------------

# Reserve a slot at the top of the sidebar for the alerts feed; we fill it
# after the tabs render so any flips this rerun show up immediately.
_alerts_slot = st.sidebar.container()

st.sidebar.header("Strategies")
st.sidebar.caption(
    "Each tab picks its own strategy. Edit params inline on a tab, or upload a "
    "preset JSON here. Format: `{name, logic_key, params}`."
)
_uploaded = st.sidebar.file_uploader("Upload strategy JSON", type=["json"], key="strat_upload")
if _uploaded is not None:
    try:
        _d = json.load(_uploaded)
        _strat = strat_registry.parse_strategy_dict(_d)
        strat_registry.save_strategy(_strat)
        st.sidebar.success(f"Added '{_strat.name}'. Pick it in a tab's Strategy dropdown.")
    except Exception as e:  # noqa: BLE001
        st.sidebar.error(f"Invalid strategy JSON: {e}")

# Downloadable template so users know the schema.
_template = json.dumps(
    strat_registry.Strategy(
        "My strategy", "gaussian_channel_v3_1",
        strat_registry.LOGICS["gaussian_channel_v3_1"].defaults(),
    ).to_dict(),
    indent=2,
)
st.sidebar.download_button("Download template JSON", _template, file_name="strategy_template.json")


def _github_secrets() -> tuple[str, str, str]:
    try:
        gh = st.secrets.get("github", {})
        return str(gh.get("token", "")), str(gh.get("repo", "")), str(gh.get("branch", "") or "main")
    except Exception:
        return "", "", "main"


_gh_token, _gh_repo, _gh_branch = _github_secrets()
if _gh_token and _gh_repo:
    if st.sidebar.button("⬆ Commit presets to repo", help="Push saved presets + per-class assignments to GitHub so they persist across restarts and drive the alert cron."):
        import repo_sync
        with st.spinner("Committing presets to GitHub…"):
            res = repo_sync.sync_presets(
                _gh_repo, _gh_token, _gh_branch,
                strat_registry.STRATEGIES_DIR, strat_registry.ASSIGNMENTS_FILE,
            )
        n = len(res["created"]) + len(res["updated"]) + len(res["deleted"])
        if res["errors"]:
            st.sidebar.error("Commit errors: " + "; ".join(res["errors"][:3]))
        elif n == 0:
            st.sidebar.info("Nothing to commit — repo already up to date.")
        else:
            st.sidebar.success(
                f"Committed {n} change(s) to {_gh_repo}. Presets now persist "
                "(the app may briefly redeploy)."
            )
    st.sidebar.caption(
        "Saved presets persist for this session. Click **Commit presets** to push "
        "them to the repo (permanent + used by alerts)."
    )
else:
    st.sidebar.caption(
        "On Streamlit Cloud, uploads/saves last for the session. Add a GitHub "
        "token in secrets (`[github] token, repo`) to enable a **Commit presets** "
        "button. For now, commit `strategies/*.json` to the repo manually."
    )

st.sidebar.header("Backtest")
lookback_days = st.sidebar.slider(
    "Lookback (days)", min_value=30, max_value=365, value=180, step=15,
    help="Window for trade count / net % / win % in the radar grid.",
)

st.sidebar.header("Layout")
table_width_pct = st.sidebar.slider(
    "Table width", min_value=25, max_value=100, value=50, step=5, format="%d%%",
    help="Width of the radar table vs the drilldown chart. 100% hides the chart "
    "and shows the table full-width.",
)
grid_height = st.sidebar.slider(
    "Table height", min_value=300, max_value=1000, value=620, step=20, format="%d px",
    help="Vertical size of the radar grid.",
)

st.sidebar.header("Telegram alerts")


def _load_telegram_secrets() -> tuple[str, str]:
    try:
        tg = st.secrets.get("telegram", {})
        return str(tg.get("bot_token", "")), str(tg.get("chat_id", ""))
    except Exception:
        return "", ""


_secret_token, _secret_chat = _load_telegram_secrets()
_has_server_secrets = bool(_secret_token and _secret_chat)

if _has_server_secrets:
    st.sidebar.success("Telegram configured from server secrets")
    bot_token, chat_id = _secret_token, _secret_chat
    alerts_enabled = st.sidebar.checkbox(
        "Fire on state flips", value=True,
        help="Sends a Telegram message when any coin flips FLAT↔LONG.",
    )
else:
    alerts_enabled = st.sidebar.checkbox(
        "Fire on state flips", value=False,
        help="Sends a Telegram message when any coin flips FLAT↔LONG.",
    )
    bot_token = st.sidebar.text_input("Bot token", type="password")
    chat_id = st.sidebar.text_input("Chat ID")
    st.sidebar.caption(
        "Persist by saving to `.streamlit/secrets.toml` "
        "(local) or the Secrets panel (Streamlit Cloud)."
    )

if st.sidebar.button("Send test alert", help="Verify your token + chat ID"):
    ok, err = alerts.send_telegram(bot_token, chat_id, "Trend Radar: test alert ✅")
    if ok:
        st.sidebar.success("Telegram OK")
    else:
        st.sidebar.error(f"Telegram failed: {err}")

if st.sidebar.button("Wipe disk cache", help="Delete cached OHLC files"):
    n = ohlc_cache.clear()
    st.sidebar.success(f"Removed {n} cache file(s).")
    st.cache_data.clear()


# --- Data fetch ----------------------------------------------------------------


@st.cache_resource(ttl=15 * 60, show_spinner=False)
def cached_resolver(asset_key: str) -> Resolver:
    """One resolver per asset class — Yahoo for stocks/metals/commodities,
    multi-source for crypto. Cached so each tab visit reuses the same instance."""
    for ac in ASSET_CLASSES:
        if ac.key == asset_key:
            return ac.resolver_factory()
    raise ValueError(f"unknown asset class {asset_key}")


def fetch_one(base: str, resolver: Resolver, interval: int, force_refresh: bool = False) -> dict:
    hit = resolver.resolve(base)
    if hit is None:
        return {"symbol": base, "ok": False, "reason": "not on any source"}
    src, resolved = hit
    cached_df = None
    if not force_refresh:
        cached_df = ohlc_cache.load(src.name, resolved, interval)
        if cached_df is not None and ohlc_cache.is_fresh(cached_df, interval):
            return {
                "symbol": base, "ok": True, "pair": resolved,
                "exchange": src.name, "exchange_short": src.short, "tv_prefix": src.tv_prefix,
                "df": cached_df, "cache_status": "cache",
            }
    try:
        df = src.fetch_with_retry(resolved, interval_minutes=interval)
    except SourceError as e:
        if cached_df is not None:
            return {
                "symbol": base, "ok": True, "pair": resolved,
                "exchange": src.name, "exchange_short": src.short, "tv_prefix": src.tv_prefix,
                "df": cached_df, "cache_status": "stale",
            }
        return {"symbol": base, "ok": False, "reason": f"{src.name}: {e}"}
    if len(df) < 60:
        return {"symbol": base, "ok": False, "reason": f"insufficient history on {src.name}"}
    ohlc_cache.save(src.name, resolved, interval, df)
    return {
        "symbol": base, "ok": True, "pair": resolved,
        "exchange": src.name, "exchange_short": src.short, "tv_prefix": src.tv_prefix,
        "df": df, "cache_status": "fresh",
    }


@st.cache_data(ttl=15 * 60, show_spinner=False)
def load_universe_data(
    asset_key: str, interval: int, bust: int, force_refresh: bool,
) -> tuple[list[dict], list[dict]]:
    ac = next(a for a in ASSET_CLASSES if a.key == asset_key)
    resolver = cached_resolver(asset_key)
    universe = ac.universe
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=20) as ex:
        futures = {
            ex.submit(fetch_one, c["symbol"], resolver, interval, force_refresh): c
            for c in universe
        }
        progress = st.progress(0.0, text=f"Loading {ac.label.lower()} candles…")
        done = 0
        cache_hits = 0
        for fut in as_completed(futures):
            meta = futures[fut]
            res = fut.result()
            res["rank"] = meta["rank"]
            res["name"] = meta["name"]
            if res.get("cache_status") == "cache":
                cache_hits += 1
            results.append(res)
            done += 1
            suffix = (
                f"({cache_hits} from cache)"
                if cache_hits or done < 5
                else "(bar just rolled — refetching all)"
            )
            progress.progress(
                done / len(universe),
                text=f"Loaded {done}/{len(universe)} {suffix}",
            )
        progress.empty()
    ok = [r for r in results if r["ok"]]
    skipped = [r for r in results if not r["ok"]]
    return ok, skipped


def compute_signal(row: dict, strategy: Strategy, lookback_days_: int) -> dict:
    df = row["df"]
    result = strat_registry.run_strategy(strategy, df)
    snap = result.snapshot
    stats = compute_stats(result.trades, now=df.index[-1], lookback_days=lookback_days_)
    return {
        "rank": row["rank"],
        "symbol": row["symbol"],
        "name": row["name"],
        "pair": row["pair"],
        "exchange": row["exchange"],
        "exchange_short": row["exchange_short"],
        "tv_prefix": row["tv_prefix"],
        "state": "LONG" if snap.in_position else "FLAT",
        "bar_color": snap.bar_color,
        "filter_up": snap.filter_up,
        "bars_in_state": snap.bars_in_state,
        "close_vs_hband_pct": snap.close_vs_hband_pct,
        "stoch_k": snap.stoch_k,
        "last_close": snap.last_close,
        "trades": stats.trades,
        "net_pct": stats.net_pct,
        "win_pct": stats.win_pct,
        "_df": df,
        "_overlays": result.overlays,
        "_state_series": result.state_series,
    }


# --- Bar cycle helpers ---------------------------------------------------------


def _bar_cycle(interval_minutes_: int) -> tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp]:
    now_ = pd.Timestamp.now(tz="UTC")
    sec_per_bar = interval_minutes_ * 60
    epoch = int(now_.timestamp())
    current_open = pd.Timestamp((epoch // sec_per_bar) * sec_per_bar, unit="s", tz="UTC")
    next_open = current_open + pd.Timedelta(minutes=interval_minutes_)
    return now_, current_open, next_open


def _fmt_hm(td: pd.Timedelta) -> str:
    total = int(td.total_seconds())
    hours, rem = divmod(max(total, 0), 3600)
    minutes = rem // 60
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"


# --- AgGrid styling (shared across all tabs) -----------------------------------


_CELLSTYLE_STATE = JsCode("""
function(p) {
    if (p.value === 'LONG') {
        return { backgroundColor: '#0aff68', color: 'black', fontWeight: 700 };
    }
    return { backgroundColor: '#2b2b2b', color: '#aaaaaa' };
}
""")

_BAR_COLORS_JSON = ",".join(f"'{k}':'{v}'" for k, v in BAR_COLORS.items())
_CELLSTYLE_BAR = JsCode(f"""
function(p) {{
    const colors = {{{_BAR_COLORS_JSON}}};
    return {{ backgroundColor: colors[p.value] || '#cccccc', color: 'black', fontWeight: 600 }};
}}
""")

_CELLSTYLE_FILTER = JsCode("""
function(p) {
    if (p.value === true) return { color: '#0aff68', fontWeight: 600 };
    return { color: '#ff0a5a', fontWeight: 600 };
}
""")

_CELLSTYLE_PCT = JsCode("""
function(p) {
    if (p.value == null) return {};
    return p.value > 0 ? { color: '#00752d' } : { color: '#ff0a5a' };
}
""")

_FMT_PCT = JsCode("function(p) { return p.value != null ? (p.value >= 0 ? '+' : '') + p.value.toFixed(2) + '%' : ''; }")
_FMT_K = JsCode("function(p) { return p.value != null ? p.value.toFixed(1) : ''; }")
_FMT_PRICE = JsCode("function(p) { return p.value != null ? p.value.toPrecision(6) : ''; }")
_FMT_INT = JsCode("function(p) { return p.value != null ? p.value.toFixed(0) : ''; }")
_FMT_WIN = JsCode("function(p) { return p.value != null ? p.value.toFixed(0) + '%' : '—'; }")

TV_CHART_ID = "6O2rb5Ql"

_TV_VALUE_FMT = JsCode("function(p) { return p.value ? 'TV ↗' : ''; }")

_TV_CELL_STYLE = {
    "cursor": "pointer",
    "color": "#00752d",
    "fontWeight": "600",
    "textAlign": "center",
}

# Open the user's saved TV chart for the clicked coin. For crypto, the exchange
# prefix (BINANCE/GATEIO/KRAKEN) comes from the row. For non-crypto, the
# tv_prefix is empty and TradingView resolves the ticker itself (NASDAQ/NYSE/
# COMEX/NYMEX) — works for most US tickers, may fail for some futures.
_TV_CLICK_HANDLER = JsCode(f"""
function(event) {{
    if (event.colDef.field === 'tv' && event.data && event.data.pair) {{
        const cleanedPair = event.data.pair.replace(/_/g, '');
        const prefix = event.data.tv_prefix || '';
        const symbolPart = prefix ? (prefix + '%3A' + cleanedPair) : cleanedPair;
        const url = 'https://www.tradingview.com/chart/{TV_CHART_ID}/?symbol=' + symbolPart;
        window.open(url, '_blank', 'noopener,noreferrer');
    }}
}}
""")


# Per-column width as a percentage of the table. AgGrid has no literal percent
# unit, but `flex` weights distribute width proportionally — so weights that sum
# to 100 make each column occupy that % of the pane. No maxWidth anywhere, so
# the grid always fills 100% width; minWidth is just a readability floor that
# triggers horizontal scroll only when the pane gets very narrow.
COLUMN_FLEX = {
    "rank": 3, "symbol": 6, "name": 12, "exchange_short": 5, "pair": 8,
    "state": 6, "bar_color": 9, "filter_up": 4, "bars_in_state": 5,
    "close_vs_hband_pct": 7, "stoch_k": 5, "last_close": 7, "trades": 6,
    "net_pct": 7, "win_pct": 6, "tv": 4,
}  # sums to 100

assert sum(COLUMN_FLEX.values()) == 100, "column flex weights must sum to 100"


def build_grid_options(df: pd.DataFrame) -> dict:
    gb = GridOptionsBuilder.from_dataframe(df)
    gb.configure_default_column(resizable=True, sortable=True, filterable=False)
    gb.configure_selection(selection_mode="single", use_checkbox=False, suppressRowDeselection=True)
    F = COLUMN_FLEX
    gb.configure_column("rank", header_name="#", flex=F["rank"], minWidth=40, type=["numericColumn"])
    gb.configure_column("symbol", header_name="Sym", flex=F["symbol"], minWidth=55)
    gb.configure_column("name", header_name="Name", flex=F["name"], minWidth=90)
    gb.configure_column("exchange_short", header_name="Src", flex=F["exchange_short"], minWidth=45)
    gb.configure_column("pair", header_name="Pair", flex=F["pair"], minWidth=65)
    gb.configure_column("exchange", hide=True)
    gb.configure_column("tv_prefix", hide=True)
    gb.configure_column("state", header_name="Pos", flex=F["state"], minWidth=55, cellStyle=_CELLSTYLE_STATE)
    gb.configure_column("bar_color", header_name="Bar", flex=F["bar_color"], minWidth=80, cellStyle=_CELLSTYLE_BAR)
    gb.configure_column("filter_up", header_name="F↑", flex=F["filter_up"], minWidth=40, cellStyle=_CELLSTYLE_FILTER)
    gb.configure_column("bars_in_state", header_name="Bars", flex=F["bars_in_state"], minWidth=45, type=["numericColumn"])
    gb.configure_column(
        "close_vs_hband_pct", header_name="vs HB", flex=F["close_vs_hband_pct"], minWidth=60,
        type=["numericColumn"], valueFormatter=_FMT_PCT, cellStyle=_CELLSTYLE_PCT,
    )
    gb.configure_column("stoch_k", header_name="StK", flex=F["stoch_k"], minWidth=45, type=["numericColumn"], valueFormatter=_FMT_K)
    gb.configure_column("last_close", header_name="Close", flex=F["last_close"], minWidth=60, type=["numericColumn"], valueFormatter=_FMT_PRICE)
    gb.configure_column("trades", header_name="Trades", flex=F["trades"], minWidth=50, type=["numericColumn"], valueFormatter=_FMT_INT)
    gb.configure_column(
        "net_pct", header_name="Net %", flex=F["net_pct"], minWidth=60,
        type=["numericColumn"], valueFormatter=_FMT_PCT, cellStyle=_CELLSTYLE_PCT,
    )
    gb.configure_column("win_pct", header_name="Win %", flex=F["win_pct"], minWidth=50, type=["numericColumn"], valueFormatter=_FMT_WIN)
    gb.configure_column(
        "tv", header_name="TV", flex=F["tv"], minWidth=45,
        sortable=False, filter=False,
        valueFormatter=_TV_VALUE_FMT, cellStyle=_TV_CELL_STYLE,
    )
    gb.configure_grid_options(onCellClicked=_TV_CLICK_HANDLER)
    return gb.build()


# --- Per-tab render ------------------------------------------------------------


# First key is the default selection on load.
SORT_MAP = {
    "State (long first)": ("state", False),  # LONG before FLAT (desc)
    "Rank": ("rank", True),
    "Bars in state": ("bars_in_state", False),
    "Stoch K": ("stoch_k", False),
    "Close vs HBand %": ("close_vs_hband_pct", False),
    "Net %": ("net_pct", False),
    "Win %": ("win_pct", False),
    "Trades": ("trades", False),
}


def _render_strategy_editor(
    ac_key: str, logic_key: str, base: Strategy, preset_widget_key: str,
) -> Strategy:
    """Editable params for the chosen preset, plus Save / Delete. Returns a
    Strategy reflecting the live-edited param values for this session."""
    logic = strat_registry.LOGICS[logic_key]
    edited: dict = {}
    with st.expander("⚙ Strategy parameters", expanded=False):
        st.caption(logic.description)
        cols = st.columns(2)
        for i, p in enumerate(logic.param_schema):
            col = cols[i % 2]
            wkey = f"param_{ac_key}_{base.name}_{p.key}"
            baseval = base.params.get(p.key, p.default)
            if p.kind == "bool":
                edited[p.key] = col.checkbox(p.label, value=bool(baseval), key=wkey)
            elif p.kind == "int":
                edited[p.key] = col.number_input(
                    p.label, value=int(baseval),
                    min_value=int(p.min) if p.min is not None else None,
                    max_value=int(p.max) if p.max is not None else None,
                    step=int(p.step or 1), key=wkey,
                )
            else:  # float
                edited[p.key] = col.number_input(
                    p.label, value=float(baseval),
                    min_value=float(p.min) if p.min is not None else None,
                    max_value=float(p.max) if p.max is not None else None,
                    step=float(p.step or 0.1), format="%.4f", key=wkey,
                )

        st.divider()
        name_col, save_col = st.columns([2, 1])
        new_name = name_col.text_input(
            "Save current params as a new preset", value="",
            key=f"newpreset_{ac_key}", placeholder="e.g. Crypto 4h aggressive",
        )
        save_col.write("")
        if save_col.button("💾 Save preset", key=f"savepreset_{ac_key}"):
            nm = new_name.strip()
            if nm:
                strat_registry.save_strategy(Strategy(nm, logic_key, edited))
                strat_registry.save_assignment(ac_key, nm)
                st.session_state[preset_widget_key] = nm  # auto-select on rerun
                st.rerun()
            else:
                st.warning("Enter a preset name first.")

        if not strat_registry.is_builtin(base.name):
            if st.button(f"🗑 Delete preset “{base.name}”", key=f"delpreset_{ac_key}"):
                strat_registry.delete_strategy(base.name)
                st.session_state.pop(preset_widget_key, None)
                strat_registry.save_assignment(ac_key, strat_registry.DEFAULT_STRATEGY_NAME)
                st.rerun()
        else:
            st.caption("Built-in presets can't be deleted. Save a copy under a new name to edit.")

    return Strategy(base.name, logic_key, edited)


def render_radar(ac: AssetClass, focus_symbol: str | None = None) -> None:
    """Render the radar UI for one asset class.
    If `focus_symbol` is set, the grid pre-selects + scrolls to that row (used
    when the user clicks an alert in the sidebar)."""
    key = ac.key
    if "bust" not in st.session_state:
        st.session_state.bust = {}
    if key not in st.session_state.bust:
        st.session_state.bust[key] = 0

    st.caption(ac.description)

    # --- Strategy (logic) + Preset selection, persisted per asset class ---
    all_strategies = strat_registry.load_strategies()
    assigned_name = strat_registry.get_assignment(key)
    assigned_strat = all_strategies.get(assigned_name)
    assigned_logic = assigned_strat.logic_key if assigned_strat else strat_registry.DEFAULT_LOGIC_KEY

    logics = strat_registry.list_logics()
    logic_keys = [k for k, _ in logics]
    logic_labels = dict(logics)

    c0, c1, c2, c3 = st.columns([3, 3, 2, 2])
    logic_idx = logic_keys.index(assigned_logic) if assigned_logic in logic_keys else 0
    chosen_logic = c0.selectbox(
        "Strategy", logic_keys, index=logic_idx,
        format_func=lambda k: logic_labels[k], key=f"logic_{key}",
    )
    presets = strat_registry.presets_for_logic(chosen_logic, all_strategies)
    preset_names = list(presets.keys())
    # Preset dropdown key is scoped to the logic so switching strategy gives a
    # clean preset list (no stale-selection error).
    preset_widget_key = f"preset_{key}_{chosen_logic}"
    preset_idx = preset_names.index(assigned_name) if assigned_name in preset_names else 0
    chosen_preset = c1.selectbox("Preset", preset_names, index=preset_idx, key=preset_widget_key)
    if chosen_preset != assigned_name:
        strat_registry.save_assignment(key, chosen_preset)

    interval_label = c2.selectbox(
        "Timeframe", options=ac.interval_options,
        format_func=lambda x: x[0], index=ac.default_interval_idx, key=f"tf_{key}",
    )
    interval_minutes = interval_label[1]
    sort_by = c3.selectbox("Sort by", list(SORT_MAP.keys()), key=f"sort_{key}")

    r1, r2, _sp = st.columns([1, 1, 8])
    soft_refresh = r1.button("Refresh", key=f"refresh_{key}", type="primary")
    hard_refresh = r2.button("Force", key=f"force_{key}", help="Ignore disk cache")

    # Assignment hint: this tab uses (logic, preset); how to make it permanent.
    if _gh_token and _gh_repo:
        _persist = "Sidebar → **⬆ Commit presets to repo** saves this choice permanently (and for alerts)."
    else:
        _persist = "Add a GitHub token in secrets to enable permanent saving (sidebar)."
    st.caption(
        f"**{ac.label}** uses **{logic_labels[chosen_logic]}** · preset "
        f"**{chosen_preset}**. {_persist}"
    )

    # Param editor (returns the strategy with live-edited params for this session)
    strategy = _render_strategy_editor(key, chosen_logic, presets[chosen_preset], preset_widget_key)

    if soft_refresh or hard_refresh:
        st.session_state.bust[key] += 1
        st.cache_data.clear()
    force_refetch = hard_refresh

    # Bar cycle context (only meaningful for 24/7 markets)
    if ac.is_24_7:
        _now, _bar_open, _bar_next = _bar_cycle(interval_minutes)
        st.caption(
            f"⏱ Current {interval_label[0]} bar: **{_bar_open.strftime('%H:%M UTC')} → "
            f"{_bar_next.strftime('%H:%M UTC')}** · "
            f"{_fmt_hm(_now - _bar_open)} in, **{_fmt_hm(_bar_next - _now)} to next rollover** · "
            f"caches refresh at the rollover."
        )
    else:
        st.caption(
            "Non-24/7 market — data updates when the underlying exchange publishes a new close. "
            "Run during your local market hours for fresh prices."
        )

    # Load
    with st.spinner(f"Loading {ac.label.lower()} data…"):
        ok_rows, skipped_rows = load_universe_data(
            key, interval_minutes, st.session_state.bust[key], force_refetch
        )

    if not ok_rows:
        st.error(f"No {ac.label.lower()} symbols resolved. Check your network and try Refresh.")
        if skipped_rows:
            st.dataframe(pd.DataFrame(skipped_rows)[["symbol", "reason"]])
        return

    signals = [compute_signal(r, strategy, lookback_days) for r in ok_rows]

    # Alert detection — per-asset-class state key prevents cross-contamination
    prev_alert_state = alerts.load_state()
    class_prefix = f"{key}|"
    interval_suffix = f"|{interval_minutes}"
    had_baseline = any(
        k.startswith(class_prefix) and k.endswith(interval_suffix)
        for k in prev_alert_state
    )
    flips, new_alert_state = alerts.detect_flips(
        signals, interval_minutes, prev_alert_state, asset_class=key,
    )
    alerts.save_state(new_alert_state)

    if not had_baseline:
        st.info(
            f"Seeded alert baseline for {len(signals)} {ac.label.lower()} symbols on this timeframe. "
            "Future flips will diff against this."
        )
    elif flips:
        # Record every real flip into the history feed (drives the sidebar list).
        alerts.record_flips(flips, asset_class=key)
        if alerts_enabled and bot_token and chat_id:
            sent, errs = alerts.fire_alerts(flips, bot_token, chat_id)
            if sent:
                st.toast(f"📨 Sent {sent} Telegram alert(s) for {ac.label}", icon="📨")
            for e in errs:
                st.warning(f"Alert failed for {e}")
        else:
            flip_summary = ", ".join(
                f"{f.symbol} {'↗' if f.direction == 'ENTRY' else '↘'}" for f in flips
            )
            st.info(f"State flips detected (alerts disabled): {flip_summary}")

    # Headline strip
    long_count = sum(1 for s in signals if s["state"] == "LONG")
    green_filter = sum(1 for s in signals if s["filter_up"])
    covered = len(signals)
    total = len(ac.universe)
    cache_hits = sum(1 for r in ok_rows if r.get("cache_status") == "cache")
    stale_hits = sum(1 for r in ok_rows if r.get("cache_status") == "stale")

    h1, h2, h3, h4 = st.columns(4)
    h1.metric("Covered", f"{covered} / {total}")
    h2.metric("In long", long_count, delta=f"{long_count / covered * 100:.0f}%")
    h3.metric("Filter rising", green_filter, delta=f"{green_filter / covered * 100:.0f}%")
    h4.metric("Timeframe", interval_label[0])

    cache_msg = f"{cache_hits}/{covered} from cache"
    if stale_hits:
        cache_msg += f" · {stale_hits} stale (source errored)"
    st.caption(cache_msg)

    # Grid + drilldown
    df = pd.DataFrame(signals)
    df_display = df.drop(columns=["_df", "_overlays", "_state_series"]).copy()
    sort_col, ascending = SORT_MAP[sort_by]
    # Tie-break by rank so each group (e.g. all LONG rows) reads top-mcap first.
    if sort_col == "rank":
        df_display = df_display.sort_values("rank", ascending=True, na_position="last")
    else:
        df_display = df_display.sort_values(
            [sort_col, "rank"], ascending=[ascending, True], na_position="last"
        )
    df_display = df_display.reset_index(drop=True)
    df_display["tv"] = df_display["pair"]

    # Layout: the sidebar "Table width" slider drives the split. At 100% the
    # chart is hidden and the table spans the full width.
    chart_hidden = table_width_pct >= 100
    if chart_hidden:
        left = st.container()
        right = None
    else:
        left, right = st.columns([table_width_pct, 100 - table_width_pct], gap="large")

    with left:
        st.subheader("Radar")
        st.caption("Click any cell in a row to drill down into that coin's chart.")
        grid_opts = build_grid_options(df_display)
        # If we arrived via an alert deep-link, mark that row as pre-selected so
        # AgGrid highlights + ensures-it's-visible on first render.
        if focus_symbol and focus_symbol in set(df_display["symbol"]):
            for row in grid_opts.get("rowData", []) or []:
                if row.get("symbol") == focus_symbol:
                    row["__pre_selected__"] = True
            grid_opts["onFirstDataRendered"] = JsCode("""
            function(p) {
                let node = null;
                p.api.forEachNode(n => { if (n.data && n.data.__pre_selected__) node = n; });
                if (node) {
                    node.setSelected(true);
                    p.api.ensureNodeVisible(node, 'middle');
                }
            }
            """)
        # Bust the AgGrid widget key when a focus changes so the renderer hook re-fires.
        grid_response = AgGrid(
            df_display,
            gridOptions=grid_opts,
            height=grid_height,
            update_mode=GridUpdateMode.SELECTION_CHANGED,
            allow_unsafe_jscode=True,
            fit_columns_on_grid_load=False,
            theme="balham-dark",
            key=f"grid_{key}_{sort_by}_{focus_symbol or ''}",
        )

    if not chart_hidden:
        selected = grid_response.get("selected_rows")
        selected_sym: str | None = None
        if isinstance(selected, pd.DataFrame) and not selected.empty:
            selected_sym = selected.iloc[0]["symbol"]
        elif isinstance(selected, list) and selected:
            selected_sym = selected[0].get("symbol")
        # Alert deep-link takes priority over the default-first fallback.
        if not selected_sym and focus_symbol and focus_symbol in set(df_display["symbol"]):
            selected_sym = focus_symbol
        if not selected_sym:
            selected_sym = df_display.iloc[0]["symbol"]

        sel = next(s for s in signals if s["symbol"] == selected_sym)

        with right:
            st.subheader(f"Drilldown — {selected_sym}")
            st.caption(f"{sel['name']} · {sel['pair']} · last 150 bars")

            chart_df = sel["_df"].copy()
            overlays = sel.get("_overlays")
            overlay_cols = []
            if overlays is not None:
                for col in ("filt", "hband", "lband"):
                    if col in overlays.columns:
                        chart_df[col] = overlays[col]
                        overlay_cols.append(col)
            chart_df["long"] = sel["_state_series"].astype(int)
            chart_df = chart_df.tail(150).reset_index().rename(columns={"ts": "time"})

            # Chart height tracks the grid height so the two panes stay aligned.
            chart_height = max(grid_height - 160, 240)

            layers = [
                alt.Chart(chart_df).mark_line(color="#cccccc", strokeWidth=1.2).encode(
                    x=alt.X("time:T", title=None),
                    y=alt.Y("close:Q", title=selected_sym, scale=alt.Scale(zero=False)),
                )
            ]
            if "filt" in overlay_cols:
                layers.append(alt.Chart(chart_df).mark_line(color="#0aff68", strokeWidth=2).encode(
                    x="time:T", y="filt:Q"))
            if "hband" in overlay_cols:
                layers.append(alt.Chart(chart_df).mark_line(color="#0aff68", strokeWidth=1, opacity=0.6).encode(
                    x="time:T", y="hband:Q"))
            if "lband" in overlay_cols:
                layers.append(alt.Chart(chart_df).mark_line(color="#ff0a5a", strokeWidth=1, opacity=0.6).encode(
                    x="time:T", y="lband:Q"))
            layers.append(alt.Chart(chart_df[chart_df["long"] == 1]).mark_point(
                color="#0aff68", filled=True, size=20, opacity=0.5,
            ).encode(x="time:T", y="close:Q"))

            chart = alt.layer(*layers).properties(height=chart_height)
            st.altair_chart(chart, use_container_width=True)

            mc1, mc2, mc3, mc4 = st.columns(4)
            mc1.metric("State", sel["state"])
            mc2.metric("Bars in state", sel["bars_in_state"])
            mc3.metric("Stoch K", f"{sel['stoch_k']:.1f}" if sel["stoch_k"] is not None else "—")
            mc4.metric("Close vs HBand", f"{sel['close_vs_hband_pct']:+.2f}%")

    if skipped_rows:
        with st.expander(f"Skipped ({len(skipped_rows)})"):
            st.dataframe(pd.DataFrame(skipped_rows)[["symbol", "reason"]], use_container_width=True)


# --- Page header + tabs --------------------------------------------------------

st.title("Trend Radar")
st.caption(
    "Per-asset-class trend strategies across crypto / stocks / metals / commodities. "
    "Each tab picks its own strategy; defaults to GaussianChannel v3.1."
)

# Deep-link support: ?tab=stocks&symbol=AAPL pre-opens that tab + focuses that
# coin in its grid. Sidebar alert items use this to "jump to" the asset.
_qp = st.query_params
_jump_class = (_qp.get("tab") or "").strip().lower() if hasattr(_qp, "get") else ""
_jump_symbol = (_qp.get("symbol") or "").strip().upper() if hasattr(_qp, "get") else ""

tabs = st.tabs([ac.label for ac in ASSET_CLASSES])
for tab, ac in zip(tabs, ASSET_CLASSES):
    with tab:
        render_radar(ac, focus_symbol=(_jump_symbol if _jump_class == ac.key else None))

# Streamlit can't switch tabs from Python; nudge it via a tiny JS snippet that
# clicks the matching tab button on page load when ?tab= is present.
if _jump_class in {ac.key for ac in ASSET_CLASSES}:
    _label = next(ac.label for ac in ASSET_CLASSES if ac.key == _jump_class)
    st.markdown(
        f"""
        <script>
        (function() {{
            const want = {json.dumps(_label)};
            const tabs = window.parent.document.querySelectorAll('button[role="tab"]');
            for (const t of tabs) {{
                if (t.innerText.trim() === want && t.getAttribute('aria-selected') !== 'true') {{
                    t.click();
                    break;
                }}
            }}
        }})();
        </script>
        """,
        unsafe_allow_html=True,
    )


# --- Sidebar alerts feed (rendered last so this rerun's flips are included) ---

def _fmt_ago(ts_iso: str) -> str:
    try:
        from datetime import datetime, timezone
        ts = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - ts
        s = int(delta.total_seconds())
        if s < 60:    return f"{s}s ago"
        if s < 3600:  return f"{s // 60}m ago"
        if s < 86400: return f"{s // 3600}h ago"
        return f"{s // 86400}d ago"
    except Exception:
        return ts_iso


_class_labels = {ac.key: ac.label for ac in ASSET_CLASSES}

with _alerts_slot:
    with st.expander("🔔 Alerts", expanded=False):
        history = alerts.load_history()
        # Newest first
        history = sorted(history, key=lambda e: e.get("ts", ""), reverse=True)

        filter_options = ["All", *(ac.label for ac in ASSET_CLASSES)]
        cf, cc = st.columns([3, 1])
        chosen_filter = cf.selectbox("Filter", filter_options, key="alerts_filter")
        if cc.button("🗑", key="alerts_clear", help="Clear all alerts"):
            alerts.clear_history()
            st.rerun()

        if chosen_filter != "All":
            target_key = next(ac.key for ac in ASSET_CLASSES if ac.label == chosen_filter)
            history = [e for e in history if e.get("asset_class") == target_key]

        if not history:
            st.caption("No alerts yet. New FLAT ↔ LONG flips appear here.")
        else:
            st.caption(f"{len(history)} alert(s) · newest first · click to jump")
            for i, e in enumerate(history[:80]):  # cap rendered count
                ac_key = e.get("asset_class", "")
                ac_label = _class_labels.get(ac_key, ac_key)
                sym = e.get("symbol", "?")
                direction = e.get("direction", "")
                arrow = "🟢" if direction == "ENTRY" else "🔴"
                verb = "LONG" if direction == "ENTRY" else "EXIT"
                price = e.get("price")
                price_str = f" @ {price:.6g}" if isinstance(price, (int, float)) else ""
                label = f"{arrow} {sym} {verb} · {ac_label} · {_fmt_ago(e.get('ts', ''))}"
                if st.button(label, key=f"alert_jump_{i}", use_container_width=True,
                             help=f"Open {sym} in the {ac_label} tab{price_str}"):
                    st.query_params["tab"] = ac_key
                    st.query_params["symbol"] = sym
                    st.rerun()
