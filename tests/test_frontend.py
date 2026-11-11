"""Frontend contract: home and reader render real data, markup keeps the
accessibility hooks the JS relies on, and presentation changes do not touch
auth, credits or CSRF behaviour."""

import json

from django.core.management import call_command
from django.test import Client, TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.credits.models import Wallet, grant_welcome_credits
from apps.library.models import Favorite, Publication
from apps.library.presenters import SENTENCES_PER_PARAGRAPH, outline, pick_specimen, reader_parts, reader_terms
from apps.library.templatetags.ui import ru_plural


class PresenterTests(TestCase):
    PAYLOAD = {
        "parts": [
            {
                "title_en": "One",
                "title_ru": "Один",
                "sentences": [{"en": f"Sentence {i}.", "ru": f"Предложение {i}."} for i in range(7)],
            },
            {"title_en": "Two", "sentences": []},
            "not-a-dict",
        ],
        "terms": [{"en": "virtue", "ru": "добродетель"}],
        "glossary": [{"term_en": "ethics", "translation_ru": "этика", "explanation_en": "Study of right and wrong."}],
    }

    def test_parts_are_split_into_paragraphs_with_unique_ids(self):
        parts = reader_parts(self.PAYLOAD)
        self.assertEqual([p["number"] for p in parts], [1, 2])
        first = parts[0]
        self.assertEqual(len(first["paragraphs"]), 2)
        self.assertEqual(len(first["paragraphs"][0]), SENTENCES_PER_PARAGRAPH)
        ids = [s["id"] for paragraph in first["paragraphs"] for s in paragraph]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(parts[1]["paragraphs"], [])

    def test_helpers_tolerate_empty_payload(self):
        self.assertEqual(reader_parts(None), [])
        self.assertEqual(reader_parts({}), [])
        self.assertEqual(outline({})["part_count"], 0)
        self.assertEqual(reader_terms({}), [])
        self.assertIsNone(pick_specimen([]))

    def test_terms_merge_seed_and_pipeline_shapes(self):
        terms = reader_terms(self.PAYLOAD)
        self.assertEqual([t["en"] for t in terms], ["virtue", "ethics"])
        self.assertEqual(terms[1]["note"], "Study of right and wrong.")

    def test_specimen_requires_translation_and_reasonable_length(self):
        parts = reader_parts({"parts": [{"title_en": "A", "sentences": [{"en": "x" * 400, "ru": "длинно"}, {"en": "Short one.", "ru": "Коротко."}]}]})
        specimen = pick_specimen(parts)
        self.assertEqual(specimen["en"], "Short one.")
        self.assertEqual(specimen["id"], "gloss-specimen")

    def test_ru_plural(self):
        self.assertEqual(ru_plural(1, "часть,части,частей"), "1 часть")
        self.assertEqual(ru_plural(3, "часть,части,частей"), "3 части")
        self.assertEqual(ru_plural(5, "часть,части,частей"), "5 частей")
        self.assertEqual(ru_plural(11, "часть,части,частей"), "11 частей")
        self.assertEqual(ru_plural(21, "часть,части,частей"), "21 часть")


class HomeAndReaderTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", verbosity=0)
        cls.publication = Publication.objects.get(slug="socrates-questions")
        cls.user = User.objects.create_user(login="frontuser", password="TestPassword123!")
        grant_welcome_credits(cls.user)

    def test_home_shows_real_material_and_workshop_for_guest(self):
        response = self.client.get(reverse("library:home"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.publication.revision.payload["title_ru"])
        self.assertContains(response, reverse("library:read", args=[self.publication.slug]))
        self.assertContains(response, 'id="workshop"')
        self.assertContains(response, reverse("workshop:create_project"))
        self.assertContains(response, "csrfmiddlewaretoken")
        self.assertContains(response, "Для создания текста нужен вход")
        # Part titles from the real payload appear in the contents list.
        self.assertContains(response, "A different kind of wisdom")
        # Credit numbers come from settings, not literals.
        self.assertNotContains(response, "Доступно:")

    def test_home_shows_balance_for_signed_in_user_without_login_hint(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("library:home"))
        self.assertContains(response, "balance__value")
        self.assertNotContains(response, "Для создания текста нужен вход")

    def test_home_without_publications_is_honest_empty_state(self):
        Publication.objects.update(status=Publication.Status.HIDDEN)
        response = self.client.get(reverse("library:home"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Первые материалы готовятся")
        self.assertContains(response, 'id="workshop"')
        self.assertNotContains(response, "Начать читать")

    def test_reader_markup_keeps_translation_hooks(self):
        response = self.client.get(reverse("library:read", args=[self.publication.slug]))
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        sentences = sum(len(p["sentences"]) for p in self.publication.revision.payload["parts"])
        self.assertEqual(body.count('class="s" role="button" tabindex="0" aria-expanded="false"'), sentences)
        self.assertEqual(body.count('role="note"'), sentences)
        # Every aria-controls target exists exactly once.
        for part in reader_parts(self.publication.revision.payload):
            for paragraph in part["paragraphs"]:
                for s in paragraph:
                    self.assertEqual(body.count(f'id="{s["id"]}"'), 1)
        self.assertContains(response, "Сократ жил в Афинах")
        self.assertContains(response, "Сократ")
        self.assertContains(response, "Примечание редакции")
        self.assertContains(response, "добродетель")

    def test_reader_guest_has_no_mutating_forms(self):
        response = self.client.get(reverse("library:read", args=[self.publication.slug]))
        self.assertNotContains(response, "favorite")
        self.assertNotContains(response, reverse("library:create_report", args=[self.publication.slug]))
        self.assertContains(response, reverse("accounts:login"))

    def test_reader_user_gets_favorite_and_report_forms_with_csrf(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        response = client.get(reverse("library:read", args=[self.publication.slug]))
        self.assertContains(response, reverse("account:favorite_toggle", args=[self.publication.pk]))
        self.assertContains(response, reverse("library:create_report", args=[self.publication.slug]))
        self.assertContains(response, "Добавить в избранное")
        self.assertEqual(response.content.decode().count("csrfmiddlewaretoken"), 3)
        # Without a token the toggle is rejected: the redesign did not weaken CSRF.
        rejected = client.post(reverse("account:favorite_toggle", args=[self.publication.pk]))
        self.assertEqual(rejected.status_code, 403)

    def test_reader_favorite_state_reflects_database(self):
        Favorite.objects.create(user=self.user, publication=self.publication)
        self.client.force_login(self.user)
        response = self.client.get(reverse("library:read", args=[self.publication.slug]))
        self.assertContains(response, "Убрать из избранного")
        other = User.objects.create_user(login="otherfront", password="TestPassword123!")
        self.client.force_login(other)
        response = self.client.get(reverse("library:read", args=[self.publication.slug]))
        self.assertContains(response, "Добавить в избранное")

    def test_report_still_works_after_redesign(self):
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("library:create_report", args=[self.publication.slug]),
            {"category": "bad_translation", "details": "Проверка", "format": "json"},
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(json.loads(response.content)["code"], "ok")

    def test_hidden_publication_is_not_readable(self):
        Publication.objects.filter(pk=self.publication.pk).update(status=Publication.Status.HIDDEN)
        self.assertEqual(self.client.get(reverse("library:read", args=[self.publication.slug])).status_code, 404)

    def test_odd_payload_does_not_break_presenter(self):
        payload = {"title_ru": "Странный", "parts": [{"title_en": "Only title"}, {"sentences": [{"en": "Hi."}]}]}
        parts = reader_parts(payload)
        self.assertEqual(len(parts), 2)
        self.assertEqual(parts[1]["paragraphs"][0][0]["ru"], "")

    def test_wallet_is_unchanged_by_page_views(self):
        self.client.force_login(self.user)
        before = Wallet.objects.get(user=self.user).total_credits
        for name, args in (("library:home", []), ("library:read", [self.publication.slug]), ("library:library", [])):
            self.client.get(reverse(name, args=args))
        self.assertEqual(Wallet.objects.get(user=self.user).total_credits, before)

    def test_static_assets_referenced_by_base_exist(self):
        from django.contrib.staticfiles import finders

        for path in (
            "css/main.css",
            "css/fonts.css",
            "js/reader.js",
            "fonts/literata-latin-opsz-normal.woff2",
            "fonts/literata-cyrillic-opsz-normal.woff2",
            "fonts/golos-text-cyrillic-wght-normal.woff2",
            "fonts/OFL-Literata.txt",
            "fonts/OFL-Golos-Text.txt",
        ):
            self.assertIsNotNone(finders.find(path), path)
