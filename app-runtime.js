(() => {
  'use strict';

  const originalFetch = window.fetch.bind(window);
  const transientStatuses = new Set([408, 429, 502, 503, 504]);
  const wait = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
  let banner;
  let bannerTimer;
  const recoveryReloads = new Map();

  function recoveryBanner(message, error = false, hideAfter = 0) {
    clearTimeout(bannerTimer);
    if (!banner) {
      banner = document.createElement('div');
      banner.setAttribute('role', 'status');
      Object.assign(banner.style, {
        position: 'fixed', left: '50%', bottom: '18px', transform: 'translateX(-50%)',
        zIndex: '99999', padding: '10px 16px', borderRadius: '10px', color: '#fff',
        font: '600 14px/1.35 system-ui,sans-serif', boxShadow: '0 8px 24px #0004',
        maxWidth: 'min(92vw,620px)', textAlign: 'center', display: 'none'
      });
      document.addEventListener('DOMContentLoaded', () => document.body.appendChild(banner), {once: true});
      if (document.body) document.body.appendChild(banner);
    }
    banner.textContent = message || '';
    banner.style.background = error ? '#b42318' : '#176b52';
    banner.style.display = message ? 'block' : 'none';
    if (message && hideAfter) bannerTimer = setTimeout(() => recoveryBanner(''), hideAfter);
  }

  function clearScheduledRecovery(url) {
    const timer = recoveryReloads.get(url);
    if (timer) clearTimeout(timer);
    recoveryReloads.delete(url);
    try { sessionStorage.removeItem(`ollies-recovery:${url}`); } catch (_) {}
  }

  function scheduleInitialRecovery(url) {
    if (performance.now() > 60000 || document.visibilityState === 'hidden' || recoveryReloads.has(url)) return;
    const key = `ollies-recovery:${url}`;
    let previous = 0;
    try { previous = Number(sessionStorage.getItem(key) || 0); } catch (_) {}
    if (previous >= 1) {
      recoveryBanner('The service is temporarily unavailable. Your saved data is safe; refresh once to try again.', true, 12000);
      return;
    }
    try { sessionStorage.setItem(key, '1'); } catch (_) {}
    recoveryBanner('The service is still starting. This page will refresh automatically…');
    recoveryReloads.set(url, setTimeout(() => location.reload(), 5000));
  }

  window.fetch = async function resilientFetch(input, init = {}) {
    const method = String(init.method || (input && input.method) || 'GET').toUpperCase();
    const url = typeof input === 'string' ? input : String(input && input.url || '');
    const retryable = (method === 'GET' || method === 'HEAD') && (url.startsWith('/') || url.startsWith(location.origin));
    const attempts = retryable ? 6 : 1;
    let lastError;

    for (let attempt = 0; attempt < attempts; attempt += 1) {
      try {
        const response = await originalFetch(input, init);
        if (!retryable || !transientStatuses.has(response.status)) {
          if (response.ok) {
            clearScheduledRecovery(url);
            recoveryBanner('');
          }
          return response;
        }
        if (attempt === attempts - 1) {
          scheduleInitialRecovery(url);
          return response;
        }
        recoveryBanner('Connection interrupted. Reconnecting automatically…');
      } catch (error) {
        lastError = error;
        if (!retryable || attempt === attempts - 1) break;
        recoveryBanner('Connection interrupted. Reconnecting automatically…');
      }
      await wait(Math.min(400 * (2 ** attempt), 3200) + Math.floor(Math.random() * 150));
    }

    scheduleInitialRecovery(url);
    throw lastError || new TypeError('The service is temporarily unavailable. Please try again.');
  };

  window.addEventListener('offline', () => recoveryBanner('You are offline. Saved data is safe; reconnecting when the network returns.', true));
  window.addEventListener('online', () => {
    recoveryBanner('Connection restored.');
    setTimeout(() => recoveryBanner(''), 2000);
  });
})();
