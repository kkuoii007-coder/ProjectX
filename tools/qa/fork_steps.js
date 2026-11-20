#!/usr/bin/env node
/* Browser check of the fork in the reader: steps, assertions, screenshots.
   Chromium through Playwright. It runs in the `browser` job of
   .github/workflows/tests.yml on every pull request, and by hand as an
   acceptance tool.

   How to run by hand (a throw-away database, never a server):

     export PR_BROWSER_CHECK=1          # tools/qa/seed_browser_check.py asks for it
     python manage.py migrate
     python tools/qa/seed_browser_check.py --olive-copy
     python manage.py runserver 127.0.0.1:8011 --noreload --nothreading
     node tools/qa/fork_steps.js --no-shots \
       --login=browser-check-reader:browser-check-only \
       --tones=clay:/read/what-does-it-mean-to-be-brave/,sea:/read/why-do-people-live-in-cities/,olive:/read/browser-check-olive/ \
       --revised=/read/browser-check-olive/ \
       --republish="python tools/qa/seed_browser_check.py --new-revision"

   (--nothreading: the development server then holds one database connection;
   with threads and CONN_MAX_AGE it leaves one behind for every browser tab.)

   Playwright is not a dependency of the project: no package.json here. The
   script takes it from PLAYWRIGHT_PATH (the workflow installs it outside the
   checkout), from `playwright` on the usual Node path or from
   /home/user/node_modules/playwright.

   Options:
     --base=http://127.0.0.1:8011
     --text=/read/a/     the text the steps are walked on (three forks)
     --other=/read/b/    another text with forks
     --tones=clay:/read/a/,sea:/read/b/,olive:/read/c/
                         texts for the hint contrast (step 8); without it the
                         tone of --text and of --other is measured
     --revised=/read/c/  a text for the "new revision" step (6), with
     --republish="cmd"   a shell command that publishes a new revision of it;
                         the script reads the text to the end, runs the
                         command and opens the text again. Without these two
                         only the planted state of another revision is checked
     --login=name:pass   a reader, to check that the reader's tools stay
     --steps=0,1,7       run only these steps (default: all)
     --out=dir           screenshots (default: docs/screenshots/pr1-fork)
     --no-shots          assertions only

   Steps (each at 390 and 1440 px):
     0 first open               8 hint close-up and contrast on each tone
     1 after answer 1           9 without JavaScript
     2 after answer 2          10 print (with JavaScript on)
     3 after answer 3: the end 11 edge cases
     4 reload                  12 the same text in two tabs
     5 the other text: closed  13 a page kept for the "back" button
     6 new revision: closed    14 the script comes late
     7 link to #part-3         15 the script fails half-way
   Steps 11-15 take no screenshots.

   A step that cannot go on is one failed check; the run goes on.
   Exit code: 0 when every check holds, 1 otherwise.

   The files in docs/screenshots/pr1-fork were taken by this script in two
   runs (steps 0-5, 7, 9-11 with two texts published; then steps 6 and 8 with
   a temporary olive text published as well) and then reduced to a 64-colour
   palette to keep the repository small; the hint close-ups (08) are as taken. */

'use strict';

const fs = require('fs');
const path = require('path');
const { execSync } = require('child_process');

function loadPlaywright() {
  const places = [process.env.PLAYWRIGHT_PATH, 'playwright', '/home/user/node_modules/playwright'].filter(Boolean);
  for (const place of places) {
    try { return require(place); } catch (error) { /* try the next place */ }
  }
  console.error('Playwright not found. Set PLAYWRIGHT_PATH to its folder.');
  process.exit(2);
}

const args = {};
for (const arg of process.argv.slice(2)) {
  const match = /^--([a-z-]+)(?:=(.*))?$/.exec(arg);
  if (!match) { console.error('Unknown argument: ' + arg); process.exit(2); }
  args[match[1]] = match[2] === undefined ? true : match[2];
}

const BASE = (args.base || process.env.BASE || 'http://127.0.0.1:8011').replace(/\/$/, '');
const TEXT = args.text || '/read/what-does-it-mean-to-be-brave/';
const OTHER = args.other || '/read/why-do-people-live-in-cities/';
const OUT = path.resolve(args.out || path.join(__dirname, '..', '..', 'docs', 'screenshots', 'pr1-fork'));
const SHOTS = !args['no-shots'];
const STEPS = args.steps ? String(args.steps).split(',').map(Number) : null;
const VIEWPORTS = [{ width: 390, height: 844 }, { width: 1440, height: 900 }];
const TONES = { clay: '#8a3a26', sea: '#1f4d5c', olive: '#4a5626' };
const KEYS = ['yes', 'no', 'not_sure'];
const TIMEOUT = 5000; // ms to wait for anything on a page
const LATE = 5000; // ms the script is held back in step 14; the page waits 3000 for it

const wanted = (step) => !STEPS || STEPS.indexOf(step) >= 0;
let failed = 0;
let passed = 0;
let skipped = 0;

function skip(label) {
  skipped += 1;
  console.log('  skip  ' + label);
}

function check(label, ok, detail) {
  if (ok) { passed += 1; console.log('  ok    ' + label); return; }
  failed += 1;
  console.log('  FAIL  ' + label + (detail === undefined ? '' : '\n        ' + (typeof detail === 'string' ? detail : JSON.stringify(detail))));
}

const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

/* A step that cannot go on (a button that never comes, a broken page) is a
   failed check with its reason, and the run goes on to the next step. */
async function step(tag, number, title, body) {
  if (!wanted(number)) { return; }
  console.log('\n' + tag + 'step ' + title);
  try {
    await body();
  } catch (error) {
    check(tag + 'step ' + title + ': ran to its end', false, String(error && error.message ? error.message : error).split('\n').slice(0, 3).join(' | '));
  }
}

