/* Philosophy Reader — small progressive-enhancement script, no dependencies.
   Without JS: Russian lines are visible, the menu is open, a fork in the text
   is plain paragraphs. With JS: a Russian line opens under its own sentence.
   Nothing here sends, saves or scores anything; the only stored value is the
   reader's choice to show or hide Russian interface captions. */
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

  /* ---- a fork in the text: three buttons, one prepared line ----------------
     With JS the part after a fork stays hidden until the reader picks an
     answer; the choice is kept on this device only (localStorage), so a
     reload does not close what is already read. Any of the three answers
     opens the next part the same way; a made choice can be changed, and
     what is open never closes again. */

  var FORK_KEY = 'pr-fork-choices';

  function readForkChoices() {
    try {
      var stored = JSON.parse(window.localStorage.getItem(FORK_KEY) || '{}');
      return stored && typeof stored === 'object' ? stored : {};
    } catch (error) { return {}; }
  }

  function writeForkChoices(choices) {
    try { window.localStorage.setItem(FORK_KEY, JSON.stringify(choices)); } catch (error) { /* private mode: the choice lasts for this page */ }
  }

  function initForks() {
    var forks = doc.querySelectorAll('[data-fork]');
    if (!forks.length) { return; }
    var saved = readForkChoices();

    forks.forEach(function (fork) {
      var group = fork.querySelector('[data-fork-buttons]');
      var buttons = fork.querySelectorAll('[data-fork-choice]');
      if (!group || !buttons.length) { return; }

      // The part this fork opens; it is hidden until an answer is chosen.
      var nextPart = null;
      if (fork.dataset.forkNext) {
        nextPart = doc.getElementById('part-' + fork.dataset.forkNext);
      }
      var hint = fork.querySelector('[data-fork-hint]');

      function mark(button) {
        buttons.forEach(function (other) {
          var option = doc.getElementById(other.getAttribute('data-fork-choice'));
          var chosen = other === button;
          other.setAttribute('aria-pressed', String(chosen));
          if (option) { option.classList.toggle('is-chosen', chosen); }
        });
        fork.classList.add('has-choice');
      }

      function choose(button, silent) {
        mark(button);
        var key = fork.dataset.forkId;
        if (key) {
          saved[key] = button.getAttribute('data-fork-key');
          writeForkChoices(saved);
        }
        if (hint) { hint.hidden = true; }
        if (nextPart) {
          nextPart.classList.remove('is-locked');
          nextPart.removeAttribute('aria-hidden');
        }
        if (!silent) {
          // Focus moves to the chosen answer's line, so the reader can go on.
          var line = doc.getElementById(button.getAttribute('data-fork-choice'));
          if (line) { line.focus(); }
        }
      }

      buttons.forEach(function (button) {
        button.setAttribute('aria-controls', button.getAttribute('data-fork-choice'));
        button.addEventListener('click', function () { choose(button); });
      });
      group.hidden = false;
      fork.classList.add('is-ready');

      // A choice kept on this device re-opens what was read, without focus.
      var key = fork.dataset.forkId;
      var remembered = key ? saved[key] : null;
      if (remembered) {
        var found = Array.prototype.some.call(buttons, function (button) {
          if (button.getAttribute('data-fork-key') === remembered) { choose(button, true); return true; }
          return false;
        });
        if (found) { return; }
      }
      // Nothing chosen yet (or the stored answer no longer exists): the part
      // after this fork waits for a choice.
      if (nextPart) {
        nextPart.classList.add('is-locked');
        nextPart.setAttribute('aria-hidden', 'true');
      }
    });
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
    // The field named by the group shows the same example as its placeholder.
    var field = doc.getElementById(group.getAttribute('data-topic-examples') || '');
    var current = 0;
    function showNext() {
      items[current].hidden = true;
      current = (current + 1) % items.length;
      items[current].hidden = false;
      var example = items[current].getAttribute('data-example');
      if (field && example) { field.placeholder = example; }
    }
    window.setInterval(showNext, 4000);
  }

  function init() {
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
      root.removeAttribute('data-js-ready');
      root.classList.remove('js');
      root.classList.remove('ui-ru-off');
    }
  }

  if (doc.readyState === 'loading') { doc.addEventListener('DOMContentLoaded', init); } else { init(); }
})();
