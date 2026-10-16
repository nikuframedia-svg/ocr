// Wait for committed edits before validation; keep failed edits visible.
(() => {
  const pending = new Map(), failures = new Set(), waiting = new Set();
  const notify = () => {
    if (pending.size) return;
    waiting.forEach(resolve => resolve());
    waiting.clear();
  };
  document.addEventListener('htmx:configRequest', event => {
    const form = event.detail.elt?.closest('form');
    if (!form?.getAttribute('hx-post')?.match(/^\/sheet\/\d+\/edit$/)) return;
    const revision = document.querySelector('#validate-form [name="expected_revision"]')?.value;
    if (revision !== undefined) event.detail.parameters.expected_revision = revision;
  });
  document.addEventListener('htmx:beforeRequest', event => {
    const detail = event.detail, form = detail.elt?.closest('form');
    if (!form?.getAttribute('hx-post')?.match(/^\/sheet\/\d+\/edit$/)) return;
    const key = form.querySelector('[name="field_path"]')?.value;
    failures.delete(key);
    pending.set(detail.xhr, {key, finish: window.ocrLatency?.start('edit')});
  });
  document.addEventListener('htmx:afterRequest', event => {
    const detail = event.detail, job = pending.get(detail.xhr);
    if (job === undefined) return;
    const {key, finish} = job;
    pending.delete(detail.xhr);
    if (!detail.successful) failures.add(key);
    else {
      failures.delete(key);
      const revision = detail.xhr.getResponseHeader('X-Sheet-Revision');
      const input = document.querySelector('#validate-form [name="expected_revision"]');
      if (input && revision !== null) input.value = String(Math.max(Number(input.value), Number(revision)));
    }
    notify();
    finish?.(detail.successful ? 'ok' : 'error');
  });
  document.addEventListener('click', event => {
    const button = event.target.closest('[data-cancel-cell-edit]');
    if (button) failures.delete(button.form?.querySelector('[name="field_path"]')?.value);
  });
  window.sheetWriteGuard = {
    async flush() {
      // Focusout starts the final cell save before the validation click.
      await Promise.resolve();
      while (pending.size) await new Promise(resolve => waiting.add(resolve));
      return failures.size === 0;
    }
  };
})();
