// Shared client for the server-side background job tracker (jobs.py).
// Used by Backup Now / Restore Now on Settings, and by the loading shells
// for Insights, Retention, and Consignment Overview.
window.VZProgress = (function () {
  function fmtElapsed(startedAt) {
    const secs = Math.max(0, Math.round((Date.now() / 1000) - startedAt));
    if (secs < 60) return `${secs}s`;
    return `${Math.floor(secs / 60)}m ${secs % 60}s`;
  }

  function render(el, data) {
    const total = data.steps.length;
    const current = Math.min(data.current, total);
    const pct = (typeof data.fraction === 'number')
      ? Math.round(data.fraction * 100)
      : (total > 0 ? Math.round((current / total) * 100) : 0);
    const isError = data.status === 'error';
    const label = isError
      ? (data.message || 'Something went wrong.')
      : (data.steps[Math.min(current, total - 1)] || '');
    const elapsed = data.started_at ? fmtElapsed(data.started_at) : '';
    el.innerHTML = `
      <div class="vz-progress-label">${label}${!isError && data.status === 'running' ? ` (${current}/${total})` : ''}</div>
      <div class="vz-progress-bar-track"><div class="vz-progress-bar-fill${isError ? ' error' : ''}" style="width:${pct}%"></div></div>
      <div class="vz-progress-meta">${isError ? 'Failed' : (data.status === 'done' ? 'Done' : 'Working')} · ${elapsed}</div>
    `;
  }

  // statusUrl: endpoint returning {status: running|done|error, steps: [...],
  // current: N, started_at: epoch_seconds, message?: str}. extraParams is
  // an object merged into the query string on every poll (e.g. job kind).
  function poll(jobId, statusUrl, extraParams, callbacks) {
    callbacks = callbacks || {};
    const intervalMs = callbacks.intervalMs || 700;
    let stopped = false;

    async function tick() {
      if (stopped) return;
      try {
        const params = new URLSearchParams(Object.assign({ job_id: jobId }, extraParams || {}));
        const res = await fetch(`${statusUrl}?${params.toString()}`);
        if (res.status === 404) {
          callbacks.onError && callbacks.onError({
            status: 'error',
            message: 'Lost track of this job — the server may have restarted. Try again.',
          });
          return;
        }
        if (!res.ok) throw new Error('status check failed');
        const data = await res.json();
        if (data.status === 'running') {
          callbacks.onUpdate && callbacks.onUpdate(data);
          setTimeout(tick, intervalMs);
        } else if (data.status === 'done') {
          callbacks.onUpdate && callbacks.onUpdate(data);
          callbacks.onDone && callbacks.onDone(data);
        } else {
          callbacks.onError && callbacks.onError(data);
        }
      } catch (e) {
        callbacks.onError && callbacks.onError({ status: 'error', message: 'Could not reach the server.' });
      }
    }
    tick();
    return { stop: () => { stopped = true; } };
  }

  return { render, poll };
})();
