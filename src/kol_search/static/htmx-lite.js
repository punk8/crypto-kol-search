/* Local, dependency-free subset used by this app: hx-get + periodic trigger + target/swap. */
(() => {
  const bind = (root = document) => {
    root.querySelectorAll('[hx-get]:not([hx-trigger])').forEach((el) => {
      if (el.dataset.hxBound) return;
      el.dataset.hxBound = '1';
      el.addEventListener('click', async (event) => {
        event.preventDefault();
        const response = await fetch(el.getAttribute('hx-get'), {headers: {'HX-Request': 'true'}});
        if (!response.ok) return;
        const html = await response.text();
        const target = document.querySelector(el.getAttribute('hx-target')) || el;
        if (el.getAttribute('hx-swap') === 'outerHTML') {
          target.outerHTML = html;
          bind(document);
        } else { target.innerHTML = html; bind(target); }
      });
    });
    root.querySelectorAll('[hx-get][hx-trigger^="every "]').forEach((el) => {
      if (el.dataset.hxBound) return;
      el.dataset.hxBound = '1';
      const seconds = parseFloat(el.getAttribute('hx-trigger').split(' ')[1]) || 2;
      const poll = async () => {
        if (!document.body.contains(el)) return;
        try {
          const response = await fetch(el.getAttribute('hx-get'), {headers: {'HX-Request': 'true'}});
          if (!response.ok) return;
          if (response.headers.get('HX-Refresh') === 'true') {
            window.location.reload();
            return;
          }
          const html = await response.text();
          const target = document.querySelector(el.getAttribute('hx-target')) || el;
          if (el.getAttribute('hx-swap') === 'outerHTML') {
            target.outerHTML = html;
            bind(document);
          } else { target.innerHTML = html; bind(target); }
        } finally { if (document.body.contains(el)) setTimeout(poll, seconds * 1000); }
      };
      setTimeout(poll, seconds * 1000);
    });
  };
  document.addEventListener('DOMContentLoaded', () => bind());
})();
