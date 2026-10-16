/* Only action names and elapsed times leave the browser; no edited values. */
(() => {
  'use strict';
  const key = 'ocrPendingInteraction';
  const report = (action, milliseconds, result = 'ok') => {
    const body = JSON.stringify({action, duration_ms: milliseconds, result});
    try {
      if (navigator.sendBeacon && navigator.sendBeacon('/telemetry/interaction', new Blob([body], {type: 'application/json'}))) return;
      fetch('/telemetry/interaction', {method: 'POST', body, keepalive: true,
        headers: {'Content-Type': 'application/json'}}).catch(() => {});
    } catch (_) { /* Metrics must never interrupt editing. */ }
  };
  window.ocrLatency = {
    start(action) {
      const started = performance.now();
      let done = false;
      return (result = 'ok') => {
        if (done) return;
        done = true;
        requestAnimationFrame(() => requestAnimationFrame(() => report(action, performance.now() - started, result)));
      };
    },
    navigation(action) {
      try { sessionStorage.setItem(key, JSON.stringify({action, at: Date.now()})); } catch (_) {}
    },
    cancelNavigation() {
      try { sessionStorage.removeItem(key); } catch (_) {}
    },
  };
  function completeNavigation() {
    try {
      const pending = JSON.parse(sessionStorage.getItem(key) || 'null');
      sessionStorage.removeItem(key);
      if (!pending || performance.getEntriesByType('navigation')[0]?.type === 'back_forward') return;
      const elapsed = Date.now() - pending.at;
      if (elapsed >= 0 && elapsed < 3600000) {
        requestAnimationFrame(() => requestAnimationFrame(() => report(pending.action, Date.now() - pending.at)));
      }
    } catch (_) {}
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', completeNavigation, {once: true});
  else completeNavigation();
})();
