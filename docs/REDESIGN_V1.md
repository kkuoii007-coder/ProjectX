# ProjectX: «Журнал для медленного чтения»

## Direction and scope

One concept: an adult intellectual journal outside, a quiet annotated book inside.
The existing “Gloss in the margin” identity is refined, not replaced with a SaaS
landing page. A typographic masthead, editorial rules, clear chapter numbers and
restrained rubric ink establish hierarchy without decorative imagery or invented
content. Literata and Golos Text remain self-hosted under their existing OFL
licenses. UI remains Russian; learning text remains English with Russian glosses.

The main action is reading. The actual featured sentence is the home page's
visual specimen, not a made-up quotation or attributed portrait. The workshop
keeps its dark surface as a distinct workspace. All catalogue, account and
authentication pages share the same spacing, navigation and form vocabulary.

Implementation is in the existing Django templates, CSS and small vanilla JS.
No frontend framework, new build system, analytics, external fonts, runtime
dependency, schema migration or deployment change was introduced.

## Baseline and boundaries

- Base and intended PR target: `main`.
- Original SHA: `247b81ba1932aa51caf002cb762c0b0b0d14cb0b`.
- Working branch: `design/redesign-v1`.
- `redesign/frontend` and server-local `design/preview` are not moved.
- No access to production credentials, database, services or SSH was used.
- The provided production URL could not be opened from this sandbox.
- GitHub inspection found zero workflows, zero repository webhooks and no
  rulesets; the owner's server diagnostic says deployment is manual.
- Push/PR is permitted; merge and production deployment are not authorized.

## Verification

See `FLOWS.md` for the acceptance matrix. Evidence was captured on November 13,
2026 using Chromium against the real local Django application, with a separate
PostgreSQL database. The screenshots are not a static prototype.

### Implemented

- Home: editorial masthead, asymmetric reading specimen, compact featured
  publication/contents, genuine collection/thinker links, separate dark workshop.
- Reader: calmer text measure and rhythm, complete chapter names on narrow
  screens, keyboard translation controls, responsive margin/inline notes.
- Library, collection, thinker and favorites: shared publication entry, genuine
  metadata and clear empty/search-reset states, no invented filters or articles.
- Login/registration, account, credits, words, security and workshop: consistent
  headings, navigation, forms and focus states; focusable scrolling tables.
- Workshop: native disclosure for existing edit forms, repaired template tags,
  rendered Russian translations and working sentence controls.
- Progressive enhancement: controls become buttons only after their handlers
  are attached. No JS or a failed script leaves prose, translations and menu
  available. Multiple open translations move inline to prevent margin drift.
- Small compatibility fixes: correct `account:` redirects, reject external
  favorite return URLs, password-manager autocomplete hints. No business
  services, routes, AI provider, settings, models or migrations changed.

### Runtime and dependencies

All exact pins from `requirements.lock` installed without changes. Runtime:
Python 3.12.15, Django 5.2.19, psycopg 3.2.13, Python pgvector 0.4.2.
The sandbox OS provides PostgreSQL 18.6 and database vector extension 0.8.1,
not production's PostgreSQL 16. This version difference remains a server-side
verification requirement.

No new application dependency. Playwright and axe-core were external QA tools,
not added to the project, loaded from the product or required on the VPS.
There is no Node build and no CDN request. Existing self-hosted OFL fonts remain.

### Checks actually executed

- Untouched base checkout: **102 Django tests passed**.
- Final code: **132 Django tests passed** (30 added regression tests).
- `manage.py check`: no issues.
- `manage.py makemigrations --check --dry-run`: no changes.
- `manage.py collectstatic --noinput --settings=config.settings.prod`:
  successful hashed static manifest; final incremental run copied 1 asset,
  retained 136 unchanged files and post-processed 103.
- `git diff --check`: clean.
- **15 actual pages × 390/768/1440 px = 45 screenshots**; all returned HTTP 200
  with no document horizontal overflow. Tables scroll locally.
- axe-core WCAG A/AA automated scan: **zero violations** on those 15 pages at
  390 px, and on the workshop with a translation and edit disclosure open.
  This is not a WCAG certification or a substitute for assistive-technology QA.
