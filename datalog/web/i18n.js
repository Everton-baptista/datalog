'use strict';
// Interface language (pt-BR / en). Portuguese is the source language of the UI markup;
// English text comes from the dictionary in i18n-en.js. API messages are English at the
// source and are shown in Portuguese through i18n-api-pt.js (I18n.api). Rendered text nodes and
// accessible attributes are translated as they enter the DOM, so modules keep
// producing Portuguese markup and user data (logs, attributes, prompts) is left alone.
const I18n = (() => {
  const KEY = 'datalog.lang';
  const LOCALES = {pt: 'pt-BR', en: 'en-US'};
  const ATTRIBUTES = ['placeholder', 'aria-label', 'title', 'alt'];
  const SKIP = 'script,style,code,pre,textarea,[translate="no"],[data-no-i18n],.log-message';
  const read = () => {
    try {
      const fromUrl = new URLSearchParams(location.search).get('lang');
      if (LOCALES[fromUrl]) { localStorage.setItem(KEY, fromUrl); return fromUrl; }
      const saved = localStorage.getItem(KEY);
      if (LOCALES[saved]) return saved;
    } catch { /* storage may be blocked */ }
    return 'pt';
  };
  const lang = read();
  const normalize = text => text.replace(/\s+/g, ' ').trim();
  const escapeRe = text => text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  // A pattern whose fixed text is only a short connector ("{} de {}") would rewrite
  // user data such as "Uso de CPU"; those patterns only accept numeric values.
  const NUMERIC = /^[\d\s.,:%–\-/]+$/;
  // Build a translator from a {source text: translation} dictionary.
  function compile(dictionary) {
    const exact = new Map();
    const patterns = [];
    for (const [key, value] of Object.entries(dictionary)) {
      if (!key.includes('{}')) { exact.set(normalize(key), value); continue; }
      const literal = key.replaceAll('{}', '').replace(/[^A-Za-zÀ-ÿ]/g, '');
      patterns.push([new RegExp(`^${key.split('{}').map(escapeRe).join('(.+?)')}$`, 's'), value, key.length, literal.length < 6]);
    }
    patterns.sort((a, b) => b[2] - a[2]);
    return text => translate(exact, patterns, text);
  }
  const ui = compile(lang === 'en' ? (window.DATALOG_EN || {}) : {});
  let apiTranslator;
  function t(text) {
    if (lang === 'pt') return text;
    return ui(text);
  }
  // Server messages are English; the PT interface shows them through DATALOG_API_PT.
  function api(text) {
    if (lang !== 'pt') return text;
    apiTranslator ||= compile(window.DATALOG_API_PT || {});
    return apiTranslator(text);
  }
  function translate(exact, patterns, text) {
    if (text === null || text === undefined) return text;
    const source = String(text);
    const core = normalize(source);
    if (!core) return source;
    let out = exact.get(core);
    if (out === undefined) {
      for (const [re, value, , numericOnly] of patterns) {
        const match = core.match(re);
        if (!match || (numericOnly && !match.slice(1).every(part => NUMERIC.test(part)))) continue;
        let i = 1;
        out = value.replace(/\{\}/g, () => { const part = match[i++] ?? ''; return exact.get(part) ?? part; });
        break;
      }
    }
    if (out === undefined) return source;
    const lead = source.match(/^\s*/)[0], tail = source.match(/\s*$/)[0];
    return lead + out + tail;
  }
  const skipped = node => {
    const element = node.nodeType === 1 ? node : node.parentElement;
    return !element || Boolean(element.closest(SKIP));
  };
  function translateText(node) {
    if (skipped(node)) return;
    const next = t(node.nodeValue);
    if (next !== node.nodeValue) node.nodeValue = next;
  }
  function translateAttributes(element) {
    if (skipped(element)) return;
    for (const name of ATTRIBUTES) {
      const value = element.getAttribute(name);
      if (value === null) continue;
      const next = t(value);
      if (next !== value) element.setAttribute(name, next);
    }
  }
  function translateTree(root) {
    if (lang === 'pt' || !root) return;
    if (root.nodeType === 3) return translateText(root);
    if (root.nodeType !== 1 && root.nodeType !== 9 && root.nodeType !== 11) return;
    if (root.nodeType === 1) translateAttributes(root);
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      if (node.nodeType === 3) translateText(node);
      else translateAttributes(node);
    }
  }
  function observe() {
    if (lang === 'pt') return;
    new MutationObserver(records => {
      for (const record of records) {
        if (record.type === 'characterData') translateText(record.target);
        else if (record.type === 'attributes') translateAttributes(record.target);
        else record.addedNodes.forEach(translateTree);
      }
    }).observe(document.documentElement, {subtree: true, childList: true, characterData: true, attributes: true, attributeFilter: ATTRIBUTES});
  }
  function set(next) {
    if (!LOCALES[next] || next === lang) return;
    try { localStorage.setItem(KEY, next); } catch { /* storage may be blocked */ }
    const url = new URL(location.href);
    if (!url.searchParams.has('lang')) return location.reload();
    url.searchParams.delete('lang');
    location.replace(url.href);
  }
  function switcher() {
    const select = document.createElement('select');
    select.className = 'lang-switch';
    select.setAttribute('translate', 'no');
    select.setAttribute('aria-label', lang === 'en' ? 'Language' : 'Idioma');
    select.innerHTML = '<option value="pt">PT</option><option value="en">EN</option>';
    select.value = lang;
    select.addEventListener('change', () => set(select.value));
    return select;
  }
  function mount() {
    document.documentElement.lang = LOCALES[lang];
    document.querySelectorAll('[data-lang-switch]').forEach(slot => slot.replaceChildren(switcher()));
    if (lang === 'pt') return;
    document.title = t(document.title);
    const description = document.querySelector('meta[name="description"]');
    if (description) description.content = t(description.content);
    translateTree(document.body);
  }
  // Native dialogs receive Portuguese strings from the modules.
  for (const name of ['alert', 'confirm', 'prompt']) {
    const native = window[name].bind(window);
    window[name] = (message, ...rest) => native(t(message), ...rest);
  }
  observe();
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mount);
  else mount();
  return {t, api, set, lang, locale: LOCALES[lang]};
})();
