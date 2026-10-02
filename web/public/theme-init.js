/* Applies the saved theme and density before first paint so the page never flashes the
   wrong theme or re-flows to a different spacing once React mounts. Kept as a separate
   same-origin file because the production CSP is script-src 'self' (no inline scripts).
   Keys and values match useTheme() and the density state in src/App.tsx. */
(function () {
  var root = document.documentElement;
  var theme = 'dark';
  var density = 'compact';
  try {
    if (window.localStorage.getItem('mkai-theme') === 'light') theme = 'light';
    if (window.localStorage.getItem('mkai-density') === 'comfortable') density = 'comfortable';
  } catch (e) {}
  root.dataset.theme = theme;
  root.dataset.density = density;
  var meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute('content', theme === 'light' ? '#F3F6FA' : '#0F1214');
})();
