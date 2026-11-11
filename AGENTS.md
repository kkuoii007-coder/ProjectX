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

Direction "Gloss in the margin": the translation is a reader's note in the
margin (right margin at >=82rem, under the sentence on narrow screens).

- `static/css/main.css` — all styles; colours, type, spacing and radii are CSS
  custom properties at the top. One accent only (`--rubric`); the dark band is
  reserved for the AI workshop. Do not add ad-hoc colours; keep text contrast
  >= WCAG AA (4.5:1).
- `static/css/fonts.css` + `static/fonts/` — Literata (text, headings) and
  Golos Text (UI, translations), self-hosted woff2 with their OFL texts.
  No external font/CDN requests.
- `static/js/reader.js` — the only JS (~170 lines, no dependencies). Pages must
  stay usable without JS: translations visible, menu expanded.
- `templates/base.html`; page templates in `templates/<app>/`. Reader markup:
  `span.s[role=button][tabindex=0][aria-expanded][aria-controls]` +
  `span.gloss[role=note][lang=ru]`. This markup is used on the reader and on
  `workshop/project.html`; the old `.sentence/.translation` markup is gone.
- View-only context builders live in `apps/library/presenters.py`; template
  helpers in `apps/library/templatetags/ui.py`.
- UI must not offer actions that do not exist (no dead buttons, no invented
  reviews or metrics). Honour `prefers-reduced-motion`.
- Frontend checks: layout at 375/768/1440, no horizontal overflow, 320px,
  keyboard operation, empty console, contrast. The dev preview is loopback only.

## Commands

See README.md for the verified command list (migrate, seed_demo,
run_ai_worker, run_maintenance, etc.).

## Reporting

After each stage record in docs/IMPLEMENTATION_LOG.md: what works,
which checks ran, known limitations and the next stage.
