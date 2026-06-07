# Learnings

## Streamlit + AgGrid

- Keep the `AgGrid(...)` call outside any optional focus or deep-link conditionals. If the grid render is nested under a branch like `if focus_symbol ...`, `grid_response` can be undefined for normal page loads.
- When tightening the table layout, `fit_columns_on_grid_load=True` is the safe way to make columns fill the available table width and remove the empty right gutter.
- If we make the grid denser, reduce both the AgGrid theme variables and the cell/header font sizes together so row height, header height, and text stay visually aligned.

## Strategy Integration

- A new strategy only needs to return the fields the app already reads:
  `snapshot`, `state_series`, `trades`, and optional `overlays`.
- The app expects `snapshot` to expose these attributes:
  `in_position`, `bars_in_state`, `entry_index`, `entry_price`, `bar_color`, `filter_up`, `close_vs_hband_pct`, `stoch_k`, `last_close`, `last_filter`, `last_hband`, `last_lband`.
- If a custom strategy hits constructor mismatch issues with the shared `SignalState` object in deployment, a lightweight attribute object like `SimpleNamespace` is a safe fallback as long as it exposes the same field names.
- For Donchian logic in this app, use the previous-bar channel values with `.shift(1)` so the breakout compares the close against the prior channel, not the current bar's own high/low.

## Deployment

- Streamlit Cloud is reading from the GitHub repo, not just local workspace edits. Local fixes are not live until they are committed and pushed.
