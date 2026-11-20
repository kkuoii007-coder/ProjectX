"""tools/qa/seed_browser_check.py: data for the browser check of the reader.

The script publishes texts with a stub in the reviewer's place, so the first
thing to keep is its refusal to run anywhere but on a throw-away database.
The second is that it still works: the `browser` job of the workflow starts
with it, and the editorial path it walks is the real one.
"""

import importlib.util
import io
from pathlib import Path
from types import SimpleNamespace

from django.conf import settings
from django.contrib.auth import authenticate, get_user_model
from django.test import TestCase, override_settings

from apps.ai.models import QualityReport
from apps.library.models import Publication

SCRIPT = Path(settings.BASE_DIR) / "tools" / "qa" / "seed_browser_check.py"
ALLOWED = {"PR_BROWSER_CHECK": "1"}


def load_script():
    spec = importlib.util.spec_from_file_location("seed_browser_check", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SeedBrowserCheckTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.seed = load_script()

    def run_script(self, argv=(), environ=None):
        out, err = io.StringIO(), io.StringIO()
        code = self.seed.main(list(argv), environ=ALLOWED if environ is None else environ, stdout=out, stderr=err)
        return code, out.getvalue(), err.getvalue()

    def assert_refused(self, environ=None):
        with self.assertNumQueries(0):
            code, out, err = self.run_script(environ=environ)
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("refused", err)
        self.assertEqual(Publication.objects.count(), 0)
        self.assertEqual(get_user_model().objects.count(), 0)
        return err

    def test_reasons_to_refuse(self):
        good = SimpleNamespace(LIVE_AI_ENABLED=False, AI_PROVIDER="fake", DEBUG=True)
        self.assertEqual(self.seed.refusals(ALLOWED, good), [])
        cases = [
            ({}, good, "PR_BROWSER_CHECK is not 1"),
            ({"PR_BROWSER_CHECK": "true"}, good, "PR_BROWSER_CHECK is not 1"),
            ({"PR_BROWSER_CHECK": "0"}, good, "PR_BROWSER_CHECK is not 1"),
            (ALLOWED, SimpleNamespace(LIVE_AI_ENABLED=True, AI_PROVIDER="fake", DEBUG=True), "LIVE_AI_ENABLED is on"),
            (ALLOWED, SimpleNamespace(LIVE_AI_ENABLED=False, AI_PROVIDER="openrouter", DEBUG=True), "AI_PROVIDER is not fake"),
            (ALLOWED, SimpleNamespace(LIVE_AI_ENABLED=False, AI_PROVIDER="fake", DEBUG=False), "DEBUG is off"),
            # A setting that is missing is read as the unsafe value.
            (ALLOWED, SimpleNamespace(), "LIVE_AI_ENABLED is on"),
        ]
        for environ, config, reason in cases:
            with self.subTest(reason=reason, environ=environ):
                self.assertIn(reason, self.seed.refusals(environ, config))
        self.assertEqual(len(self.seed.refusals({}, SimpleNamespace(LIVE_AI_ENABLED=True, AI_PROVIDER="openrouter", DEBUG=False))), 4)

    def test_refuses_without_the_variable(self):
        with override_settings(DEBUG=True, LIVE_AI_ENABLED=False, AI_PROVIDER="fake"):
            self.assertIn("PR_BROWSER_CHECK is not 1", self.assert_refused(environ={}))
            self.assertIn("PR_BROWSER_CHECK is not 1", self.assert_refused(environ={"PR_BROWSER_CHECK": "yes"}))

    def test_refuses_when_live_ai_is_on(self):
        with override_settings(DEBUG=True, LIVE_AI_ENABLED=True, AI_PROVIDER="fake"):
            self.assertIn("LIVE_AI_ENABLED is on", self.assert_refused())

    def test_refuses_a_real_provider(self):
        with override_settings(DEBUG=True, LIVE_AI_ENABLED=False, AI_PROVIDER="openrouter"):
            self.assertIn("AI_PROVIDER is not fake", self.assert_refused())

    def test_refuses_with_debug_off_as_on_a_server(self):
        with override_settings(DEBUG=False, LIVE_AI_ENABLED=False, AI_PROVIDER="fake"):
            self.assertIn("DEBUG is off", self.assert_refused())

    def test_the_warning_is_the_first_thing_in_the_file(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertLess(source.index("WARNING. NEVER RUN THIS ON A SERVER"), source.index("import "))

    @override_settings(DEBUG=True, LIVE_AI_ENABLED=False, AI_PROVIDER="fake")
    def test_publishes_both_texts_through_the_gate_and_makes_a_reader(self):
        code, out, err = self.run_script()
        self.assertEqual((code, err), (0, ""))
        texts = sorted(path.stem for path in (Path(settings.BASE_DIR) / "content" / "texts").glob("*.json"))
        self.assertEqual(sorted(self.seed.TEXTS), texts)  # every text of the repository, and only those
        self.assertEqual(sorted(Publication.objects.values_list("slug", flat=True)), texts)
        for publication in Publication.objects.all():
            self.assertEqual(self.client.get(f"/read/{publication.slug}/").status_code, 200)
        # The stub leaves its name on every report: nobody takes it for a review.
        self.assertEqual(set(QualityReport.objects.values_list("review_model", flat=True)), {self.seed.STUB_MODEL})
        self.assertIn("not-a-live-review", self.seed.STUB_MODEL)
        users = get_user_model().objects
        # The administrator publishes and cannot log in; no one else is given rights.
        self.assertEqual(list(users.filter(is_superuser=True).values_list("login", flat=True)), [self.seed.ADMIN_LOGIN])
        self.assertFalse(users.get(login=self.seed.ADMIN_LOGIN).has_usable_password())
        people = users.count()
        reader = authenticate(login=self.seed.READER_LOGIN, password=self.seed.READER_PASSWORD)
        self.assertIsNotNone(reader)
        self.assertFalse(reader.is_superuser or reader.is_staff)
        # Safe to repeat: nothing is published twice.
        self.assertEqual(self.run_script()[0], 0)
        self.assertEqual(Publication.objects.count(), 2)
        self.assertEqual(users.count(), people)

    @override_settings(DEBUG=True, LIVE_AI_ENABLED=False, AI_PROVIDER="fake")
    def test_olive_copy_and_its_new_revision_keep_one_address(self):
        self.assertEqual(self.run_script(["--olive-copy"])[0], 0)
        copy = Publication.objects.get(slug=self.seed.OLIVE_SLUG)
        self.assertEqual(copy.cover_tone, "olive")
        self.assertEqual(Publication.objects.count(), 3)
        first = copy.revision.content_hash
        self.assertEqual(self.run_script(["--new-revision"])[0], 0)
        self.assertEqual(Publication.objects.count(), 3)
        copy.refresh_from_db()
        self.assertNotEqual(copy.revision.content_hash, first)
        # The files of the repository are not touched.
        self.assertFalse((Path(settings.BASE_DIR) / "content" / "texts" / f"{self.seed.OLIVE_SLUG}.json").exists())
