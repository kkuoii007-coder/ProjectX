/* Philosophy Reader — small progressive-enhancement script.
   Without JS: translations are visible, nav is open. With JS: translations open on demand. */
(function () {
  'use strict';

  var doc = document;

  function setOpen(sentence, open) {
    var gloss = doc.getElementById(sentence.getAttribute('data-gloss'));
    if (!gloss) { return; }
    sentence.classList.toggle('is-open', open);
    gloss.classList.toggle('is-open', open);
    sentence.setAttribute('aria-expanded', String(open));
  }

  function isOpen(sentence) {
    return sentence.classList.contains('is-open');
  }

  function hasTextSelection() {
    var selection = window.getSelection && window.getSelection();
    return !!selection && !selection.isCollapsed && selection.toString().trim().length > 0;
  }

  function initGlosses() {
    var sentences = doc.querySelectorAll('.s[data-gloss]');
    if (!sentences.length) { return; }

    doc.addEventListener('click', function (event) {
      var sentence = event.target.closest && event.target.closest('.s[data-gloss]');
      if (!sentence || hasTextSelection()) { return; }
      setOpen(sentence, !isOpen(sentence));
      syncToggleAll();
    });

    doc.addEventListener('keydown', function (event) {
      var sentence = event.target.closest && event.target.closest('.s[data-gloss]');
      if (!sentence) { return; }
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        setOpen(sentence, !isOpen(sentence));
        syncToggleAll();
      } else if (event.key === 'Escape' && isOpen(sentence)) {
        setOpen(sentence, false);
        syncToggleAll();
      }
    });

    // The specimen on the home page is a single sentence; it carries no tab stop of its own.
    sentences.forEach(function (sentence) {
      if (!sentence.hasAttribute('tabindex')) {
        sentence.setAttribute('tabindex', '0');
        sentence.setAttribute('role', 'button');
        sentence.setAttribute('aria-expanded', 'false');
        sentence.setAttribute('aria-controls', sentence.getAttribute('data-gloss'));
      }
    });
  }

  var toggleAll = null;

  function syncToggleAll() {
    if (!toggleAll) { return; }
    var all = doc.querySelectorAll('.reader .s[data-gloss]');
    var openCount = doc.querySelectorAll('.reader .s.is-open').length;
    var everythingOpen = all.length > 0 && openCount === all.length;
    toggleAll.setAttribute('aria-pressed', String(everythingOpen));
    toggleAll.textContent = everythingOpen ? 'Скрыть все переводы' : 'Показать все переводы';
  }

  function initToggleAll() {
    var tools = doc.querySelector('[data-tools]');
    toggleAll = doc.querySelector('[data-toggle-all]');
    if (!tools || !toggleAll) { return; }
    tools.hidden = false;
    toggleAll.addEventListener('click', function () {
      var open = toggleAll.getAttribute('aria-pressed') !== 'true';
      var reader = doc.querySelector('[data-reader]');
      if (reader) { reader.classList.toggle('is-all', open); }
      doc.querySelectorAll('.reader .s[data-gloss]').forEach(function (sentence) { setOpen(sentence, open); });
      syncToggleAll();
    });
  }

  function initMenu() {
    var button = doc.querySelector('.menu-toggle');
    var nav = doc.getElementById('site-nav');
    if (!button || !nav) { return; }
    button.addEventListener('click', function () {
      var open = nav.classList.toggle('is-open');
      button.setAttribute('aria-expanded', String(open));
    });
    doc.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && nav.classList.contains('is-open')) {
        nav.classList.remove('is-open');
        button.setAttribute('aria-expanded', 'false');
        button.focus();
      }
    });
  }

  function initReadingLine() {
    var bar = doc.querySelector('[data-reading-line]');
    var article = doc.querySelector('[data-reader]');
    if (!bar || !article) { return; }
    var frame = null;
    function update() {
      frame = null;
      var rect = article.getBoundingClientRect();
      var total = rect.height - window.innerHeight;
      var progress = total > 0 ? Math.min(1, Math.max(0, -rect.top / total)) : 0;
      bar.style.transform = 'scaleX(' + progress.toFixed(4) + ')';
    }
    function schedule() { if (frame === null) { frame = window.requestAnimationFrame(update); } }
    window.addEventListener('scroll', schedule, { passive: true });
    window.addEventListener('resize', schedule);
    update();
  }

  function initRail() {
    var links = doc.querySelectorAll('[data-part-link]');
    if (!links.length || !('IntersectionObserver' in window)) { return; }
    var byId = {};
    links.forEach(function (link) { byId[link.getAttribute('data-part-link')] = link; });
    var observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) { return; }
        links.forEach(function (link) { link.classList.remove('is-current'); link.removeAttribute('aria-current'); });
        var link = byId[entry.target.id];
        if (link) { link.classList.add('is-current'); link.setAttribute('aria-current', 'true'); }
      });
    }, { rootMargin: '-20% 0px -70% 0px' });
    doc.querySelectorAll('.part').forEach(function (part) { observer.observe(part); });
  }

  function init() {
    initGlosses();
    initToggleAll();
    initMenu();
    initReadingLine();
    initRail();
  }

  if (doc.readyState === 'loading') { doc.addEventListener('DOMContentLoaded', init); } else { init(); }
})();