- Additional 320 px reflow on six representative pages; synthetic DOM-only
  long-title/unbroken-token/long-translation stress on six pages at four widths
  (24 cases); 200% root-text scaling on all 15 pages at 768 px: no page overflow.
  Synthetic strings were not saved to the database or learning content.
- Keyboard: skip link, menu/Escape/focus return, Tab/Enter/Space/Escape
  translations, chapter links, scrolling tables; selection guard and
  reduced-motion styles; desktop/mobile/desktop resizing.
- Browser: catalog search/no-results/reset; specimen translation; real
  publication, thinker and collection navigation; all/individual/mixed gloss
  states; no-JS and blocked-script fallback.
- Browser forms: invalid/valid login; invalid/valid invitation registration;
  favorite add/remove from reader and favorites; save word; report text issue;
  password error/change; logout-all; JSON export;
  wrong-password deletion and deletion of a disposable account with no projects.
- Workshop through real HTTP + CSRF: generate, edit sentence, compare JSON,
  apply revision, edit part, ask. The local worker ran only explicitly with
  FakeProvider. Generation and sentence edit produced stored revisions; the
  fake part-edit test returned its existing no-charge explanation rather than
  a fabricated edited result. The fake answer appeared in discussion.
- Final navigation pass: no console errors, uncaught JS exceptions, failed
  requests or external requests.

### Visual QA correction

The first mobile favorites capture exposed a specificity issue: the remove
form kept its desktop grid column and squeezed the title. It was corrected,
recaptured at all three widths and included in the unbroken-title stress pass.
Screenshots were inspected, not merely generated.

### Evidence

Representative before/after images are in `docs/redesign-v1/`.
The delivery also includes the complete screenshot archive and machine-readable
browser results. Before images use the original frontend and the same seeded
Socrates publication; after images use the integrated redesign. Workshop
images visibly identify FakeProvider and are not claims about a live AI model.

## Run and build

Use an isolated database with the `vector` extension installed before migrations.
The existing first migration creates a vector column before the second
migration enables the extension. A local test cluster also needs the extension
in the template used to create Django's test database, or a pre-provisioned
test database. Never change a production template database just for these tests.

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.lock

# Supply local-only DJANGO_SECRET_KEY and DB_NAME/DB_USER/DB_PASSWORD.
# DB_HOST must be loopback and the DB must not be production.
export DJANGO_SETTINGS_MODULE=config.settings.dev
export AI_PROVIDER=fake LIVE_AI_ENABLED=false OPENROUTER_API_KEY=
export REGISTRATION_MODE=invite

.venv/bin/python manage.py migrate --noinput
.venv/bin/python manage.py seed_demo
.venv/bin/python manage.py check
.venv/bin/python manage.py runserver 127.0.0.1:8813

# A separate terminal, with the same sandbox-only environment:
.venv/bin/python manage.py test --settings=config.settings.test --noinput
.venv/bin/python manage.py makemigrations --check --dry-run
.venv/bin/python manage.py collectstatic --noinput --settings=config.settings.prod
```

Do not use `seed_demo` in production. Do not start a worker with a live provider
for this verification. A disposable local user can be created through the
normal `create_user` and `grant_welcome_credits` functions; the seeded editorial
account is deliberately inactive and has no login password.

## Isolated VPS handoff

The complete app is running locally in the designer's sandbox, not at a public
preview URL. The supplied VPS preview is loopback-only and no SSH credential
or tunnel is available in this session. A static replacement is not offered
as an integrated preview. Keep Django CSRF, session and frame protections intact.

For the server agent, use only the existing `/opt/pr-design` clone and `pr_design`
database from the owner's diagnostic. The owner receives the exact final SHA
in the delivery report. Start on a new loopback port (8813) so the existing
8812 preview and production services are not stopped.

```bash
cd /opt/pr-design
test -z "$(git status --porcelain)" || { echo "STOP: local changes"; exit 1; }
git fetch origin design/redesign-v1
git rev-parse origin/design/redesign-v1
# Compare to the owner's delivered final SHA. STOP on mismatch.
git switch --detach <FINAL_SHA_FROM_DELIVERY>
```

Run the following as `prdr`, not root, with a clean environment; the preview's
existing `.env` supplies only its local DB credentials. Inspect assertions
without printing secrets. No production environment file is sourced.

```bash
cd /opt/pr-design
PY=/opt/pr-design/.venv/bin/python
test -x "$PY" || PY=/opt/pr-design/venv/bin/python
test -x "$PY" || { echo "STOP: locate the isolated preview venv"; exit 1; }
# The diagnostic did not specify which of these venv directory names it uses.
# Do not fall back to /opt/philosophy-reader or system Python.
preview() {
  env -i HOME="$HOME" PATH=/usr/bin:/bin \
    DJANGO_SETTINGS_MODULE=config.settings.dev \
    AI_PROVIDER=fake LIVE_AI_ENABLED=false OPENROUTER_API_KEY= \
    REGISTRATION_MODE=invite \
    "$PY" manage.py "$@"
}
preview shell -c \
  'import sys; from pathlib import Path; from django.conf import settings as s; assert Path(sys.prefix).resolve().is_relative_to(Path("/opt/pr-design")); d=s.DATABASES["default"]; assert d["NAME"]=="pr_design"; assert d["USER"]=="pr_design"; assert d["HOST"] in ("127.0.0.1","localhost"); assert s.AI_PROVIDER=="fake" and not s.LIVE_AI_ENABLED; assert not s.OPENROUTER_API_KEY; print("isolated preview configuration OK")' \
  || { echo "STOP: isolation check failed"; exit 1; }
