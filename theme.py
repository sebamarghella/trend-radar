"""Design tokens for the Trend Radar dashboard — light + dark palettes.

Two-layer model:
  - Semantic constants (ACCENT, BULLISH, BAR_COLORS, type scale) don't change
    between modes — green is green either way.
  - Surface tokens (BG, FG, BORDER, NEUTRAL_LINE, FLAT row chrome) flip per
    mode. `get_palette(mode)` returns a flat dict merging both layers so the
    consumer code accesses everything via PALETTE["KEY"].

Sources cited in the build commit:
  - colors.csv "Financial Dashboard" (dark)
  - colors.csv "Personal Finance Tracker" → light surfaces
  - charts.csv "Stock / Trading OHLC" → bullish/bearish
  - ui-reasoning.csv "Fintech/Crypto" → anti-patterns (no neon, no AI gradients)
"""

# --- Mode-independent semantic tokens -----------------------------------------

ACCENT = "#22C55E"            # primary green (LONG / positive)
DESTRUCTIVE = "#EF4444"       # red (EXIT / negative)
TRUST_BLUE = "#3B82F6"        # links / informational
BULLISH = "#26A69A"           # chart: filter, BUY markers (TradingView green)
BEARISH = "#EF5350"           # chart: lower band, SELL markers (TradingView red)

# State chip — LONG is always accent green with near-black text (legible on green
# in both modes); FLAT chrome varies by mode (in the per-mode block below).
STATE_LONG_BG = ACCENT
STATE_LONG_FG = "#0F172A"

# Bar color matrix — same scale on both modes; the per-bar foreground (white vs
# dark) is decided in the AgGrid cellStyle JS based on which bucket the row is in.
BAR_COLORS = {
    "STRONG_UP":   "#16A34A",
    "UP":          "#22C55E",
    "WEAK_UP":     "#14532D",
    "WEAK_DOWN":   "#7F1D1D",
    "DOWN":        "#EF4444",
    "STRONG_DOWN": "#B91C1C",
    "NEUTRAL":     "#475569",
}

# Typography
FONT_FAMILY_SANS = "'Inter', system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif"
FONT_FAMILY_MONO = "'JetBrains Mono', 'SF Mono', Menlo, Consolas, monospace"
TYPE_SCALE = {"xs": 12, "sm": 14, "base": 16, "md": 18, "lg": 24, "xl": 32}
SPACING = {"xs": 4, "sm": 8, "md": 12, "lg": 16, "xl": 24}


# --- Per-mode surface tokens --------------------------------------------------

DARK = {
    "MODE":            "dark",
    "BG_BASE":         "#020617",
    "BG_CARD":         "#0E1223",
    "BG_MUTED":        "#1A1E2F",
    "FG_PRIMARY":      "#F8FAFC",
    "FG_MUTED":        "#94A3B8",
    "BORDER":          "#334155",
    "RING":            "#1E40AF",
    "NEUTRAL_LINE":    "#94A3B8",   # chart price line
    "STATE_FLAT_BG":   "#2B2B2B",
    "STATE_FLAT_FG":   "#94A3B8",
    "AGGRID_THEME":    "balham-dark",
    "GRID_OPACITY":    0.30,
    # White text on dark/saturated bar buckets, light text on dim ones.
    "BAR_FG_WHITE":    ["STRONG_UP", "UP", "DOWN", "STRONG_DOWN", "WEAK_DOWN"],
}

LIGHT = {
    "MODE":            "light",
    "BG_BASE":         "#FFFFFF",
    "BG_CARD":         "#F8FAFC",
    "BG_MUTED":        "#F1F5F9",
    "FG_PRIMARY":      "#0F172A",
    "FG_MUTED":        "#64748B",
    "BORDER":          "#E2E8F0",
    "RING":            "#1E40AF",
    "NEUTRAL_LINE":    "#475569",   # darker price line for white bg
    "STATE_FLAT_BG":   "#F1F5F9",
    "STATE_FLAT_FG":   "#64748B",
    "AGGRID_THEME":    "balham",
    "GRID_OPACITY":    0.55,
    # On light mode the strong buckets stay white-on-bold; the dim ones flip
    # (WEAK_UP / WEAK_DOWN are dark text on pale fill).
    "BAR_FG_WHITE":    ["STRONG_UP", "UP", "DOWN", "STRONG_DOWN"],
}


def get_palette(mode: str) -> dict:
    """Returns a flat dict with surface + semantic + typography + bar tokens."""
    surface = DARK if str(mode).lower() == "dark" else LIGHT
    return {
        **surface,
        # Mode-independent overlay
        "ACCENT": ACCENT,
        "DESTRUCTIVE": DESTRUCTIVE,
        "TRUST_BLUE": TRUST_BLUE,
        "BULLISH": BULLISH,
        "BEARISH": BEARISH,
        "STATE_LONG_BG": STATE_LONG_BG,
        "STATE_LONG_FG": STATE_LONG_FG,
        "BAR_COLORS": BAR_COLORS,
        "FONT_SANS": FONT_FAMILY_SANS,
        "FONT_MONO": FONT_FAMILY_MONO,
    }
