#!/usr/bin/env python
"""Data for the browser check of the reader (tools/qa/fork_steps.js).

WARNING. NEVER RUN THIS ON A SERVER, AND NEVER AGAINST A DATABASE WITH REAL
READERS OR REAL TEXTS. It is for a throw-away database only: the `browser`
job of .github/workflows/tests.yml and a local check.

What it does, and why it must stay away from real data:
  * it publishes both content/texts/*.json through the real editorial path
    (submit_text -> review_text -> publish_text, so the publication gate runs),
    but the reviewer is a stub that passes any text. No call is made and
    nothing is paid; the quality report it leaves is NOT a review;
  * it creates an administrator (no password, cannot log in) to publish on
    whose behalf, and a reader whose password is written in this file.

The script refuses to do anything unless all of this is true:
  * the variable PR_BROWSER_CHECK is exactly "1";
  * settings.LIVE_AI_ENABLED is off;
  * settings.AI_PROVIDER is "fake";
  * settings.DEBUG is on (a server runs with DEBUG off).
The refusal comes before the first query to the database.

Usage (from the project root, with the variables of a throw-away database):

  PR_BROWSER_CHECK=1 python tools/qa/seed_browser_check.py
      both texts and the reader; safe to repeat
  PR_BROWSER_CHECK=1 python tools/qa/seed_browser_check.py --olive-copy
      also a copy of one text with the olive cover (/read/browser-check-olive/):
      no text in content/texts has this cover yet
  PR_BROWSER_CHECK=1 python tools/qa/seed_browser_check.py --new-revision
      a new revision of that copy, published at the same address (for the
      "new revision" step of fork_steps.js, --republish)
"""

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
TEXTS = ("what-does-it-mean-to-be-brave", "why-do-people-live-in-cities")
OLIVE_SOURCE = "why-do-people-live-in-cities"
OLIVE_SLUG = "browser-check-olive"
ADMIN_LOGIN = "browser-check-admin"
READER_LOGIN = "browser-check-reader"
READER_PASSWORD = "browser-check-only"
STUB_MODEL = "browser-check-stub-not-a-live-review"


def refusals(environ, settings):
    """Why the script must not run here. An empty list means it may."""
    reasons = []
    if environ.get("PR_BROWSER_CHECK") != "1":
        reasons.append("PR_BROWSER_CHECK is not 1")
    if getattr(settings, "LIVE_AI_ENABLED", True):
        reasons.append("LIVE_AI_ENABLED is on")
    if getattr(settings, "AI_PROVIDER", None) != "fake":
        reasons.append("AI_PROVIDER is not fake")
    if not getattr(settings, "DEBUG", False):
        reasons.append("DEBUG is off")
    return reasons


class StubReviewer:
    """Stands in for the live reviewer and passes any text. Not a review."""

    editorial_review_prompt_version = "review-v3"

    def review_revision(self, payload, cards, job=None):
        self._attempts = [{
            "request_hash": "0" * 64,
            "provider_request_id": "browser-check-stub",
            "usage_json": {},
            "model": STUB_MODEL,
            "cost_usd": None,
        }]
        return {
            "verdict": "pass",
            "factual_state": "supported",
            "issues": [],
            "unsupported_claims": [],
            "language_notes": [],
            "translation_issues": [],
            "uncertainty_notes": ["BROWSER CHECK STUB: this text was not reviewed"],
        }


def publish(path, stdout):
    """One text file through the editorial path, the stub in the reviewer's place."""
    from django.core.management import call_command

    call_command("submit_text", str(path), stdout=stdout)
    with patch("apps.ai.services.get_provider", return_value=StubReviewer()):
        call_command("review_text", str(path), "--inline", stdout=stdout)
    call_command("publish_text", str(path), "--admin", ADMIN_LOGIN, stdout=stdout)


def publish_olive_copy(stdout):
    """A copy of a real text under its own address, olive cover. Each call
    changes one Russian note, so each call is a new revision."""
    data = json.loads((ROOT / "content" / "texts" / f"{OLIVE_SOURCE}.json").read_text(encoding="utf-8"))
    data["slug"] = OLIVE_SLUG
    data["cover"] = "olive"
    note = data["payload"]["simplification_note_ru"].rstrip()
    data["payload"]["simplification_note_ru"] = f"{note} (Копия для проверки в браузере, {time.time_ns()}.)"
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / f"{OLIVE_SLUG}.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        publish(path, stdout)


def people():
    from django.contrib.auth import get_user_model

    users = get_user_model().objects
    if not users.filter(login=ADMIN_LOGIN).exists():
        users.create_superuser(login=ADMIN_LOGIN, password=None)  # no password: nobody logs in as this one
    if not users.filter(login=READER_LOGIN).exists():
        users.create_user(login=READER_LOGIN, password=READER_PASSWORD)


def seed(stdout, olive_copy=False, new_revision=False):
    people()
    if not new_revision:
        for slug in TEXTS:
            publish(ROOT / "content" / "texts" / f"{slug}.json", stdout)
    if olive_copy or new_revision:
        publish_olive_copy(stdout)


def main(argv=None, environ=None, stdout=None, stderr=None):
    environ = os.environ if environ is None else environ
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    parser = argparse.ArgumentParser(description="Data for the browser check of the reader. Never for a server.")
    parser.add_argument("--olive-copy", action="store_true", help="also publish a copy of one text with the olive cover")
    parser.add_argument("--new-revision", action="store_true", help="only publish a new revision of that copy")
    options = parser.parse_args(argv)

    if environ.get("PR_BROWSER_CHECK") != "1":
        # Before Django is even loaded.
        stderr.write("seed_browser_check: refused (PR_BROWSER_CHECK is not 1). This script is for a throw-away database only.\n")
        return 2

    import django
    from django.conf import settings

    if not settings.configured:
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
    reasons = refusals(environ, settings)
    if reasons:
        stderr.write("seed_browser_check: refused (" + "; ".join(reasons) + "). This script is for a throw-away database only.\n")
        return 2
    django.setup()
    seed(stdout, olive_copy=options.olive_copy, new_revision=options.new_revision)
    stdout.write(f"seed_browser_check: done. Reader: {READER_LOGIN} / {READER_PASSWORD}\n")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    sys.exit(main())