preview check || exit 1
preview runserver 127.0.0.1:8813 --noreload
```

Then on the owner's machine:

```bash
ssh -L 8813:127.0.0.1:8813 <user>@185.112.100.238
# Open http://127.0.0.1:8813/
```

Run checks and the PostgreSQL-16 browser smoke before considering deployment.
Do not run the full Django test command under the production DB role: it needs
its own disposable test DB and appropriate local permissions. Use a demo user
in `pr_design`; invite mode remains enabled. There are no new migrations.

## Publication and rollback

This PR is a reviewable code change, **not a production release approval**.
Before push/PR the branch, workflows, repository hooks and rulesets were checked.
No workflow or repository webhook was configured; the owner's diagnostic states
manual production deployment. Unknown external automation cannot be disproved
from repository files alone. No deployment file was changed.

Only the owner may merge or deploy. A later approved release needs the existing
dependency lock, `check`, hashed `collectstatic`, and an owner-controlled web
process reload so cached templates refresh. No database migration or AI-worker
configuration change is required by this redesign. Do not touch Hermes.

Rollback before merge: close the PR; main/production were not changed.
Rollback of the isolated preview: stop only its 8813 process and switch the
clean `/opt/pr-design` checkout to `247b81ba1932aa51caf002cb762c0b0b0d14cb0b`
or the unchanged `design/preview` branch.
For a later production rollback, the owner should revert the merge through a new
commit/PR (no force-push), restore the prior frontend/static manifest with
`collectstatic`, and reload only the existing web service. No data rollback is
introduced by this change.

## Limits and prior defects

- Production was not reachable from this sandbox; it was neither changed nor
  verified in the browser. PostgreSQL 16, real TLS/nginx/static cache behavior,
  Safari/Firefox, real mobile devices and screen readers remain unverified.
- No hosted full-app preview was published. No production database, secrets,
  payments, email or paid API was used. Actual 200% browser UI zoom was not
  certified; root-text scaling/reflow was checked separately.
- No CEFR audit or rewriting of the existing learning texts was performed.
  A0–A1 is the product audience, not an invented certification of seeded text.
- Closed-registration, owner-isolation and many server error variants are
  covered by Django tests, not all by browser screenshots.
- Existing backend risks found by code inspection, **not introduced or repaired
  here**: `purge_service.py` can encounter protected QualityReport references
  for generated accounts; queued credit reservations are not settled on purge;
  registration-event cleanup is not user-scoped. Browser deletion covered only
  an empty disposable account, not these populated-account paths. These need
  separate backend remediation before claiming account deletion is reliable.
- Existing workshop error contexts can omit the current revision/history;
  generation truncates input to 200 characters despite the 2000-char field;
  the comparison endpoint is raw/incomplete JSON, not a complete visual diff.
  Withdrawn favorites and dashboard ordering also have prior limitations.
- Older README claims about failed review/no-charge and public deployment are
  inconsistent with current code. The redesign preserves visible review notes
  and existing charging behavior; it does not silently change that policy.
