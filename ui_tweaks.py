"""Small front-end behaviours Streamlit can't express natively.

Radar grid corner-drag: streamlit-aggrid renders the grid in a same-origin
iframe with a fixed pixel height. We make the iframe's wrapper CSS-resizable
(native bottom-right handle), let the iframe and the grid inside it follow the
wrapper's height, and remember the dragged height in localStorage. The Table
height slider still sets the starting height; changing it resets the drag.

The script runs directly in the app page via `st.html(..., unsafe_allow_javascript=True)`
(the old `st.components.v1.html` iframe loader is deprecated). A window flag makes it
install once; its timer then survives Streamlit reruns.
"""

from __future__ import annotations

import streamlit as st

_PARENT_JS = r"""
(function () {
  if (window.__trGridResizeV3) return;
  window.__trGridResizeV3 = true;
  const KEY = 'tr_grid_h_v1';
  const MIN = 120;                       // never persist a hidden/collapsed (0px) grid
  const HANDLE = 12;                     // px strip under the grid for the drag handle
  const read = () => { try { return JSON.parse(localStorage.getItem(KEY) || 'null'); } catch (e) { return null; } };
  const write = v => { try { localStorage.setItem(KEY, JSON.stringify(v)); } catch (e) {} };

  function tick() {
    document.querySelectorAll('iframe[data-testid="stCustomComponentV1"]').forEach(f => {
      if (!/agGrid/i.test(f.getAttribute('src') || '')) return;
      const w = f.parentElement;
      const base = parseInt(f.getAttribute('height'), 10) || 620;   // set by streamlit-aggrid
      if (w.dataset.trBase !== String(base)) {
        const saved = read();
        const h = (saved && saved.base === base && saved.h >= MIN) ? saved.h : base + HANDLE;
        w.dataset.trBase = String(base);
        w.style.height = h + 'px';
      }
      if (!w.dataset.trInit) {
        w.dataset.trInit = '1';
        w.style.minHeight = MIN + 'px';
        w.style.resize = 'vertical';
        w.style.overflow = 'hidden';
      }
      // Poll instead of ResizeObserver: user drags don't reliably notify it.
      const h = Math.round(w.getBoundingClientRect().height);
      if (w.offsetParent && h >= MIN && w.dataset.trH !== String(h)) {
        w.dataset.trH = String(h);
        write({ base: parseInt(w.dataset.trBase, 10), h });
      }
      f.style.setProperty('height', 'calc(100% - ' + HANDLE + 'px)', 'important');
      const d = f.contentDocument;
      if (d && d.head && !d.getElementById('tr-fit')) {
        const s = d.createElement('style');
        s.id = 'tr-fit';
        s.textContent = 'html,body,#root{height:100%!important;margin:0}#root>div{height:100%!important}';
        d.head.appendChild(s);
      }
    });
  }
  setInterval(tick, 400);
  tick();
})();
"""


def install_grid_resize() -> None:
    """Call once per page run; cheap and idempotent. Never breaks the page: if
    Streamlit's API changes, the grid simply loses its drag handle."""
    try:
        st.html(f"<script>{_PARENT_JS}</script>", unsafe_allow_javascript=True)
    except Exception:  # noqa: BLE001 - cosmetic feature
        pass