async function newContext(browser, options) {
  const context = await browser.newContext(options);
  context.setDefaultTimeout(TIMEOUT);
  context.setDefaultNavigationTimeout(3 * TIMEOUT);
  return context;
}

/* The place kept for the text of this view, or null. */
function kept(view) {
  try { return JSON.parse(view.storage['pr-fork:' + view.scope] || 'null'); } catch (error) { return null; }
}

/* What the reader sees: taken from layout, not from classes. */
function look(page) {
  return page.evaluate(() => {
    const shown = (el) => !!el && el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden';
    const all = (selector, root) => Array.prototype.slice.call((root || document).querySelectorAll(selector));
    const numbers = (list) => list.map((el, index) => (shown(el) ? index + 1 : 0)).filter(Boolean);
    const forks = all('[data-fork]');
    const storage = {};
    try { for (let i = 0; i < localStorage.length; i += 1) { storage[localStorage.key(i)] = localStorage.getItem(localStorage.key(i)); } } catch (error) { storage.error = String(error); }
    const closed = all('.part, [data-fork], [data-reader-end], .choice__option').filter((el) => !shown(el));
    const tools = document.querySelector('.reader-tools');
    return {
      parts: numbers(all('.part')),
      forks: numbers(forks),
      hints: numbers(forks.map((fork) => fork.querySelector('[data-fork-hint]'))),
      buttons: numbers(forks.map((fork) => fork.querySelector('[data-fork-buttons]'))),
      readOn: numbers(forks.map((fork) => fork.querySelector('.choice__next'))),
      lines: forks.map((fork) => all('.choice__option', fork).filter(shown).map((el) => el.id.replace(/^fork-\d+-|-line$/g, '').replace('-', '_'))),
      labels: all('.choice__label').filter(shown).length,
      pressed: forks.map((fork) => all('[data-fork-choice][aria-pressed="true"]', fork).map((el) => el.getAttribute('data-fork-key'))),
      words: shown(document.querySelector('.end-block.terms')),
      about: shown(document.querySelector('.end-block.about')),
      next: shown(document.querySelector('.end-block.next')),
      tools: tools ? shown(tools) : null,
      // What is closed is out of the layout and of the accessibility tree.
      closedByDisplay: closed.every((el) => getComputedStyle(el).display === 'none'),
      overflow: document.documentElement.scrollWidth - window.innerWidth,
      scope: (document.querySelector('[data-fork-scope]') || { getAttribute: () => null }).getAttribute('data-fork-scope'),
      focus: document.activeElement ? (document.activeElement.id || document.activeElement.tagName) : null,
      scrollY: Math.round(window.scrollY),
      storage: storage,
    };
  });
}

/* One line per view: what must be seen, in short. */
function brief(name, value) {
  if (value === true) { return name; }
  if (value === false) { return 'no ' + name; }
  if (name === 'lines' || name === 'pressed') { return name + ' ' + value.map((list) => list.join('+') || '-').join(' / '); }
  if (Array.isArray(value)) { return name + ' ' + (value.join(',') || 'none'); }
  return name + ' ' + value;
}

function expectView(label, view, want) {
  const names = Object.keys(want);
  const wrong = names.filter((name) => !same(view[name], want[name]));
  check(label + ': ' + names.map((name) => brief(name, want[name])).join('; '), wrong.length === 0, wrong.map((name) => name + ' is ' + JSON.stringify(view[name])).join('; '));
}

const CLOSED = { parts: [1], forks: [1], hints: [1], buttons: [1], readOn: [], lines: [[], [], []], pressed: [[], [], []], words: true, about: false, next: false, closedByDisplay: true };
const OPEN_ALL = { parts: [1, 2, 3, 4], forks: [1, 2, 3], hints: [], buttons: [1, 2, 3], words: true, about: true, next: true };

async function shot(page, name, options) {
  if (!SHOTS) { return; }
  fs.mkdirSync(OUT, { recursive: true });
  await page.evaluate(() => document.fonts.ready);
  await page.mouse.move(0, 0); // no hover underline under the pointer left by a click
  await page.screenshot(Object.assign({ path: path.join(OUT, name + '.png'), fullPage: true, animations: 'disabled' }, options || {}));
  console.log('  shot  ' + name + '.png');
}

function watch(page, errors) {
  page.on('pageerror', (error) => errors.push('pageerror: ' + error.message));
  page.on('console', (message) => { if (message.type() === 'error') { errors.push('console: ' + message.text()); } });
}

async function open(page, url) {
  await page.goto(BASE + url, { waitUntil: 'load' });
  await page.evaluate(() => document.fonts.ready);
}

async function answer(page, fork, key) {
  await page.locator('[data-fork]').nth(fork - 1).locator('[data-fork-key="' + key + '"]').click();
}

/* Wait for a smooth scroll to end, then say where the part is. */
async function settledTop(page, selector) {
  let last = null;
  for (let i = 0; i < 40; i += 1) {
    const top = await page.evaluate((s) => Math.round(document.querySelector(s).getBoundingClientRect().top), selector);
    if (top === last) { return top; }
    last = top;
    await page.waitForTimeout(100);
  }
  return last;
}

