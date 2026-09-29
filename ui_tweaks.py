"""Small front-end behaviours Streamlit can't express natively.

Radar grid corner-drag: streamlit-aggrid renders the grid in a same-origin
iframe with a fixed pixel height. We make the iframe's wrapper CSS-resizable
(native bottom-right handle), let the iframe and the grid inside it follow the
wrapper's height, and remember the dragged height in localStorage. The Table
height slider still sets the starting height; changing it resets the drag.

Drilldown/Radar divider: a full-height, keyboard-accessible separator resizes
the two Streamlit columns in place. Each asset tab remembers its own split.

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


_PANE_JS = r"""
(function () {
  if (window.__trPaneResizeV1) return;
  window.__trPaneResizeV1 = true;
  const DEFAULT = 2.5 / 3.5;
  const PREFIX = 'tr_pane_split_v1_';
  const narrow = window.matchMedia('(max-width: 900px)');

  const css = document.createElement('style');
  css.textContent = `
    .tr-pane-row { position: relative; }
    .tr-pane-divider {
      position: absolute; top: 0; bottom: 0; width: 18px; z-index: 6;
      cursor: col-resize; touch-action: none; background: transparent;
    }
    .tr-pane-divider::before {
      content: ''; position: absolute; top: 0; bottom: 0; left: 8px;
      width: 1px; background: var(--tr-pane-border);
    }
    .tr-pane-divider::after {
      content: ''; position: absolute; top: 22px; left: 3px;
      width: 12px; height: 32px; border-radius: 7px;
      border: 1px solid var(--tr-pane-border);
      background: var(--tr-pane-surface);
      box-shadow: 0 2px 5px rgba(15, 23, 42, .12);
    }
    .tr-pane-divider:hover::before, .tr-pane-divider:focus-visible::before,
    .tr-pane-divider.is-dragging::before { width: 2px; background: var(--tr-pane-accent); }
    .tr-pane-divider:hover::after, .tr-pane-divider:focus-visible::after,
    .tr-pane-divider.is-dragging::after { border-color: var(--tr-pane-accent); }
    .tr-pane-divider:focus-visible { outline: 2px solid var(--tr-pane-accent); outline-offset: 2px; }
    @media (max-width: 900px) { .tr-pane-divider { display: none !important; } }
  `;
  document.head.appendChild(css);

  const clamp = (row, value) => {
    const width = Math.max(1, row.getBoundingClientRect().width - 16);
    const low = Math.max(.2, Math.min(.48, 380 / width));
    const high = Math.min(.8, Math.max(.52, 1 - 300 / width));
    return Math.max(low, Math.min(high, value));
  };
  const read = key => {
    try { const n = Number(localStorage.getItem(PREFIX + key)); return Number.isFinite(n) && n > 0 ? n : DEFAULT; }
    catch (e) { return DEFAULT; }
  };
  const save = (key, ratio) => { try { localStorage.setItem(PREFIX + key, String(ratio)); } catch (e) {} };

  function apply(row) {
    const [left, right] = row.__trPanes;
    const handle = row.__trHandle;
    if (narrow.matches) {
      left.style.removeProperty('flex');
      right.style.removeProperty('flex');
      left.style.removeProperty('min-width');
      right.style.removeProperty('min-width');
      handle.style.display = 'none';
      return;
    }
    const ratio = clamp(row, row.__trRatio);
    row.__trRatio = ratio;
    left.style.setProperty('flex', ratio + ' 1 0%', 'important');
    right.style.setProperty('flex', (1 - ratio) + ' 1 0%', 'important');
    left.style.setProperty('min-width', '0', 'important');
    right.style.setProperty('min-width', '0', 'important');
    handle.style.display = '';
    handle.setAttribute('aria-valuenow', String(Math.round(ratio * 100)));
    handle.setAttribute('aria-valuetext', Math.round(ratio * 100) + '% Drilldown, ' +
      Math.round((1 - ratio) * 100) + '% Radar');
    requestAnimationFrame(() => {
      if (!row.isConnected) return;
      const a = left.getBoundingClientRect(), b = right.getBoundingClientRect();
      handle.style.left = ((a.right + b.left) / 2 - row.getBoundingClientRect().left - 9) + 'px';
    });
  }

  function tick() {
    document.querySelectorAll('[data-testid="stHorizontalBlock"]').forEach(row => {
      const cols = Array.from(row.children).filter(el => el.getAttribute('data-testid') === 'stColumn');
      if (cols.length !== 2 ||
          !cols[0].querySelector('[class*="st-key-drilldown_pane_"]') ||
          !cols[1].querySelector('[class*="st-key-radar_panel_"]')) return;
      const panel = cols[1].querySelector('[class*="st-key-radar_panel_"]');
      const key = Array.from(panel.classList).find(c => c.startsWith('st-key-radar_panel_'))
        ?.slice('st-key-radar_panel_'.length) || 'default';
      row.__trPanes = cols;
      if (row.__trHandle && row.__trHandle.parentElement !== row) row.appendChild(row.__trHandle);
      if (row.__trKey && row.__trKey !== key) {
        row.__trKey = key;
        row.__trRatio = read(key);
      }
      if (!row.__trHandle) {
        row.classList.add('tr-pane-row');
        row.__trKey = key;
        row.__trRatio = read(key);
        const handle = document.createElement('div');
        handle.className = 'tr-pane-divider';
        handle.setAttribute('role', 'separator');
        handle.setAttribute('aria-orientation', 'vertical');
        handle.setAttribute('aria-label', 'Resize Drilldown and Radar panels');
        handle.setAttribute('aria-valuemin', '20');
        handle.setAttribute('aria-valuemax', '80');
        handle.setAttribute('tabindex', '0');
        handle.title = 'Drag to resize panels; arrow keys also work. Double-click to reset.';
        row.appendChild(handle);
        row.__trHandle = handle;
        handle.addEventListener('pointerdown', e => {
          if (narrow.matches) return;
          e.preventDefault();
          handle.setPointerCapture(e.pointerId);
          handle.classList.add('is-dragging');
          row.__trDrag = { x: e.clientX, ratio: row.__trRatio };
        });
        handle.addEventListener('pointermove', e => {
          if (!row.__trDrag) return;
          const width = Math.max(1, row.getBoundingClientRect().width - 16);
          row.__trRatio = row.__trDrag.ratio + (e.clientX - row.__trDrag.x) / width;
          apply(row);
        });
        const finish = () => {
          if (!row.__trDrag) return;
          row.__trDrag = null;
          handle.classList.remove('is-dragging');
          save(key, row.__trRatio);
        };
        handle.addEventListener('pointerup', finish);
        handle.addEventListener('pointercancel', finish);
        handle.addEventListener('keydown', e => {
          if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
          e.preventDefault();
          row.__trRatio += (e.key === 'ArrowRight' ? 1 : -1) * (e.shiftKey ? .01 : .05);
          apply(row);
          save(key, row.__trRatio);
        });
        handle.addEventListener('dblclick', () => {
          row.__trRatio = DEFAULT;
          apply(row);
          save(key, row.__trRatio);
        });
      }
      apply(row);
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


def install_pane_resize() -> None:
    """Add an in-page drag separator to the Drilldown/Radar columns."""
    try:
        st.html(f"<script>{_PANE_JS}</script>", unsafe_allow_javascript=True)
    except Exception:  # noqa: BLE001 - keep the dashboard usable if JS is unavailable
        pass
