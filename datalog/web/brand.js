'use strict';
// SynapseDev brand: theme selection (before first paint) and the SynapseAI symbol.
// The symbol and the SynapseDev identity are trademarks of SynapseAI (see TRADEMARKS.md);
// forks that redistribute Datalog replace SYMBOL below.
const Brand = (() => {
  const KEY = 'datalog.theme';
  const media = window.matchMedia ? matchMedia('(prefers-color-scheme: dark)') : null;
  const stored = () => { try { const v = localStorage.getItem(KEY); return v === 'dark' || v === 'light' ? v : null; } catch { return null; } };
  const current = () => stored() || (media && media.matches ? 'dark' : 'light');
  const apply = theme => {
    document.documentElement.dataset.theme = theme;
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.content = theme === 'dark' ? '#08080B' : '#FAFAFB';
  };
  apply(current());
  media?.addEventListener?.('change', () => { if (!stored()) apply(current()); });

  let ids = 0;
  // Two overlapping spheres (people and technology) form a lens; the open orbit and the dark sphere complete the mark.
  const symbol = (label = '') => {
    const id = `brand-lens-${++ids}`;
    return `<svg class="brand-mark" viewBox="0 0 760 660" ${label ? `role="img" aria-label="${label}"` : 'aria-hidden="true"'} focusable="false">
<defs><mask id="${id}" maskUnits="userSpaceOnUse" x="0" y="0" width="760" height="660"><rect width="760" height="660" fill="#fff"/><circle cx="491" cy="381" r="262" fill="#777"/></mask>
<clipPath id="${id}-c"><circle cx="277" cy="278" r="277"/></clipPath></defs>
<circle cx="491" cy="381" r="253" fill="none" stroke="currentColor" stroke-width="18"/>
<circle cx="277" cy="278" r="277" fill="currentColor" mask="url(#${id})"/>
<circle cx="628" cy="557" r="92" fill="currentColor"/></svg>`;
  };
  const icons = {
    dark: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"/></svg>',
    light: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>',
  };
  function toggle() {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'theme-toggle';
    const render = () => {
      const dark = document.documentElement.dataset.theme === 'dark';
      button.innerHTML = dark ? icons.light : icons.dark;
      button.setAttribute('aria-label', dark ? 'Usar tema claro' : 'Usar tema escuro');
      button.title = button.getAttribute('aria-label');
    };
    button.addEventListener('click', () => {
      const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
      try { localStorage.setItem(KEY, next); } catch { /* storage may be blocked */ }
      if (document.startViewTransition && !matchMedia('(prefers-reduced-motion: reduce)').matches) document.startViewTransition(() => apply(next));
      else apply(next);
      document.querySelectorAll('.theme-toggle').forEach(b => b.dispatchEvent(new Event('datalog:theme')));
    });
    button.addEventListener('datalog:theme', render);
    render();
    return button;
  }
  function mount() {
    document.querySelectorAll('[data-brand-mark]').forEach(slot => { slot.outerHTML = symbol(slot.dataset.brandMark); });
    document.querySelectorAll('[data-theme-toggle]').forEach(slot => slot.replaceChildren(toggle()));
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mount);
  else mount();
  return {symbol, theme: current};
})();