/* Contrast of the hint: the colours the browser really paints. */
function hintContrast(page) {
  return page.evaluate(() => {
    const rgba = (value) => {
      const m = /rgba?\(([^)]+)\)/.exec(value);
      const n = m ? m[1].split(/[,\s/]+/).filter(Boolean).map(Number) : [0, 0, 0, 0];
      return { r: n[0], g: n[1], b: n[2], a: n.length > 3 ? n[3] : 1 };
    };
    const over = (top, under) => ({ r: top.r * top.a + under.r * (1 - top.a), g: top.g * top.a + under.g * (1 - top.a), b: top.b * top.a + under.b * (1 - top.a), a: 1 });
    const lum = (c) => {
      const f = (v) => { v /= 255; return v <= 0.04045 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
      return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b);
    };
    const hex = (c) => '#' + [c.r, c.g, c.b].map((v) => ('0' + Math.round(v).toString(16)).slice(-2)).join('');
    function background(el) {
      const layers = [];
      let images = 0;
      for (let node = el; node; node = node.parentElement) {
        const style = getComputedStyle(node);
        if (style.backgroundImage !== 'none') { images += 1; }
        const colour = rgba(style.backgroundColor);
        if (colour.a > 0) { layers.push(colour); }
        if (colour.a === 1) { break; }
      }
      let result = { r: 255, g: 255, b: 255, a: 1 };
      for (let i = layers.length - 1; i >= 0; i -= 1) { result = over(layers[i], result); }
      return { colour: result, images: images };
    }
    function measure(el, label) {
      const style = getComputedStyle(el);
      let opacity = 1;
      for (let node = el; node; node = node.parentElement) { opacity *= parseFloat(getComputedStyle(node).opacity); }
      const back = background(el);
      const ink = rgba(style.color);
      const text = over({ r: ink.r, g: ink.g, b: ink.b, a: ink.a * opacity }, back.colour);
      const hi = Math.max(lum(text), lum(back.colour));
      const lo = Math.min(lum(text), lum(back.colour));
      return { line: label, text: hex(text), background: hex(back.colour), images: back.images, opacity: opacity, ratio: Math.round(((hi + 0.05) / (lo + 0.05)) * 100) / 100, size: style.fontSize, weight: style.fontWeight };
    }
    const hint = document.querySelector('[data-fork-hint]');
    return [measure(hint, 'en'), measure(hint.querySelector('.ru-ui'), 'ru')];
  });
}

