"""Design tokens for the Trend Radar dashboard.

Single source of truth for colors, type scale, and spacing. App.py + alerts +
chart code all import from here so the palette is consistent and tweakable in
one place.

Derived from UI Pro Max (nextlevelbuilder/ui-ux-pro-max-skill):
  - colors.csv row 1 "Financial Dashboard" — base dark palette
  - charts.csv row 1 "Stock / Trading OHLC" — bullish/bearish markers
  - ui-reasoning.csv row 6 "Financial Dashboard" — Dark Mode (OLED) + Data-Dense
  - ux-guidelines.csv — modular type scale, color-not-the-only-signal
"""

# --- Palette (semantic tokens) -------------------------------------------------

# Surfaces
BG_BASE = "#020617"          # page bg
BG_CARD = "#0E1223"          # cards / panels
BG_MUTED = "#1A1E2F"          # secondary surfaces (sidebar, etc.)
BORDER = "#334155"
RING = "#1E40AF"

# Text
FG_PRIMARY = "#F8FAFC"
FG_MUTED = "#94A3B8"

# Brand
ACCENT = "#22C55E"            # primary green for LONG / positive
DESTRUCTIVE = "#EF4444"       # red for EXIT / negative
TRUST_BLUE = "#3B82F6"        # links / informational

# Chart-specific (calmer than UI accents — used for line/marker)
BULLISH = "#26A69A"           # filter, BUY markers (TradingView green)
BEARISH = "#EF5350"           # lower band, SELL markers (TradingView red)
NEUTRAL_LINE = "#94A3B8"      # price line

# State-color mapping for the radar grid bar colors
# Keeps the "scannable at a glance" matrix of 6 buckets while replacing the
# eye-burning neons with TradingView-grade greens/reds.
BAR_COLORS = {
    "STRONG_UP":   "#16A34A",   # solid bullish
    "UP":          "#22C55E",   # bullish
    "WEAK_UP":     "#14532D",   # dim bullish
    "WEAK_DOWN":   "#7F1D1D",   # dim bearish
    "DOWN":        "#EF4444",   # bearish
    "STRONG_DOWN": "#B91C1C",   # solid bearish
    "NEUTRAL":     "#475569",   # slate grey, not pure grey
}

# Cell tokens (text-on-accent)
STATE_LONG_BG = ACCENT
STATE_LONG_FG = "#0F172A"
STATE_FLAT_BG = "#2b2b2b"
STATE_FLAT_FG = FG_MUTED

# --- Typography (modular scale) -----------------------------------------------

FONT_FAMILY_SANS = "'Inter', system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif"
FONT_FAMILY_MONO = "'JetBrains Mono', 'SF Mono', Menlo, Consolas, monospace"

# Type scale (px) — from ux-guidelines.csv typography rule
TYPE_SCALE = {"xs": 12, "sm": 14, "base": 16, "md": 18, "lg": 24, "xl": 32}

# --- Spacing -------------------------------------------------------------------

SPACING = {"xs": 4, "sm": 8, "md": 12, "lg": 16, "xl": 24}
