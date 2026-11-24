(() => {
  'use strict';

  const originalFetch = window.fetch.bind(window);
  const transientStatuses = new Set([408, 429, 502, 503, 504]);
  const wait = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
  let banner;

  function recoveryBanner(message, error = false) {
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
  }

  window.fetch = async function resilientFetch(input, init = {}) {
    const method = String(init.method || (input && input.method) || 'GET').toUpperCase();
    const url = typeof input === 'string' ? input : String(input && input.url || '');
    const retryable = (method === 'GET' || method === 'HEAD') && (url.startsWith('/') || url.startsWith(location.origin));
    const attempts = retryable ? 3 : 1;
    let lastError;

    for (let attempt = 0; attempt < attempts; attempt += 1) {
      try {
        const response = await originalFetch(input, init);
        if (!retryable || !transientStatuses.has(response.status) || attempt === attempts - 1) {
          if (response.ok) recoveryBanner('');
          return response;
        }
        recoveryBanner('Connection interrupted. Reconnecting automatically…');
      } catch (error) {
        lastError = error;
        if (!retryable || attempt === attempts - 1) break;
        recoveryBanner('Connection interrupted. Reconnecting automatically…');
      }
      await wait(400 * (2 ** attempt) + Math.floor(Math.random() * 150));
    }

    recoveryBanner('The connection is still unavailable. Your saved data is safe; please try again.', true);
    throw lastError || new TypeError('The service is temporarily unavailable. Please try again.');
  };

  window.addEventListener('offline', () => recoveryBanner('You are offline. Saved data is safe; reconnecting when the network returns.', true));
  window.addEventListener('online', () => {
    recoveryBanner('Connection restored.');
    setTimeout(() => recoveryBanner(''), 2000);
  });
})();
