/* Philosophy Reader — small progressive-enhancement script, no dependencies.
   Without JS: Russian lines are visible, the menu is open, a fork in the text
   is plain paragraphs and the whole text is there. With JS: a Russian line
   opens under its own sentence, and a text with forks opens answer by answer.
   Nothing here sends or scores anything. Two things are kept on this device
   only: the choice to show or hide Russian interface captions, and how far
   the reader has opened each text. */
(function () {
  'use strict';

  var doc = document;
  var root = doc.documentElement;
  var RU_KEY = 'pr-ui-ru';

  /* ---- a Russian line under its sentence ---------------------------------- */

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

  function sentenceOf(event) {
    return event.target.closest ? event.target.closest('.s[data-gloss]') : null;
  }

  function initGlosses() {
    var sentences = doc.querySelectorAll('.s[data-gloss]');
    if (!sentences.length) { return; }

    doc.addEventListener('click', function (event) {
      var sentence = sentenceOf(event);
      if (!sentence || hasTextSelection()) { return; }
      setOpen(sentence, !isOpen(sentence));
    });

    doc.addEventListener('keydown', function (event) {
      var sentence = sentenceOf(event);
      if (!sentence) { return; }
      if (event.key === 'Enter' || event.key === ' ' || event.key === 'Spacebar') {
        event.preventDefault();
        setOpen(sentence, !isOpen(sentence));
      } else if (event.key === 'Escape' && isOpen(sentence)) {
        setOpen(sentence, false);
      }
    });

    // The no-JS page is readable prose. Button semantics come only with working handlers.
    sentences.forEach(function (sentence) {
      sentence.setAttribute('tabindex', '0');
      sentence.setAttribute('role', 'button');
      sentence.setAttribute('aria-controls', sentence.getAttribute('data-gloss'));
      // A line marked data-open starts open, so the reader sees what a tap does.
      setOpen(sentence, sentence.hasAttribute('data-open'));
    });
  }

  /* ---- RU: show or hide Russian interface captions ------------------------ */

  function readPref() {
    try { return window.localStorage.getItem(RU_KEY); } catch (error) { return null; }
  }

  function writePref(value) {
    try { window.localStorage.setItem(RU_KEY, value); } catch (error) { /* private mode: the choice lasts for this page */ }
  }

  function initRuToggle() {
    var button = doc.querySelector('[data-ru-toggle]');
    if (!button) {
      // Pages that are Russian throughout have no switch and never hide captions.
      root.classList.remove('ui-ru-off');
      return;
    }
    function apply(show) {
      root.classList.toggle('ui-ru-off', !show);
      button.setAttribute('aria-pressed', String(show));
    }
    apply(readPref() !== 'off');
    button.hidden = false;
    button.addEventListener('click', function () {
      var show = button.getAttribute('aria-pressed') !== 'true';
      apply(show);
      writePref(show ? 'on' : 'off');
    });
  }

  /* ---- the menu on narrow service pages ----------------------------------- */

  function initMenu() {
    var button = doc.querySelector('.menu-toggle');
    var nav = doc.getElementById('site-nav');
    if (!button || !nav) { return; }
    function setMenu(open) {
      nav.classList.toggle('is-open', open);
      button.setAttribute('aria-expanded', String(open));
    }
    button.addEventListener('click', function () {
      setMenu(!nav.classList.contains('is-open'));
    });
    nav.addEventListener('click', function (event) {
      if (event.target.closest('a')) { setMenu(false); }
    });
    doc.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && nav.classList.contains('is-open')) {
        setMenu(false);
        button.focus();
      }
    });
  }

  /* ---- forks in the text: the reader opens it answer by answer -------------
     A text with forks opens as a chain: the first part and its question, then
     each answer shows its prepared line, the next part and the next question;
     "About this text" and "Next question" come with the last part.
     The page is drawn from one state, { answers, upto }: the answer given at
     each fork and the number of parts that are open. render() sets the whole
     page from it, so a later part can never be open above a closed one.
     The state is kept on this device only, under a name of its own for each
     text and revision (data-fork-scope): another text, or a new revision of
     this one, starts closed. What is open never closes again; an answer can
     be changed. A link to #part-N opens the text up to part N and answers
     nothing for the reader. Nothing is sent and nothing is scored.
     The same text can be open twice: in another tab, or as a page the browser
     kept for the "back" button. So this page never writes from its memory
     alone: before every change it takes what is kept (catchUp), and it does
     the same when the other page writes or when it is shown again. */

  var FORK_PREFIX = 'pr-fork:';
  var FORK_RETIRED_KEY = 'pr-fork-choices'; // one shared key for every text: no longer read, only removed

  function readForkState(name) {
    try { return JSON.parse(window.localStorage.getItem(name) || 'null'); } catch (error) { return null; }
  }

  function writeForkState(name, state) {
    try { window.localStorage.setItem(name, JSON.stringify(state)); } catch (error) { /* private mode: the state lasts for this page */ }
  }

  // Answers to an earlier revision of this text are of no use to anyone.
  function dropStaleForkStates(scope) {
    try {
      var storage = window.localStorage;
      var sameText = FORK_PREFIX + scope.slice(0, scope.lastIndexOf(':') + 1);
      var stale = [FORK_RETIRED_KEY];
      for (var i = 0; i < storage.length; i += 1) {
        var name = storage.key(i);
        if (name && name.indexOf(sameText) === 0 && name !== FORK_PREFIX + scope) { stale.push(name); }
      }
      stale.forEach(function (name) { storage.removeItem(name); });
    } catch (error) { /* no storage: nothing to clean */ }
  }

  function initForks() {
    var reader = doc.querySelector('[data-fork-scope]');
    if (!reader) { return; }
    var scope = reader.getAttribute('data-fork-scope');
    var name = FORK_PREFIX + scope;

    // The chain, in the order of the page: parts, and the fork after each.
    var parts = [];
    var forks = [];
    Array.prototype.forEach.call(reader.querySelectorAll('[data-part], [data-fork]'), function (el) {
      if (el.hasAttribute('data-part')) { parts.push(el); return; }
      var group = el.querySelector('[data-fork-buttons]');
      var buttons = Array.prototype.slice.call(el.querySelectorAll('[data-fork-choice]'));
      if (!parts.length || !group || !buttons.length) { return; }
      forks.push({
        el: el,
        after: parts.length, // this fork closes the way from part `after` to the next one
        id: parts[parts.length - 1].getAttribute('data-part'),
        group: group,
        buttons: buttons,
        hint: el.querySelector('[data-fork-hint]')
      });
    });
    var ends = reader.querySelectorAll('[data-reader-end]');
    var total = parts.length;
    if (!total) { return; }

    function forkAfter(number) {
      for (var i = 0; i < forks.length; i += 1) { if (forks[i].after === number) { return forks[i]; } }
      return null;
    }

    var state = { answers: {}, upto: 1 };

    // The one rule of the chain: a part is followed by the next one as soon as
    // nothing stands between them, or the fork between them has an answer.
    function advance() {
      while (state.upto < total) {
        var fork = forkAfter(state.upto);
        if (fork && !state.answers[fork.id]) { break; }
        state.upto += 1;
      }
    }

    // Take what is kept on the device into the state of this page. The place
    // never goes back. A kept answer is the later one: every answer is written
    // at once, so what this page remembers of that fork is older.
    function catchUp() {
      var stored = readForkState(name);
      if (stored && typeof stored === 'object') {
        var upto = parseInt(stored.upto, 10);
        if (upto >= 1) { state.upto = Math.max(state.upto, Math.min(upto, total)); }
        var answers = stored.answers && typeof stored.answers === 'object' ? stored.answers : {};
        forks.forEach(function (fork) {
          var known = fork.buttons.some(function (button) { return button.getAttribute('data-fork-key') === answers[fork.id]; });
          if (known) { state.answers[fork.id] = answers[fork.id]; }
        });
      }
      advance();
      // An answer to a fork the reader has not reached is not kept.
      forks.forEach(function (fork) { if (fork.after > state.upto) { delete state.answers[fork.id]; } });
    }

    function render() {
      parts.forEach(function (part, index) { part.classList.toggle('is-reached', index < state.upto); });
      forks.forEach(function (fork) {
        var answer = state.answers[fork.id] || null;
        // Passed: the text after this fork is open (by an answer or by a link).
        var passed = fork.after < state.upto || !!answer;
        fork.el.classList.toggle('is-reached', fork.after <= state.upto);
        fork.el.classList.toggle('is-passed', passed);
        if (fork.hint) { fork.hint.hidden = passed; }
        fork.buttons.forEach(function (button) {
          var chosen = button.getAttribute('data-fork-key') === answer;
          var line = doc.getElementById(button.getAttribute('data-fork-choice'));
          button.setAttribute('aria-pressed', String(chosen));
          if (line) { line.classList.toggle('is-chosen', chosen); }
        });
      });
      Array.prototype.forEach.call(ends, function (block) { block.classList.toggle('is-reached', state.upto >= total); });
    }

    function choose(fork, button) {
      catchUp(); // first what another page has kept, then this answer on top of it
      state.answers[fork.id] = button.getAttribute('data-fork-key');
      advance();
      writeForkState(name, state);
      render();
      // Focus moves to the chosen answer's line, so the reader can go on.
      var line = doc.getElementById(button.getAttribute('data-fork-choice'));
      if (line) { line.focus(); }
    }

    // A link to #part-N opens the text up to that part. The browser cannot
    // scroll to a closed part, so the scroll is repeated once it is open.
    function openFromHash(arriving) {
      var match = /^#part-(\d+)$/.exec(window.location.hash || '');
      var target = match ? doc.getElementById('part-' + match[1]) : null;
      var index = parts.indexOf(target);
      if (index < 0) { return; }
      catchUp();
      var wasClosed = index >= state.upto;
      if (wasClosed) {
        state.upto = index + 1;
        advance();
        writeForkState(name, state);
      }
      render();
      if (wasClosed || arriving) {
        target.setAttribute('tabindex', '-1');
        target.focus({ preventScroll: true }); // Tab goes on from this part, as after any jump inside a page
        // A page opened by the link starts at the part at once, as the browser does it.
        target.scrollIntoView(arriving ? { behavior: 'instant', block: 'start' } : true);
      }
    }

    // On a reload or a step back the browser returns to the old place itself.
    function arrivedByLink() {
      try {
        var entry = window.performance.getEntriesByType('navigation')[0];
        return !entry || entry.type === 'navigate';
      } catch (error) { return true; }
    }

    forks.forEach(function (fork) {
      fork.buttons.forEach(function (button) {
        button.setAttribute('aria-controls', button.getAttribute('data-fork-choice'));
        button.addEventListener('click', function () { choose(fork, button); });
      });
      fork.group.hidden = false;
    });

    function refresh() {
      catchUp();
      render();
    }

    dropStaleForkStates(scope);
    refresh();
    openFromHash(arrivedByLink());
    window.addEventListener('hashchange', function () { openFromHash(false); });
    // Another tab with this text has written its place: follow it. Nothing is
    // written here and the page does not move.
    window.addEventListener('storage', function (event) { if (event.key === name) { refresh(); } });
    // The browser shows a page it kept (the "back" button): it may be behind.
    window.addEventListener('pageshow', function (event) { if (event.persisted) { refresh(); } });
  }

  /* ---- rotating topic examples on the workshop entry form ------------------ */

  function initTopicExamples() {
    var group = doc.querySelector('[data-topic-examples]');
    if (!group) { return; }
    var items = group.querySelectorAll('span');
    if (items.length < 2) { return; }
    if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      return; // Keep the first example only; the form stays fully usable.
    }
    var current = 0;
    function showNext() {
      items[current].hidden = true;
      current = (current + 1) % items.length;
      items[current].hidden = false;
    }
    window.setInterval(showNext, 4000);
  }

  // The page without the script: nothing is hidden, and the controls the
  // script has shown so far go away with the js class (main.css).
  function plainPage() {
    root.removeAttribute('data-js-ready');
    root.classList.remove('js');
    root.classList.remove('ui-ru-off');
    try {
      // Sentences are plain prose again, not buttons that open nothing.
      Array.prototype.forEach.call(doc.querySelectorAll('.s[data-gloss]'), function (sentence) {
        ['role', 'tabindex', 'aria-controls', 'aria-expanded'].forEach(function (name) { sentence.removeAttribute(name); });
      });
    } catch (error) { /* the page is readable already */ }
  }

  function init() {
    // The script came after the page was given back to the reader (the inline
    // script in <head> waits three seconds). The reader may be in the middle
    // of the whole text by now: nothing closes under them. The page stays
    // plain until the next load.
    if (root.hasAttribute('data-js-off')) { return; }
    try {
      initGlosses();
      initRuToggle();
      initMenu();
      initForks();
      initTopicExamples();
      // Enhance only when everything above worked. The page waits for this
      // mark: without it the inline script in <head> takes the js class back.
      root.setAttribute('data-js-ready', '');
      root.classList.add('js');
    } catch (error) {
      // A broken script must not hide anything: back to the readable no-JS page.
      plainPage();
    }
  }

  if (doc.readyState === 'loading') { doc.addEventListener('DOMContentLoaded', init); } else { init(); }
})();
