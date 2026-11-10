# AGENTS.md — Philosophy Reader implementation

This repository implements the MVP described in docs/spec/ (SPEC.md,
CHANGELOG.md and docs/). Those documents are the source of truth.

## Non-negotiable rules

- Django monolith, PostgreSQL + pgvector, server templates, small JS.
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

## Commands

See README.md for the verified command list (migrate, seed_demo,
run_ai_worker, run_maintenance, etc.).

## Reporting

After each stage record in docs/IMPLEMENTATION_LOG.md: what works,
which checks ran, known limitations and the next stage.