async function main() {
  const { chromium } = loadPlaywright();
  const browser = await chromium.launch();
  const errors = [];
  const contrastRows = [];
  console.log('fork_steps: ' + BASE + '  text ' + TEXT + '  other ' + OTHER);

  for (const viewport of VIEWPORTS) {
    const w = viewport.width;
    const tag = '[' + w + '] ';
    const context = await newContext(browser, { viewport: viewport });
    const page = await context.newPage();
    watch(page, errors);
    const requests = [];

    await step(tag, 0, '0: first open', async () => {
      await open(page, TEXT);
      const view = await look(page);
      expectView(tag + '0', view, CLOSED);
      check(tag + '0: nothing is stored before an answer', Object.keys(view.storage).filter((key) => key.indexOf('pr-fork') === 0).length === 0, view.storage);
      check(tag + '0: scope names the text and a revision', new RegExp('^' + TEXT.replace(/^\/read\/|\/$/g, '') + ':[0-9a-f]{16}$').test(view.scope || ''), view.scope);
      check(tag + '0: no sideways scroll', view.overflow <= 0, view.overflow);
      await shot(page, w + '-00-first-open');
    });

    await step(tag, 1, '1: after answer 1', async () => {
      if (!wanted(0)) { await open(page, TEXT); }
      page.on('request', (request) => requests.push(request.method() + ' ' + request.url()));
      await answer(page, 1, 'yes');
      const view = await look(page);
      expectView(tag + '1', view, { parts: [1, 2], forks: [1, 2], hints: [2], readOn: [1], lines: [['yes'], [], []], pressed: [['yes'], [], []], labels: 0, words: true, about: false, next: false, closedByDisplay: true });
      check(tag + '1: focus is on the line of the answer', view.focus === 'fork-1-yes-line', view.focus);
      check(tag + '1: the answer is kept under the scope of this text', same(kept(view), { answers: { 1: 'yes' }, upto: 2 }), view.storage);
      await shot(page, w + '-01-after-answer-1');
    });

    await step(tag, 2, '2: after answer 2', async () => {
      await answer(page, 2, 'no');
      const view = await look(page);
      expectView(tag + '2', view, { parts: [1, 2, 3], forks: [1, 2, 3], hints: [3], readOn: [1, 2], lines: [['yes'], ['no'], []], pressed: [['yes'], ['no'], []], words: true, about: false, next: false, closedByDisplay: true });
      check(tag + '2: focus is on the line of the answer', view.focus === 'fork-2-no-line', view.focus);
      await shot(page, w + '-02-after-answer-2');
    });

    await step(tag, 3, '3: after answer 3, the end of the text', async () => {
      // The third answer is given from the keyboard: the buttons are real buttons.
      await page.locator('[data-fork]').nth(2).locator('[data-fork-key="not_sure"]').focus();
      await page.keyboard.press('Space');
      const view = await look(page);
      expectView(tag + '3', view, Object.assign({}, OPEN_ALL, { readOn: [1, 2, 3], lines: [['yes'], ['no'], ['not_sure']], pressed: [['yes'], ['no'], ['not_sure']] }));
      check(tag + '3: focus is on the line of the answer', view.focus === 'fork-3-not-sure-line', view.focus);
      check(tag + '3: no sideways scroll', view.overflow <= 0, view.overflow);
      check(tag + '3: three answers sent nothing to the server', requests.length === 0, requests);
      await shot(page, w + '-03-after-answer-3-end');

      // An answer can be changed; nothing closes.
      await answer(page, 1, 'no');
      const changed = await look(page);
      expectView(tag + '3 (answer 1 changed)', changed, Object.assign({}, OPEN_ALL, { lines: [['no'], ['no'], ['not_sure']], pressed: [['no'], ['no'], ['not_sure']] }));
      await answer(page, 1, 'yes');
    });

    await step(tag, 4, '4: reload', async () => {
      await page.reload({ waitUntil: 'load' });
      const view = await look(page);
      expectView(tag + '4', view, Object.assign({}, OPEN_ALL, { readOn: [1, 2, 3], lines: [['yes'], ['no'], ['not_sure']], pressed: [['yes'], ['no'], ['not_sure']] }));
      await shot(page, w + '-04-after-reload');
    });

    await step(tag, 5, '5: the other text, first open after the first one is read', async () => {
      const before = await look(page);
      await open(page, OTHER);
      const view = await look(page);
      expectView(tag + '5', view, CLOSED);
      check(tag + '5: the other text has a scope of its own', !!view.scope && view.scope !== before.scope, [before.scope, view.scope]);
      check(tag + '5: the first text keeps its place', before.storage['pr-fork:' + before.scope] === view.storage['pr-fork:' + before.scope] && !view.storage['pr-fork:' + view.scope], view.storage);
      await shot(page, w + '-05-second-text-first-open');
      await open(page, TEXT);
      expectView(tag + '5 (back to the first text)', await look(page), OPEN_ALL);
    });

    await step(tag, 6, '6a: a kept place of another revision of this text (planted)', async () => {
      const fresh = await newContext(browser, { viewport: viewport });
      const tab = await fresh.newPage();
      watch(tab, errors);
      await open(tab, TEXT);
      const slug = (await look(tab)).scope.split(':')[0];
      await tab.evaluate((name) => {
        localStorage.setItem('pr-fork:' + name + ':0000000000000000', JSON.stringify({ answers: { 1: 'yes', 2: 'yes', 3: 'yes' }, upto: 4 }));
        localStorage.setItem('pr-fork-choices', JSON.stringify({ 'fork-1': 'yes', 'fork-2': 'no', 'fork-3': 'yes' }));
        localStorage.setItem('pr-fork:another-text:0000000000000000', '{"answers":{"1":"no"},"upto":2}');
        localStorage.setItem('pr-ui-ru', 'on');
      }, slug);
      await tab.reload({ waitUntil: 'load' });
      const view = await look(tab);
      expectView(tag + '6a', view, CLOSED);
      check(tag + '6a: the old revision and the old shared key are removed, the rest stays', same(Object.keys(view.storage).sort(), ['pr-fork:another-text:0000000000000000', 'pr-ui-ru']), Object.keys(view.storage));
      if (!args.republish) { await shot(tab, w + '-06-new-revision-first-open'); }
      await fresh.close();
    });

    await step(tag, 7, '7: a link to #part-3 on a clean profile', async () => {
      const fresh = await newContext(browser, { viewport: viewport });
      const tab = await fresh.newPage();
      watch(tab, errors);
      await open(tab, TEXT + '#part-3');
      const top = await settledTop(tab, '#part-3');
      const view = await look(tab);
      expectView(tag + '7', view, { parts: [1, 2, 3], forks: [1, 2, 3], hints: [3], readOn: [1, 2], lines: [[], [], []], pressed: [[], [], []], words: true, about: false, next: false, closedByDisplay: true });
      check(tag + '7: the page is scrolled to part 3', top >= 0 && top <= 40 && view.scrollY > 0, { top: top, scrollY: view.scrollY });
      check(tag + '7: focus is on part 3', view.focus === 'part-3', view.focus);
      check(tag + '7: the link gave no answers', same(kept(view), { answers: {}, upto: 3 }), view.storage);
      await shot(tab, w + '-07-link-to-part-3', { fullPage: false });
      await shot(tab, w + '-07-link-to-part-3-whole-page');

      await open(tab, TEXT);
      expectView(tag + '7 (opened again without the link)', await look(tab), { parts: [1, 2, 3], forks: [1, 2, 3], about: false, next: false });
      await answer(tab, 1, 'no');
      expectView(tag + '7 (an answer at a passed fork)', await look(tab), { parts: [1, 2, 3], hints: [3], lines: [['no'], [], []], about: false });

      const clean = await newContext(browser, { viewport: viewport });
      const other = await clean.newPage();
      watch(other, errors);
      await open(other, TEXT);
      expectView(tag + '7 (hashchange) before', await look(other), { parts: [1] });
      await other.evaluate(() => { window.location.hash = '#part-2'; });
      const second = await settledTop(other, '#part-2');
      const moved = await look(other);
      expectView(tag + '7 (hashchange to #part-2)', moved, { parts: [1, 2], forks: [1, 2], hints: [2], lines: [[], [], []], about: false, next: false });
      check(tag + '7 (hashchange): the page is scrolled to part 2', second >= 0 && second <= 40 && moved.scrollY > 0, { top: second, scrollY: moved.scrollY });
      await other.evaluate(() => { window.location.hash = '#part-4'; });
      await settledTop(other, '#part-4');
      expectView(tag + '7 (hashchange to #part-4)', await look(other), Object.assign({}, OPEN_ALL, { lines: [[], [], []] }));
      await other.evaluate(() => { window.location.hash = '#part-1'; });
      await other.evaluate(() => { window.location.hash = '#part-99'; });
      await other.evaluate(() => { window.location.hash = '#terms-title'; });
      expectView(tag + '7 (links back and to nowhere close nothing)', await look(other), OPEN_ALL);
      await clean.close();
      await fresh.close();
    });

    await step(tag, 9, '9: without JavaScript', async () => {
      const plain = await newContext(browser, { viewport: viewport, javaScriptEnabled: false });
      const tab = await plain.newPage();
      await tab.goto(BASE + TEXT, { waitUntil: 'load' });
      const view = await look(tab); // the probe runs through the debugger; the page's own scripts are off
      check(tag + '9: the page ran no script', (await tab.evaluate(() => document.documentElement.className)) === '' && view.storage && Object.keys(view.storage).length === 0);
      expectView(tag + '9', view, { parts: [1, 2, 3, 4], forks: [1, 2, 3], hints: [], buttons: [], readOn: [1, 2, 3], lines: [KEYS, KEYS, KEYS], labels: 9, words: true, about: true, next: true });
      await shot(tab, w + '-09-without-js');
      await plain.close();

      // The script file does not arrive: the inline script gives the page back.
      const broken = await newContext(browser, { viewport: viewport });
      const lost = await broken.newPage();
      await lost.route('**/reader*.js', (route) => route.abort());
      await lost.goto(BASE + TEXT, { waitUntil: 'load' });
      expectView(tag + '9 (reader.js failed to load)', await look(lost), { parts: [1, 2, 3, 4], forks: [1, 2, 3], buttons: [], lines: [KEYS, KEYS, KEYS], about: true, next: true });
      await broken.close();
    });

    await step(tag, 10, '10: print, JavaScript on, the text closed on screen', async () => {
      const fresh = await newContext(browser, { viewport: viewport });
      const tab = await fresh.newPage();
      watch(tab, errors);
      await open(tab, TEXT);
      await answer(tab, 1, 'yes');
      expectView(tag + '10 on screen', await look(tab), { parts: [1, 2], about: false });
      await tab.emulateMedia({ media: 'print' });
      const view = await look(tab);
      expectView(tag + '10 in print', view, { parts: [1, 2, 3, 4], forks: [1, 2, 3], hints: [], buttons: [], readOn: [], lines: [KEYS, KEYS, KEYS], labels: 9, words: true, about: true, next: true });
      await shot(tab, w + '-10-print');
      await tab.emulateMedia({ media: 'screen' });
      expectView(tag + '10 back on screen', await look(tab), { parts: [1, 2], about: false });
      await fresh.close();
    });

    await step(tag, 11, '11: edge cases', async () => {
      // Broken or foreign data under the key of this text.
      const fresh = await newContext(browser, { viewport: viewport });
      const tab = await fresh.newPage();
      watch(tab, errors);
      await open(tab, TEXT);
      const scope = (await look(tab)).scope;
      const cases = [
        ['an answer to a fork not reached yet', '{"answers":{"3":"yes"},"upto":1}', CLOSED],
        ['not JSON', '{oops', CLOSED],
        ['an unknown answer', '{"answers":{"1":"maybe"},"upto":1}', CLOSED],
        ['a number of parts out of range', '{"answers":{},"upto":99}', Object.assign({}, OPEN_ALL, { lines: [[], [], []] })],
        ['answers without the number of parts', '{"answers":{"1":"no","2":"yes"}}', { parts: [1, 2, 3], forks: [1, 2, 3], hints: [3], lines: [['no'], ['yes'], []], about: false, next: false }],
      ];
      for (const item of cases) {
        await tab.evaluate((pair) => localStorage.setItem(pair[0], pair[1]), ['pr-fork:' + scope, item[1]]);
        await tab.reload({ waitUntil: 'load' });
        expectView(tag + '11 stored ' + item[0], await look(tab), item[2]);
      }
      await fresh.close();

      // No localStorage at all: the place is kept for the life of the page.
      const sealed = await newContext(browser, { viewport: viewport });
      await sealed.addInitScript(() => {
        Object.defineProperty(window, 'localStorage', { get: function () { throw new DOMException('denied', 'SecurityError'); } });
      });
      const dark = await sealed.newPage();
      const darkErrors = [];
      watch(dark, darkErrors);
      await open(dark, TEXT);
      expectView(tag + '11 no storage, first open', await look(dark), { parts: [1], forks: [1], hints: [1], buttons: [1] });
      await answer(dark, 1, 'not_sure');
      expectView(tag + '11 no storage, after answer 1', await look(dark), { parts: [1, 2], forks: [1, 2], lines: [['not_sure'], [], []] });
      await dark.reload({ waitUntil: 'load' });
      expectView(tag + '11 no storage, after reload', await look(dark), { parts: [1], forks: [1] });
      check(tag + '11 no storage: no errors on the page', darkErrors.length === 0, darkErrors);
      await sealed.close();

      // A narrow phone: nothing runs off the side, closed or open.
      const narrow = await newContext(browser, { viewport: { width: 320, height: 640 } });
      const small = await narrow.newPage();
      watch(small, errors);
      await open(small, TEXT);
      const first = await look(small);
      await open(small, TEXT + '#part-4');
      const last = await look(small);
      check(tag + '11 at 320 px no sideways scroll, closed and open', first.overflow <= 0 && last.overflow <= 0, [first.overflow, last.overflow]);
      await narrow.close();

      if (args.login) {
        const pair = String(args.login).split(':');
        const signed = await newContext(browser, { viewport: viewport });
        const member = await signed.newPage();
        watch(member, errors);
        await member.goto(BASE + '/accounts/login/');
        await member.fill('input[name="login"]', pair[0]);
        await member.fill('input[name="password"]', pair.slice(1).join(':'));
        await Promise.all([member.waitForNavigation(), member.press('input[name="password"]', 'Enter')]);
        await open(member, TEXT);
        const view = await look(member);
        expectView(tag + '11 a signed-in reader, first open', view, Object.assign({}, CLOSED, { tools: true }));
        await signed.close();
      } else {
        skip(tag + '11 a signed-in reader: no --login');
      }
    });

    // A page that hears nothing of what another page writes: the browser sends
    // no "storage" event to a page it has put aside, and may be late with it.
    const deaf = async (context) => {
      const tab = await context.newPage();
      await tab.addInitScript(() => { window.addEventListener('storage', (event) => event.stopImmediatePropagation(), true); });
      watch(tab, errors);
      return tab;
    };
    const readToTheEnd = async (tab) => {
      await answer(tab, 1, 'yes');
      await answer(tab, 2, 'no');
      await answer(tab, 3, 'not_sure');
    };
    const READ = { answers: { 1: 'yes', 2: 'no', 3: 'not_sure' }, upto: 4 };
    const READ_VIEW = Object.assign({}, OPEN_ALL, { lines: [['yes'], ['no'], ['not_sure']], pressed: [['yes'], ['no'], ['not_sure']] });
    const CHANGED_VIEW = Object.assign({}, OPEN_ALL, { lines: [['no'], ['no'], ['not_sure']], pressed: [['no'], ['no'], ['not_sure']] });
    const followed = (tab, parts) => tab.waitForFunction((n) => document.querySelectorAll('.part.is-reached').length === n, parts).catch(() => null);

    await step(tag, 12, '12: the same text in two tabs', async () => {
      // Two tabs side by side: the first follows what is done in the second.
      const shared = await newContext(browser, { viewport: viewport });
      const one = await shared.newPage();
      const two = await shared.newPage();
      watch(one, errors);
      watch(two, errors);
      await open(one, TEXT);
      await open(two, TEXT);
      const before = await look(one);
      await readToTheEnd(two);
      await followed(one, 4);
      const after = await look(one);
      expectView(tag + '12 the first tab follows the second', after, READ_VIEW);
      check(tag + '12 following moves neither the page nor the focus', after.scrollY === before.scrollY && after.focus === before.focus, [before.scrollY, after.scrollY, before.focus, after.focus]);
      check(tag + '12 following writes nothing', same(kept(after), READ), after.storage);
      await answer(one, 1, 'no');
      await two.waitForFunction(() => !!document.querySelector('#fork-1-no-line.is-chosen')).catch(() => null);
      expectView(tag + '12 an answer changed in the first tab shows in the second', await look(two), CHANGED_VIEW);
      await shared.close();

      // The first tab has missed it all and is still closed. An answer there
      // must not throw away what the second tab has kept.
      const apart = await newContext(browser, { viewport: viewport });
      const stale = await deaf(apart);
      const other = await apart.newPage();
      watch(other, errors);
      await open(stale, TEXT);
      await open(other, TEXT);
      await readToTheEnd(other);
      expectView(tag + '12 a tab that missed the news is still closed', await look(stale), { parts: [1], forks: [1] });
      await answer(stale, 1, 'no');
      const merged = await look(stale);
      expectView(tag + '12 an answer in that tab: it catches up, nothing closes', merged, CHANGED_VIEW);
      check(tag + '12 the kept place: the new answer on top of the kept ones', same(kept(merged), { answers: { 1: 'no', 2: 'no', 3: 'not_sure' }, upto: 4 }), merged.storage);
      await other.reload({ waitUntil: 'load' });
      expectView(tag + '12 the second tab after a reload', await look(other), CHANGED_VIEW);
      await apart.close();

      // The same with a link to a part instead of an answer.
      const linked = await newContext(browser, { viewport: viewport });
      const behind = await deaf(linked);
      const ahead = await linked.newPage();
      watch(ahead, errors);
      await open(behind, TEXT);
      await open(ahead, TEXT);
      await readToTheEnd(ahead);
      await behind.evaluate(() => { window.location.hash = '#part-2'; });
      await settledTop(behind, '#part-2');
      const jumped = await look(behind);
      expectView(tag + '12 a link to #part-2 in a tab that missed the news: it catches up', jumped, READ_VIEW);
      check(tag + '12 the link took nothing from the kept place', same(kept(jumped), READ), jumped.storage);
      await linked.close();

      // What is kept can be behind this page (a write that failed, a place
      // put there by hand). The page takes the kept answer and closes nothing.
      // It was opened by a link, so no answer of its own holds its parts open.
      const ahead2 = await newContext(browser, { viewport: viewport });
      const read = await ahead2.newPage();
      const elsewhere = await ahead2.newPage();
      watch(read, errors);
      await open(read, TEXT + '#part-4');
      await elsewhere.goto(BASE + '/library/', { waitUntil: 'load' });
      await elsewhere.evaluate((name) => localStorage.setItem(name, '{"answers":{"1":"no"},"upto":2}'), 'pr-fork:' + (await look(read)).scope);
      await read.waitForFunction(() => !!document.querySelector('#fork-1-no-line.is-chosen')).catch(() => null);
      expectView(tag + '12 a kept place that is behind closes nothing here', await look(read), Object.assign({}, OPEN_ALL, { lines: [['no'], [], []], pressed: [['no'], [], []] }));
      await answer(read, 2, 'yes');
      const whole = await look(read);
      check(tag + '12 the next answer keeps the whole place again', same(kept(whole), { answers: { 1: 'no', 2: 'yes' }, upto: 4 }), whole.storage);
      await ahead2.close();
    });

    await step(tag, 13, '13: a page the browser kept for the "back" button', async () => {
      // The event the browser sends when it shows a kept page again.
      const apart = await newContext(browser, { viewport: viewport });
      const stale = await deaf(apart);
      const other = await apart.newPage();
      watch(other, errors);
      await open(stale, TEXT);
      await open(other, TEXT);
      await readToTheEnd(other);
      await stale.evaluate(() => window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: false })));
      expectView(tag + '13 before it is shown again the page is behind', await look(stale), { parts: [1], forks: [1] });
      await stale.evaluate(() => window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true })));
      const shown = await look(stale);
      expectView(tag + '13 shown again (pageshow, persisted): it catches up', shown, READ_VIEW);
      check(tag + '13 catching up writes nothing', same(kept(shown), READ), shown.storage);
      await apart.close();

      // The real thing: closed text -> Library -> the same text read to the
      // end -> "back" twice. Playwright starts Chromium with this cache off
      // (a kept page fires no "load"), so this part has a browser of its own.
      const keeping = await loadPlaywright().chromium.launch({ ignoreDefaultArgs: ['--disable-back-forward-cache'] });
      try {
        const context = await newContext(keeping, { viewport: viewport });
        const tab = await context.newPage();
        watch(tab, errors);
        await tab.addInitScript(() => { window.addEventListener('pageshow', (event) => { window.shownFromCache = event.persisted; }); });
        await open(tab, TEXT);
        expectView(tag + '13 (real) the text is closed', await look(tab), { parts: [1], forks: [1] });
        await tab.locator('.site-footer a[href="/library/"]').click();
        await tab.waitForURL('**/library/');
        await open(tab, TEXT);
        await readToTheEnd(tab);
        for (const address of ['**/library/', '**' + TEXT]) {
          await tab.goBack({ waitUntil: 'commit' });
          await tab.waitForURL(address, { waitUntil: 'commit' });
        }
        await tab.waitForFunction(() => typeof window.shownFromCache === 'boolean');
        if (await tab.evaluate(() => window.shownFromCache)) {
          const back = await look(tab);
          expectView(tag + '13 (real) the kept page, shown again, has caught up', back, READ_VIEW);
          await answer(tab, 1, 'no');
          check(tag + '13 (real) an answer on the kept page keeps the rest', same(kept(await look(tab)), { answers: { 1: 'no', 2: 'no', 3: 'not_sure' }, upto: 4 }), (await look(tab)).storage);
        } else {
          skip(tag + '13 (real): this browser loaded the page again instead of keeping it');
        }
      } finally {
        await keeping.close();
      }
    });

    await step(tag, 14, '14: the script comes late, after the page was given back to the reader', async () => {
      const slow = await newContext(browser, { viewport: viewport });
      const tab = await slow.newPage();
      watch(tab, errors);
      let arrived;
      const delivered = new Promise((resolve) => { arrived = resolve; });
      await tab.route('**/reader*.js', async (route) => {
        await new Promise((resolve) => setTimeout(resolve, LATE));
        await route.continue();
      });
      tab.on('requestfinished', (request) => { if (/\/reader[^/]*\.js/.test(request.url())) { arrived(); } });
      await tab.goto(BASE + TEXT, { waitUntil: 'commit' });
      await tab.waitForSelector('.site-footer', { state: 'attached' });
      expectView(tag + '14 at first, no script yet', await look(tab), { parts: [1], forks: [1], buttons: [], hints: [], about: false, next: false });
      // No script after three seconds: the inline script gives the whole text.
      await tab.waitForFunction(() => !document.documentElement.classList.contains('js'), null, { timeout: 3 * TIMEOUT });
      const WHOLE = { parts: [1, 2, 3, 4], forks: [1, 2, 3], hints: [], buttons: [], readOn: [1, 2, 3], lines: [KEYS, KEYS, KEYS], labels: 9, words: true, about: true, next: true };
      expectView(tag + '14 after three seconds: the whole text', await look(tab), WHOLE);
      // The reader goes on reading, down to the end of the text.
      await tab.evaluate(() => {
        document.documentElement.style.scrollBehavior = 'auto';
        document.querySelector('.end-block.about').scrollIntoView();
      });
      const place = await tab.evaluate(() => ({ scrollY: Math.round(window.scrollY), height: document.documentElement.scrollHeight }));
      await delivered;
      await tab.waitForLoadState('load');
      await tab.waitForTimeout(300);
      const late = await look(tab);
      expectView(tag + '14 the script has come: nothing closes', late, WHOLE);
      const now = await tab.evaluate(() => ({ scrollY: Math.round(window.scrollY), height: document.documentElement.scrollHeight, js: document.documentElement.classList.contains('js'), ready: document.documentElement.hasAttribute('data-js-ready') }));
      check(tag + '14 the reader is where they were', now.scrollY === place.scrollY && now.height === place.height && place.scrollY > 0, [place, now]);
      check(tag + '14 the page stays plain until the next load', !now.js && !now.ready, now);
      check(tag + '14 the late script kept nothing', Object.keys(late.storage).length === 0, late.storage);
      await tab.unroute('**/reader*.js');
      await tab.reload({ waitUntil: 'load' });
      expectView(tag + '14 the next load, the script in time: closed as usual', await look(tab), CLOSED);
      await slow.close();
    });

    await step(tag, 15, '15: the script fails half-way', async () => {
      const cases = [['at its last step, all controls already shown', '[data-topic-examples]'], ['at the fork', '[data-fork-scope]']];
      for (const item of cases) {
        const broken = await newContext(browser, { viewport: viewport });
        const tab = await broken.newPage();
        watch(tab, errors);
        // The script asks the page for this once; that one call fails.
        await tab.addInitScript((selector) => {
          const original = Document.prototype.querySelector;
          Document.prototype.querySelector = function (asked) {
            if (asked === selector) {
              Document.prototype.querySelector = original;
              window.plantedFailure = true;
              throw new Error('planted failure');
            }
            return original.apply(this, arguments);
          };
        }, item[1]);
        await open(tab, TEXT);
        const view = await look(tab);
        const page = await tab.evaluate(() => ({
          failed: window.plantedFailure === true,
          js: document.documentElement.classList.contains('js'),
          ready: document.documentElement.hasAttribute('data-js-ready'),
          toggle: document.querySelector('[data-ru-toggle]').getClientRects().length,
          sentenceButtons: document.querySelectorAll('.s[role="button"], .s[tabindex]').length,
        }));
        check(tag + '15 ' + item[0] + ': the failure happened', page.failed, page);
        expectView(tag + '15 ' + item[0] + ': the whole text', view, { parts: [1, 2, 3, 4], forks: [1, 2, 3], hints: [], buttons: [], readOn: [1, 2, 3], lines: [KEYS, KEYS, KEYS], labels: 9, words: true, about: true, next: true });
        check(tag + '15 ' + item[0] + ': a plain page, no control that needs the script', !page.js && !page.ready && page.toggle === 0 && page.sentenceButtons === 0, page);
        await broken.close();
      }
    });

    await context.close();
  }

  if (wanted(6) && !(args.republish && args.revised)) {
    console.log('');
    skip('step 6b (a really published new revision): no --revised and --republish');
  }
  if (args.republish && args.revised) await step('', 6, '6b: a new revision published while the text is read to the end (' + args.revised + ')', async () => {
    const tabs = [];
    for (const viewport of VIEWPORTS) {
      const context = await newContext(browser, { viewport: viewport });
      const page = await context.newPage();
      watch(page, errors);
      await open(page, args.revised);
      await answer(page, 1, 'yes');
      await answer(page, 2, 'yes');
      await answer(page, 3, 'yes');
      const view = await look(page);
      expectView('[' + viewport.width + '] 6b before', view, OPEN_ALL);
      tabs.push({ context: context, page: page, width: viewport.width, before: view });
    }
    console.log('  run   ' + args.republish);
    execSync(args.republish, { stdio: ['ignore', 'ignore', 'inherit'] });
    for (const tab of tabs) {
      const tag = '[' + tab.width + '] ';
      await tab.page.reload({ waitUntil: 'load' });
      const view = await look(tab.page);
      const slugOf = (scope) => scope.slice(0, scope.lastIndexOf(':'));
      check(tag + '6b: the same address, another revision', slugOf(view.scope) === slugOf(tab.before.scope) && view.scope !== tab.before.scope, [tab.before.scope, view.scope]);
      expectView(tag + '6b after', view, CLOSED);
      check(tag + '6b: the place of the old revision is removed', Object.keys(view.storage).filter((key) => key.indexOf('pr-fork') === 0).length === 0, view.storage);
      await shot(tab.page, tab.width + '-06-new-revision-first-open');
      await tab.context.close();
    }
  });

  await step('', 8, '8: the hint on each tone', async () => {
    const tones = args.tones
      ? String(args.tones).split(',').map((pair) => ({ tone: pair.split(':')[0], url: pair.split(':').slice(1).join(':') }))
      : [{ tone: null, url: TEXT }, { tone: null, url: OTHER }];
    for (const viewport of VIEWPORTS) {
      const context = await newContext(browser, { viewport: viewport, deviceScaleFactor: 2 });
      const page = await context.newPage();
      watch(page, errors);
      for (const item of tones) {
        await open(page, item.url);
        const tone = await page.evaluate(() => document.body.getAttribute('data-cover'));
        const tag = '[' + viewport.width + '] 8 ' + tone;
        if (item.tone) { check(tag + ': the text has the tone asked for', tone === item.tone, tone); }
        const view = await look(page);
        check(tag + ': the hint is shown at the first open', same(view.hints, [1]), view.hints);
        for (const row of await hintContrast(page)) {
          contrastRows.push(Object.assign({ width: viewport.width, tone: tone }, row));
          check(tag + ' ' + row.line + ': contrast ' + row.ratio.toFixed(2) + ' >= 4.5 (' + row.text + ' on ' + row.background + ')', row.ratio >= 4.5 && row.background === TONES[tone] && row.images === 0 && row.opacity === 1, row);
        }
        if (SHOTS) {
          const box = await page.evaluate(() => {
            const fork = document.querySelector('[data-fork]');
            const a = fork.querySelector('[data-fork-buttons]').getBoundingClientRect();
            const b = fork.querySelector('[data-fork-hint]').getBoundingClientRect();
            const pad = 20;
            const left = Math.max(0, Math.min(a.left, b.left) - pad);
            const right = Math.min(document.documentElement.clientWidth, Math.max(a.right, b.right) + pad);
            const top = a.top + window.scrollY - pad;
            return { x: left, y: top, width: right - left, height: b.bottom + window.scrollY + pad - top };
          });
          await shot(page, viewport.width + '-08-hint-' + tone, { clip: box });
        }
      }
      await context.close();
    }
    console.log('\n  hint contrast (text and background as painted)');
    console.log('  width  tone   line  text     background  ratio  size     weight');
    for (const row of contrastRows) {
      console.log('  ' + [String(row.width).padEnd(5), row.tone.padEnd(5), row.line.padEnd(4), row.text, row.background.padEnd(10), row.ratio.toFixed(2).padEnd(5), row.size.padEnd(7), row.weight].join('  '));
    }
  });

  await browser.close();
  check('no errors in the console of any page', errors.length === 0, errors);
  console.log('\nfork_steps: ' + passed + ' passed, ' + failed + ' failed' + (skipped ? ', ' + skipped + ' skipped' : ''));
  process.exit(failed ? 1 : 0);
}

main().catch((error) => { console.error(error); process.exit(1); });
