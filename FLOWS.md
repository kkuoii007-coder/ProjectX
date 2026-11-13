# Redesign v1: acceptance and browser inventory

Baseline: `main@247b81ba1932aa51caf002cb762c0b0b0d14cb0b`.
This checklist is repository documentation, not served by Django.
Production inspection failed from this sandbox (tunnel/reset errors).
Local baseline and final screenshots must use the actual Django app, not a mockup.

## Public reading

- Home: real featured publication, specimen translation, part links, collections,
  thinkers, library and workshop form. No fabricated articles, images or metrics.
- Library: search by Russian title/thinker, results, no-results, reset search.
- Collection/thinker: real linked publications; return to library.
- Reader: original English/Russian payload unchanged; Enter/Space/click toggle;
  Escape closes; show/hide all; mixed individual/all state; selection does not
  toggle; chapter navigation and progress; terms and editorial note.
- Anonymous reader: login hint rather than favorite/report mutations.
- Without JS: translations and navigation remain available, no dead reader controls.

## Account and forms

- Login: invalid error, valid demo login, logout via POST, menu keyboard control.
- Registration: invite required, invalid invite, safe test invite success.
- Dashboard: projects and job status; all account navigation reachable.
- Favorites: add/remove in reader and remove from favorites, safe return path.
- Words: validation and save; display entered term/translation.
- Credits/help: actual wallet and configured costs; no fake purchase action.
- Security: wrong/current password handling, change password, logout all,
  export/download, deletion only of disposable sandbox account.
- POST forms retain CSRF; missing token rejected; owner isolation unchanged.

## Workshop

- New project: required topic; fake-only job enqueue and result.
- Project: genuine revision payload and sentence translations; edit sentence and
  edit part forms; question form; fake-only job completion.
- Versions: comparison (existing JSON endpoint), explicit apply and current marker.
- Error/empty/queued states remain usable, no fabricated progress or polling.

## Visual and robustness matrix

- Actual pages at 390, 768, 1440 px; additional 320 px and 200% reflow.
- Home and reader baseline/final; catalogue, auth, account, workshop final.
- No document horizontal overflow; local tables may scroll with keyboard access.
- Long titles and unbroken words, long translations, many open translations.
- Focus visible, skip link, Tab/Enter/Space/Escape, no sticky obstruction.
- Mobile menu desktop → mobile → desktop; reduced motion; no-JS fallback.
- Inspect screenshots, not only geometry; check console/network errors.
- Explicitly separate automated tests, browser evidence and unverified production.
