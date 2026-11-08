(function () {
  const DEFAULT_ITEMS = 5;
  const MIN_ITEMS = 1;
  const MAX_ITEMS = 80;
  const FALLBACK_ROW_PX = {
    activity: 52,
    queue: 44,
  };

  const counts = {
    activity: DEFAULT_ITEMS,
    queue: DEFAULT_ITEMS,
  };

  function clamp(value, minValue, maxValue) {
    return Math.max(minValue, Math.min(maxValue, value));
  }

  function parsePx(value) {
    const parsed = parseFloat(value);
    return Number.isFinite(parsed) ? parsed : 0;
  }

  function sampleRowHeight(panel, rowSelector, fallbackPx) {
    const rows = panel.querySelectorAll(rowSelector);
    if (!rows.length) {
      return fallbackPx;
    }

    const sampleSize = Math.min(rows.length, 3);
    let total = 0;

    for (let index = 0; index < sampleSize; index += 1) {
      const row = rows[index];
      const style = window.getComputedStyle(row);
      total += row.getBoundingClientRect().height + parsePx(style.marginBottom);
    }

    return total / sampleSize;
  }

  function computeCapacity(kind) {
    const panel = document.getElementById(`slot-${kind}`);
    if (!panel) {
      return counts[kind];
    }

    const header = panel.querySelector(":scope > header");
    if (!header) {
      return counts[kind];
    }

    const panelStyle = window.getComputedStyle(panel);
    const headerStyle = window.getComputedStyle(header);
    const contentHeight =
      panel.clientHeight -
      parsePx(panelStyle.paddingTop) -
      parsePx(panelStyle.paddingBottom) -
      header.offsetHeight -
      parsePx(headerStyle.marginBottom);

    if (contentHeight <= 0) {
      return counts[kind];
    }

    const rowSelector = kind === "activity" ? ".act-row" : ".q-item";
    const rowHeight = sampleRowHeight(panel, rowSelector, FALLBACK_ROW_PX[kind]);
    if (rowHeight <= 0) {
      return counts[kind];
    }

    const rows = Math.floor(contentHeight / rowHeight);
    return clamp(rows, MIN_ITEMS, MAX_ITEMS);
  }

  function refreshPanelCounts() {
    counts.activity = computeCapacity("activity");
    counts.queue = computeCapacity("queue");
  }

  window.addEventListener("resize", refreshPanelCounts);
  document.addEventListener("DOMContentLoaded", refreshPanelCounts);

  document.body.addEventListener("htmx:afterSwap", (event) => {
    const swapped = event.detail?.elt;
    if (!swapped?.id) {
      return;
    }
    if (swapped.id === "slot-active" || swapped.id === "slot-activity" || swapped.id === "slot-queue") {
      window.requestAnimationFrame(refreshPanelCounts);
    }
  });

  document.body.addEventListener("htmx:configRequest", (event) => {
    const detail = event.detail;
    if (!detail) {
      return;
    }

    if (detail.path === "/api/activity") {
      refreshPanelCounts();
      detail.parameters.max_items = counts.activity;
      return;
    }

    if (detail.path === "/api/queue") {
      refreshPanelCounts();
      detail.parameters.max_items = counts.queue;
    }
  });
})();
