"""A withdrawn text leaves every public page (owner decision, 20 Nov 2026).

The Socrates text is withdrawn on the server with `withdraw_publication`.
These tests fix what the reader sees afterwards: no library card, no
search hit, no `Next question` link, no empty thinker or collection page.
"""

import json
from io import StringIO
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.test import override_settings

from apps.library.editorial import load_card, submit_text
from apps.library.models import Publication
from apps.library.services import publish_revision
from apps.operations.models import AuditEvent

from .helpers import publish_demo_fixture
from .test_editorial_path import run_review
from .test_reading_path import READ_URL, SLUG, ReadingBase, page

SOCRATES = "socrates-questions"
CITY_SLUG = "why-do-people-live-in-cities"
CONTENT = Path(settings.BASE_DIR) / "content"


def publish_city_text(admin):
    """The second text of the library, through the same gate as the first."""
    data = json.loads((CONTENT / "texts" / f"{CITY_SLUG}.json").read_text(encoding="utf-8"))
    for reference in data["knowledge_cards"]:
        card_file = CONTENT / "cards" / f"{reference['slug']}-v{reference['version']}.json"
        load_card(json.loads(card_file.read_text(encoding="utf-8")), actor="test")
    revision, _ = submit_text(data, actor="test")
    run_review(revision)
    publication, _ = publish_revision(revision=revision, admin=admin, slug=CITY_SLUG)
    return publication


class WithdrawnTextTests(ReadingBase):
    def setUp(self):
        self.socrates = publish_demo_fixture()
        self.thinker_url = f"/thinkers/{self.socrates.thinker.slug}/"
        self.collection_url = f"/collections/{self.socrates.collections.get().slug}/"

    def withdraw(self):
        out = StringIO()
        call_command("withdraw_publication", SOCRATES, "--admin", "owner", stdout=out)
        return out.getvalue()

    def test_published_text_is_everywhere_before_withdrawal(self):
        # The control: every check below fails for a real reason, not by default.
        self.assertEqual(self.client.get(f"/read/{SOCRATES}/").status_code, 200)
        self.assertIn(SOCRATES, [item["publication"].slug for item in self.client.get("/library/").context["items"]])
        self.assertEqual([item["publication"].slug for item in self.client.get("/library/", {"q": "socrates"}).context["items"]], [SOCRATES])
        self.assertEqual(self.client.get(READ_URL).context["next"]["publication"].slug, SOCRATES)
        self.assertEqual(self.client.get(self.thinker_url).status_code, 200)
        self.assertEqual(self.client.get(self.collection_url).status_code, 200)

    def test_withdrawn_text_leaves_library_search_and_next_question(self):
        self.assertIn(SOCRATES, self.withdraw())
        self.assertEqual(Publication.objects.get(slug=SOCRATES).status, Publication.Status.WITHDRAWN)
        self.assertTrue(AuditEvent.objects.filter(action="editorial_withdraw").exists())

        self.assertEqual(self.client.get(f"/read/{SOCRATES}/").status_code, 404)

        library = self.client.get("/library/")
        self.assertEqual([item["publication"].slug for item in library.context["items"]], [SLUG])
        self.assertNotIn(f"/read/{SOCRATES}/", page(library))

        for query in ("socrates", "Сократ", SOCRATES):
            with self.subTest(query=query):
                found = self.client.get("/library/", {"q": query})
                self.assertEqual(found.context["items"], [])
                self.assertNotIn(f"/read/{SOCRATES}/", page(found))

        reader = self.client.get(READ_URL)
        self.assertIsNone(reader.context["next"])
        content = page(reader)
        self.assertNotIn(f"/read/{SOCRATES}/", content)
        self.assertIn("Next question soon.", content)

        with override_settings(START_READING_SLUG="no-such-text"):
            self.assertNotIn(f"/read/{SOCRATES}/", page(self.client.get("/")))
        self.assertNotIn(f"/read/{SOCRATES}/", page(self.client.get("/")))

    def test_next_question_of_the_city_text_does_not_lead_to_the_withdrawn_one(self):
        # The server has three texts: brave, cities and Socrates. After the
        # withdrawal the city text must offer a published text, not Socrates.
        city = publish_city_text(self.admin)
        city_url = f"/read/{city.slug}/"
        offered = {self.client.get(url).context["next"]["publication"].slug for url in (READ_URL, city_url, f"/read/{SOCRATES}/")}
        self.assertIn(SOCRATES, offered)  # before: Socrates is someone's next question
        self.withdraw()
        reader = self.client.get(city_url)
        self.assertEqual(reader.context["next"]["publication"].slug, SLUG)
        content = page(reader)
        self.assertIn(f'href="{READ_URL}"', content)
        self.assertNotIn(f"/read/{SOCRATES}/", content)
        self.assertEqual(self.client.get(READ_URL).context["next"]["publication"].slug, CITY_SLUG)
        library = self.client.get("/library/")
        self.assertEqual(sorted(item["publication"].slug for item in library.context["items"]), sorted([SLUG, CITY_SLUG]))

    def test_thinker_and_collection_without_published_texts_answer_404(self):
        self.withdraw()
        for url in (self.thinker_url, self.collection_url):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 404)
                self.assertNotIn("No texts here yet", page(response))

    def test_withdrawal_twice_changes_nothing(self):
        self.withdraw()
        self.assertIn("уже снято", self.withdraw())
        self.assertEqual(AuditEvent.objects.filter(action="editorial_withdraw").count(), 1)
