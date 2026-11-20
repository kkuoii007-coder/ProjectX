# AGENTS.md — Philosophy Reader implementation

This repository implements the MVP described in docs/spec/ (SPEC.md,
CHANGELOG.md and docs/). Those documents are the source of truth.

## Non-negotiable rules

- Django monolith, PostgreSQL + pgvector, server templates, small JS.
  Frontend stack (revised for the redesign, see "Frontend" below): hand-written
  CSS on design tokens + vanilla JS + two self-hosted OFL fonts. No Node, no
  bundler, no React/Tailwind/CDN, no build step on the VPS. Any further stack
  expansion needs separate owner approval.
- Custom User (no email/first_name/last_name) exists since the first migration.
- Credits are integers; money is Decimal. Never float for money.
- Every user-owned object is filtered by owner on each request.
- State changes use POST + CSRF only; GET never mutates.
- No network calls inside DB transactions; no AI calls in reader HTTP path.
- Secrets never enter Git. On the server .env holds ONLY OPENROUTER_API_KEY;
  other secrets live in /etc/philosophy-reader/service.env (systemd).
- Ready-made texts enter the library only through `submit_text` ->
  `review_text` -> `publish_text` (the `publish_revision` gate). Never create
  `Publication` or `QualityReport` by hand and never loosen the gate. The text
  itself is stored unchanged; nothing may state that a human checked it.
- The home page shows the reading named in `START_READING_SLUG`, never
  "the latest published".
- FakeProvider is allowed only when AI_PROVIDER=fake in dev/test.
  Production failures must surface as errors, never as fake content.
- Welcome grant (30 credits default) is issued exactly once, atomically
  with registration; Unique(user, campaign) enforces it.
- Hermes on this VPS must not be touched: container, image, /var/lib/hermes*,
  /opt/hermes-deploy and its network are off-limits.

## Layout

config/ settings (base/dev/test/prod), urls, wsgi, asgi
apps/   accounts, library, sources, workshop, ai, credits, operations
templates/, static/, tests/, content/, docs/spec/, deploy/

## Frontend

Direction "A question on the cover": every page opens with a field in the
cover tone that carries the header and a large title; the content follows in
ink on paper. A translation opens under its own sentence, at any width.

- `static/css/main.css` — all styles; colours, type, spacing and radii are CSS
  custom properties at the top. The cover palette is closed: three tones,
  `--cover-clay` #8a3a26, `--cover-sea` #1f4d5c, `--cover-olive` #4a5626,
  chosen with `data-cover`; text on a cover is `--on-cover` (paper). Each
  published question has one tone (`Publication.cover_tone`); account, sign-in,
  workshop and help pages use clay. There is no other accent and no dark band.
  Do not add ad-hoc colours; keep text contrast >= WCAG AA (4.5:1) on every
  tone. The fork mark is drawn in code; no portraits or images.
- Language. Reading path (home, library, thinker, collection, reader): a plain
  English line with a small Russian caption under it (`.ru-ui`, `lang="ru"`).
  The `RU` switch in the header hides these captions (`localStorage`
  `pr-ui-ru`), never the translations of sentences. Account, sign-in,
  registration and workshop pages stay Russian.
- The first screen of the home page holds only the question, two sentences of
  the text, `Start reading` and the small line: no registration, credits,
  generator or search. One text appears on the home page once.
- `static/css/fonts.css` + `static/fonts/` — Literata (text, headings) and
  Golos Text (UI, translations), self-hosted woff2 with their OFL texts.
  No external font/CDN requests.
- `static/js/reader.js` — the only JS file of the site (~330 lines, no dependencies), plus
  one inline line in `<head>` that sets the `js` and `ui-ru-off` classes before
  the first paint and removes them if the file fails to load. Pages must
  stay usable without JS: translations visible, menu expanded.
- `templates/base.html` (Russian service pages) and `templates/base_reading.html`
  (reading path: `lang="en"`, header with the name and the `RU` switch only);
  page templates in `templates/<app>/`. One sentence per line. Reader markup:
  `span.s[data-gloss]` + `span.gloss[role=note][lang=ru]`. Working JS adds
  `[role=button][tabindex=0][aria-expanded][aria-controls]` to each sentence;
  without JS these remain ordinary prose with visible translations, not dead
  keyboard controls. This markup is used on the reader and on
  `workshop/project.html`; the old `.sentence/.translation` markup is gone.
- View-only context builders live in `apps/library/presenters.py`; template
  helpers in `apps/library/templatetags/ui.py`.
- UI must not offer actions that do not exist (no dead buttons, no invented
  reviews or metrics). Honour `prefers-reduced-motion`.
- Frontend checks: layout at 375/768/1440, no horizontal overflow, 320px,
  keyboard operation, empty console, contrast. The dev preview is loopback only.
- Redesign v1 keeps the same stack and content. Shared account navigation and
  publication entries live in `templates/includes/`. Do not split Django tags
  or variable expressions across newlines. All POST forms must render a real
  CSRF input, including forms inside `details`.
- Test 390/768/1440 plus 320px, script failure and no-JS. On 390x844 the first
  English sentence of a text is visible without scrolling. Keep
  English/Russian learning payloads unchanged.
- A text with forks (`data-fork`) opens as a chain: first only part 1 and its
  question; each answer shows its prepared line, the next part and the next
  question; `About this text` and `Next question` come with the last part,
  `Words` and the reader's tools are always there. `reader.js` draws the whole
  page from one state in one `render()`; do not open or close single blocks
  elsewhere. The state lives on the device only, under `pr-fork:<slug>:<revision
  hash>` (`data-fork-scope`): another text and a new revision start closed.
  What is open never closes; an answer can be changed; `#part-N` opens the
  text up to part N and answers nothing. A fork is not a form: nothing is sent
  or scored. Closing is CSS under `.js` inside `@media screen` only, so
  without JS, on a script failure and in print the whole text is there with
  the lines of all answers. Browser check: `tools/qa/fork_steps.js`
  (Playwright, an acceptance tool, not in the test suite).
- Tables use `.table-scroll` with a focusable labelled region. Keep auth,
  registration, error, empty and workshop states at the same quality as home.
- Use isolated PostgreSQL + pgvector and `AI_PROVIDER=fake`,
  `LIVE_AI_ENABLED=false` for local QA. Never copy production `.env`/data.
  Python lock installation and `collectstatic` are the build; no Node required.

## Commands

See README.md for the verified command list (migrate, seed_demo,
run_ai_worker, run_maintenance, etc.).

## Reporting

After each stage record in docs/IMPLEMENTATION_LOG.md: what works,
which checks ran, known limitations and the next stage.
