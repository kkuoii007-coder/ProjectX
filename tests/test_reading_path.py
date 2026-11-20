"""Stages E and F: the cover of the home page, the library and the reader.

The first text reaches these pages the real way: submit_text -> review
(stubbed live reviewer) -> publish_revision. Nothing here creates a
Publication or a QualityReport by hand for real content.
"""

import html
import re
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings

from apps.accounts.models import User
from apps.ai.models import AIJob
from apps.credits.models import Wallet
from apps.library.editorial import EditorialError, load_card, submit_text
from apps.library.models import Publication
from apps.library.presenters import about_text, cover_lines, reader_parts, reader_terms, titles
from apps.library.services import publish_revision
from apps.operations.models import AuditEvent

from .helpers import publish_demo_fixture, put_book_on_shelf
from .test_editorial_path import PASS_REVIEW, TEXT_FILE, StubReviewer, card_data, run_review, text_data

SLUG = "what-does-it-mean-to-be-brave"
READ_URL = f"/read/{SLUG}/"
QUESTION = "What does it mean to be brave?"
CONTACT = "evgeniy.shults.2016@mail.ru"


def page(response):
    """Rendered HTML with entities decoded, so text is compared as written."""
    return html.unescape(response.content.decode("utf-8"))


def visible_text(response):
    """The page without tags, scripts and styles: what a reader can see or hear."""
    content = re.sub(r"(?s)<(script|style)\b.*?</\1>", " ", response.content.decode("utf-8"))
    return html.unescape(re.sub(r"<[^>]+>", " ", content))


class ReadingBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(login="owner", password="TestPassword123!", is_staff=True, is_superuser=True)
        load_card(card_data(), actor="test")
        cls.revision, _ = submit_text(text_data(), actor="test")
        run_review(cls.revision)
        cls.publication, _ = publish_revision(revision=cls.revision, admin=cls.admin, slug=SLUG)
        cls.payload = text_data()["payload"]


# --- stage E: home ---------------------------------------------------------------


class CoverTests(ReadingBase):
    def test_cover_shows_the_question_two_sentences_and_one_button(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        content = page(response)
        first, second = self.payload["parts"][0]["sentences"][:2]
        self.assertIn(QUESTION, content)
        for sentence in (first, second):
            self.assertIn(sentence["en"], content)
            self.assertIn(sentence["ru"], content)
        self.assertIn("Start reading", content)
        self.assertIn("Big questions in simple English.", content)
        self.assertIn("For adult beginners.", content)
        self.assertIn("Tap a sentence for Russian.", content)
        self.assertIn("More questions soon.", content)
        self.assertEqual([line["en"] for line in response.context["start"]["lines"]], [first["en"], second["en"]])
        # The third sentence is not on the cover: exactly two lines.
        self.assertNotIn(self.payload["parts"][0]["sentences"][2]["en"], content)

    def test_cover_has_no_registration_credits_generator_or_search(self):
        user = User.objects.create_user(login="reader", password="TestPassword123!")
        for client_user in (None, user):
            with self.subTest(signed_in=bool(client_user)):
                if client_user:
                    self.client.force_login(client_user)
                response = self.client.get("/")
                text = visible_text(response).lower()
                content = page(response)
                for word in ("регистрац", "кредит", "мастерск", "создать текст", "sign up", "register", "credit", "search", "поиск"):
                    self.assertNotIn(word, text, word)
                self.assertNotIn("/accounts/register/", content)
                self.assertNotIn("/projects/new/", content)
                self.assertNotIn("<textarea", content)
                self.assertNotIn('type="search"', content)
                self.assertNotIn("Редакционная подборка", content)

    def test_one_text_is_shown_once(self):
        content = page(self.client.get("/"))
        self.assertEqual(content.count(f'href="{READ_URL}"'), 1)
        self.assertEqual(len(re.findall(r"<h[1-6][^>]*>[^<]*" + re.escape(QUESTION), content)), 1)
        self.assertEqual(self.client.get("/").context["others"], [])

    def test_reading_path_is_english_with_the_cover_tone(self):
        content = page(self.client.get("/"))
        self.assertRegex(content, r'<html[^>]*\blang="en"')
        self.assertIn('data-cover="clay"', content)
        self.assertIn("data-ru-toggle", content)
        self.assertIn('lang="ru"', content)  # Russian captions are marked as Russian

    def test_start_reading_is_named_explicitly_not_the_latest(self):
        later = publish_demo_fixture()  # published after the first text
        self.assertGreater(later.published_at, Publication.objects.get(slug=SLUG).published_at)
        response = self.client.get("/")
        self.assertEqual(response.context["start"]["publication"].slug, SLUG)
        self.assertEqual([item["publication"].slug for item in response.context["others"]], ["socrates-questions"])
        content = page(response)
        self.assertEqual(content.count(f'href="{READ_URL}"'), 1)
        self.assertEqual(content.count('href="/read/socrates-questions/"'), 1)
        self.assertNotIn("More questions soon.", content)

    def test_cover_never_falls_back_to_the_latest_publication(self):
        publish_demo_fixture()
        for slug in ("no-such-text", ""):
            with self.subTest(slug=slug), override_settings(START_READING_SLUG=slug):
                response = self.client.get("/")
                self.assertEqual(response.status_code, 200)
                self.assertIsNone(response.context["start"])
                content = page(response)
                # Other questions are listed below, so the cover must not say that none exist yet.
                self.assertIn("Choose a question.", content)
                self.assertNotIn("The first question is coming soon.", content)
                self.assertNotIn("Start reading", content)
                self.assertNotIn("More questions soon.", content)
                self.assertEqual(len(response.context["others"]), 2)
                for slug_shown in (SLUG, "socrates-questions"):
                    self.assertEqual(content.count(f'href="/read/{slug_shown}/"'), 1)

    def test_withdrawn_start_reading_leaves_the_cover_empty(self):
        Publication.objects.filter(slug=SLUG).update(status=Publication.Status.WITHDRAWN)
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["start"])
        self.assertNotIn(QUESTION, page(response))

    def test_empty_site_still_opens(self):
        Publication.objects.all().delete()
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("The first question is coming soon.", page(response))

    def test_no_template_code_leaks_onto_a_page(self):
        user = User.objects.create_user(login="leak", password="TestPassword123!")
        for signed_in in (False, True):
            if signed_in:
                self.client.force_login(user)
            for url in ("/", "/library/", READ_URL, "/accounts/login/", "/help/credits/", "/read/nope/", "/account/", "/projects/new/"):
                text = visible_text(self.client.get(url, follow=True))
                for marker in ("{#", "#}", "{%", "%}", "{{", "}}"):
                    self.assertNotIn(marker, text, f"{url}: {marker}")

    def test_pages_of_the_reading_path_change_nothing(self):
        user = User.objects.create_user(login="reader", password="TestPassword123!")
        self.client.force_login(user)
        before = (Wallet.objects.filter(user=user).values_list("total_credits", "reserved_credits").first(), AIJob.objects.count(), AuditEvent.objects.count())
        for url in ("/", "/library/", READ_URL):
            self.assertEqual(self.client.get(url).status_code, 200)
        after = (Wallet.objects.filter(user=user).values_list("total_credits", "reserved_credits").first(), AIJob.objects.count(), AuditEvent.objects.count())
        self.assertEqual(before, after)


class CoverToneTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(login="owner", password="TestPassword123!", is_staff=True, is_superuser=True)
        load_card(card_data(), actor="test")

    def publish_file(self, data, folder):
        import json
        from pathlib import Path

        path = Path(folder) / "text.json"
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        out = StringIO()
        call_command("submit_text", str(path), stdout=out)
        with patch("apps.ai.services.get_provider", return_value=StubReviewer(PASS_REVIEW)):
            call_command("review_text", str(path), "--inline", stdout=out)
        call_command("publish_text", str(path), stdout=out)
        return Publication.objects.get(slug=SLUG)

    def test_first_text_file_names_its_tone(self):
        self.assertEqual(text_data()["cover"], "clay")

    def test_tone_comes_from_the_file_and_needs_no_new_review(self):
        import tempfile

        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(self.publish_file(text_data(), folder).cover_tone, "clay")
            self.assertIn('data-cover="clay"', page(self.client.get("/")))
            recoloured = {**text_data(), "cover": "sea"}
            publication = self.publish_file(recoloured, folder)  # same payload, same revision, same report
        self.assertEqual(publication.cover_tone, "sea")
        self.assertEqual(Publication.objects.count(), 1)
        self.assertIn('data-cover="sea"', page(self.client.get("/")))
        self.assertIn('data-cover="sea"', page(self.client.get(READ_URL)))

    def test_file_without_a_tone_keeps_the_stored_one_and_a_change_is_audited(self):
        import tempfile

        with tempfile.TemporaryDirectory() as folder:
            self.publish_file({**text_data(), "cover": "olive"}, folder)
            change = AuditEvent.objects.get(action="cover_tone_changed")
            self.assertEqual(change.sanitized_metadata, {"from": "clay", "to": "olive"})
            silent = text_data()
            del silent["cover"]
            self.assertEqual(self.publish_file(silent, folder).cover_tone, "olive")
        self.assertEqual(AuditEvent.objects.filter(action="cover_tone_changed").count(), 1)

    def test_long_cover_lines_are_reported_but_do_not_block(self):
        from apps.library.editorial import check_editorial_payload

        payload = text_data()["payload"]
        self.assertEqual([w for w in check_editorial_payload(payload)[1]["warnings_ru"] if w.startswith("Обложка")], [])
        payload["title_en"] = "What does it mean to live a good life when nobody is watching you?"
        payload["parts"][0]["sentences"][1]["en"] = "Their names are Socrates and Laches, and they are old friends from the same city."
        errors, diagnostics = check_editorial_payload(payload)
        self.assertEqual(errors, [])
        self.assertEqual(len([w for w in diagnostics["warnings_ru"] if w.startswith("Обложка")]), 2)

    def test_unknown_tone_is_refused_and_missing_tone_is_clay(self):
        with self.assertRaises(EditorialError):
            submit_text({**text_data(), "cover": "crimson"}, actor="test")
        data = text_data()
        del data["cover"]
        revision, _ = submit_text(data, actor="test")
        run_review(revision)
        publication, _ = publish_revision(revision=revision, admin=self.admin, slug=SLUG)
        self.assertEqual(publication.cover_tone, "clay")


# --- stage E: library ----------------------------------------------------------------


class LibraryTests(ReadingBase):
    def test_library_lists_the_question_in_english_once(self):
        response = self.client.get("/library/")
        content = page(response)
        self.assertRegex(content, r'<html[^>]*\blang="en"')
        self.assertIn(QUESTION, content)
        self.assertIn(self.payload["title_ru"], content)  # Russian caption
        self.assertEqual(content.count(f'href="{READ_URL}"'), 1)
        self.assertNotIn("Редакционная подборка", content)
        self.assertEqual(response.context["items"][0]["tone"], "clay")

    def test_search_is_offered_only_when_there_is_something_to_search(self):
        response = self.client.get("/library/")
        self.assertFalse(response.context["show_search"])
        self.assertNotIn('type="search"', page(response))
        found = self.client.get("/library/", {"q": "brave"})  # the existing query still works
        self.assertTrue(found.context["show_search"])
        self.assertEqual([item["publication"].slug for item in found.context["items"]], [SLUG])
        self.assertEqual(self.client.get("/library/", {"q": "zzz"}).context["items"], [])
        with patch("apps.library.views.LIBRARY_SEARCH_MIN", 1):
            self.assertIn('type="search"', page(self.client.get("/library/")))

    def test_thinker_and_collection_pages_use_the_same_items(self):
        publication = publish_demo_fixture()
        for url in (f"/thinkers/{publication.thinker.slug}/", f"/collections/{publication.collections.get().slug}/"):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertEqual([item["publication"].slug for item in response.context["items"]], ["socrates-questions"])
                self.assertEqual(page(response).count('href="/read/socrates-questions/"'), 1)


# --- stage F: reader --------------------------------------------------------------------


class ReaderTests(ReadingBase):
    def test_every_sentence_and_translation_is_shown_unchanged_and_in_order(self):
        content = page(self.client.get(READ_URL))
        position = 0
        for part in self.payload["parts"]:
            for sentence in part["sentences"]:
                for text in (sentence["en"], sentence["ru"]):
                    found = content.find(text, position)
                    self.assertGreater(found, -1, text)
                    position = found
        shown = re.findall(r'<span[^>]*\bdata-gloss="gloss-\d+-\d+"[^>]*>(.*?)</span>', self.client.get(READ_URL).content.decode("utf-8"))
        expected = [s["en"] for part in self.payload["parts"] for s in part["sentences"]]
        self.assertEqual([html.unescape(s) for s in shown], expected)

    def test_text_starts_before_notes_contents_and_instructions(self):
        content = page(self.client.get(READ_URL))
        first_sentence = content.index(self.payload["parts"][0]["sentences"][0]["en"])
        self.assertLess(content.index(QUESTION, content.index("<body")), first_sentence)
        self.assertLess(first_sentence, content.index(self.payload["simplification_note_ru"]))
        self.assertLess(first_sentence, content.index("About this text"))
        # The parts that come later are not listed ahead of the text.
        self.assertLess(first_sentence, content.index(self.payload["parts"][3]["title_en"], content.index("<body")))
        for old in ("Редакционная подборка", "Примечание редакции", "не тратит кредиты"):
            self.assertNotIn(old, content)

    def test_fork_sits_between_part_one_and_part_two(self):
        response = self.client.get(READ_URL)
        content = page(response)
        fork = self.payload["parts"][0]["fork"]
        parts = response.context["parts"]
        self.assertEqual(parts[0]["fork"]["next_part"], 2)
        self.assertEqual([parts[1]["fork"]["next_part"], parts[2]["fork"]["next_part"]], [3, 4])
        self.assertIsNone(parts[3]["fork"])
        last_of_first = content.index(self.payload["parts"][0]["sentences"][-1]["en"])
        first_of_second = content.index(self.payload["parts"][1]["sentences"][0]["en"])
        prompt = content.index(fork["prompt"]["en"])
        self.assertLess(last_of_first, prompt)
        self.assertLess(prompt, first_of_second)
        self.assertIn(fork["title_en"], content)
        self.assertIn(fork["prompt"]["ru"], content)
        for option in fork["options"]:
            for text in (option["en"], option["ru"]):
                found = content.index(text)
                self.assertLess(prompt, found)
                self.assertLess(found, first_of_second)
            self.assertIn(option["label_en"], content[prompt:first_of_second])
        self.assertIn('href="#part-2"', content[prompt:first_of_second])

    def test_fork_is_not_a_form_and_is_never_scored(self):
        content = page(self.client.get(READ_URL))
        start = content.index(self.payload["parts"][0]["fork"]["prompt"]["en"])
        block = content[start:content.index(self.payload["parts"][1]["sentences"][0]["en"])]
        for marker in ("<form", "<input", 'name="', "csrfmiddlewaretoken"):
            self.assertNotIn(marker, block)
        for word in ("correct", "wrong answer", "right answer", "score", "верно", "правильн", "ошибк"):
            self.assertNotIn(word, block.lower())

    def test_fork_offers_the_hint_and_marks_the_part_it_opens(self):
        content = page(self.client.get(READ_URL))
        self.assertIn("Choose an answer to read on. Any answer is fine.", content)
        self.assertIn("Выберите ответ, чтобы читать дальше", content)
        # The fork names the part it opens; the choice itself happens in JS.
        self.assertIn('data-fork-id="fork-1"', content)
        self.assertIn('data-fork-next="2"', content)
        self.assertIn('data-fork-key="yes"', content)

    def test_end_of_the_text(self):
        response = self.client.get(READ_URL)
        content = page(response)
        text = re.sub(r"\s+", " ", visible_text(response))
        last_sentence = content.index(self.payload["parts"][-1]["sentences"][-1]["en"])
        about = content.index("About this text")
        self.assertLess(last_sentence, about)
        self.assertIn("Written and checked by AI. Source: Plato, Laches.", text)
        self.assertLess(about, content.index(self.payload["simplification_note_ru"]))
        self.assertIn("Next question soon.", content)
        self.assertEqual(content.count("mailto:"), 1)
        self.assertRegex(content, rf'<a[^>]*href="mailto:{re.escape(CONTACT)}"[^>]*>\s*Write to us')
        for term in self.payload["glossary"]:
            for value in term.values():
                self.assertIn(value, content)

    def test_guest_gets_one_feedback_link_and_no_login_prompt(self):
        content = page(self.client.get(READ_URL))
        self.assertNotIn("Войдите", content)
        self.assertNotIn("/accounts/login/?", content)
        self.assertNotIn('action="/reports/', content)
        self.assertNotIn("favorite", content)

    def test_nothing_claims_a_human_check(self):
        for url in ("/", "/library/", READ_URL):
            text = visible_text(self.client.get(url)).lower()
            for claim in ("human", "editor", "reviewed by", "проверен человеком", "проверено человеком", "редактор", "редакци", "вычитан", "эксперт"):
                self.assertNotIn(claim, text, f"{url}: {claim}")

    def test_signed_in_reader_keeps_favourite_and_report(self):
        user = User.objects.create_user(login="reader", password="TestPassword123!")
        self.client.force_login(user)
        content = page(self.client.get(READ_URL))
        self.assertIn(f'action="/account/favorites/{self.publication.pk}/toggle/"', content)
        self.assertIn(f'action="/reports/{SLUG}/"', content)
        self.assertEqual(content.count("mailto:"), 1)
        response = self.client.post(f"/reports/{SLUG}/", {"category": "other", "details": "Опечатка"})
        self.assertEqual(response.status_code, 302)

    def test_next_question_replaces_the_soon_line(self):
        publish_demo_fixture()
        response = self.client.get(READ_URL)
        content = page(response)
        self.assertEqual(response.context["next"]["publication"].slug, "socrates-questions")
        self.assertNotIn("Next question soon.", content)
        self.assertIn('href="/read/socrates-questions/"', content)
        back = self.client.get("/read/socrates-questions/")
        self.assertEqual(back.context["next"]["publication"].slug, SLUG)

    def test_older_text_without_english_title_or_byline_still_reads(self):
        publish_demo_fixture()
        response = self.client.get("/read/socrates-questions/")
        self.assertEqual(response.status_code, 200)
        content = page(response)
        self.assertEqual(response.context["about"]["byline_en"], "")
        self.assertNotIn("Written and checked by AI", content)
        self.assertNotIn("Source: .", content)
        self.assertIn("Write to us", content)

    def test_reader_has_the_switch_tone_and_language(self):
        content = page(self.client.get(READ_URL))
        self.assertRegex(content, r'<html[^>]*\blang="en"')
        self.assertIn('data-cover="clay"', content)
        self.assertIn("data-ru-toggle", content)
        self.assertIn("Tap a sentence for Russian.", content)

    @override_settings(CONTACT_EMAIL="reader@example.org")
    def test_contact_address_is_a_setting(self):
        self.assertIn('href="mailto:reader@example.org"', page(self.client.get(READ_URL)))

    @override_settings(CONTACT_EMAIL="")
    def test_no_address_means_no_dead_link(self):
        content = page(self.client.get(READ_URL))
        self.assertNotIn("mailto:", content)
        self.assertNotIn("Write to us", content)

    def test_fork_after_the_last_part_offers_no_way_on(self):
        parts = reader_parts(self.payload)
        parts[0]["fork"]["next_part"] = None
        parts[1]["fork"]["next_part"] = None
        parts[2]["fork"]["next_part"] = None
        with patch("apps.library.views.reader_parts", return_value=parts):
            content = page(self.client.get(READ_URL))
        self.assertIn(self.payload["parts"][0]["fork"]["prompt"]["en"], content)
        self.assertNotIn('href="#part-', content)
        self.assertNotIn("Read on<", content)

    def test_text_fields_are_escaped(self):
        attack = '<script>alert(1)</script><img src=x onerror=alert(1)>'
        parts = reader_parts(self.payload)
        parts[0]["title_en"] = attack
        parts[0]["sentences"][0] = {"id": "gloss-1-1", "en": attack, "ru": attack}
        parts[0]["fork"]["prompt"]["en"] = attack
        parts[0]["fork"]["options"][0].update(label_en=attack, en=attack, ru=attack)
        terms = [{"en": attack, "ru": attack, "note": attack}]
        about = {"byline_en": attack, "source_en": attack, "note_ru": attack}
        with patch("apps.library.views.reader_parts", return_value=parts), patch("apps.library.views.reader_terms", return_value=terms), patch(
            "apps.library.views.about_text", return_value=about
        ), patch("apps.library.views.titles", return_value={"title_en": attack, "title_ru": attack, "title_lang": "en"}):
            raw = self.client.get(READ_URL).content.decode("utf-8")
        self.assertNotIn("<script>alert(1)", raw)
        self.assertNotIn("<img src=x", raw)
        self.assertGreaterEqual(raw.count("&lt;script&gt;alert(1)&lt;/script&gt;"), 12)

    def test_older_russian_title_is_marked_as_russian(self):
        publish_demo_fixture()
        self.assertRegex(page(self.client.get("/read/socrates-questions/")), r'<h1[^>]*\blang="ru"')
        self.assertNotRegex(page(self.client.get(READ_URL)), r'<h1[^>]*\blang="ru"')

    def test_missing_page_is_part_of_the_site(self):
        response = self.client.get("/read/no-such-text/")
        self.assertEqual(response.status_code, 404)
        content = page(response)
        self.assertIn("This page is not here.", content)
        self.assertRegex(content, r'<html[^>]*\blang="en"')
        self.assertIn('data-cover="clay"', content)
        self.assertIn('href="/"', content)

    def test_guest_reading_makes_no_writes_and_no_ai_queries(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        for url in ("/", "/library/", READ_URL):
            with self.subTest(url=url), CaptureQueriesContext(connection) as queries:
                self.assertEqual(self.client.get(url).status_code, 200)
            statements = [q["sql"] for q in queries.captured_queries]
            for sql in statements:
                self.assertTrue(sql.lstrip().upper().startswith("SELECT"), sql)
                for table in ("ai_aijob", "ai_providerattempt", "credits_"):
                    self.assertNotIn(table, sql)
            self.assertLessEqual(len(statements), 4)


# --- presenters ------------------------------------------------------------------------------


class PresenterTests(SimpleTestCase):
    def payload(self):
        return text_data()["payload"]

    def test_fork_is_passed_through_word_for_word(self):
        parts = reader_parts(self.payload())
        source = self.payload()["parts"][0]["fork"]
        fork = parts[0]["fork"]
        self.assertEqual(fork["title_en"], source["title_en"])
        self.assertEqual((fork["prompt"]["en"], fork["prompt"]["ru"]), (source["prompt"]["en"], source["prompt"]["ru"]))
        self.assertEqual([(o["key"], o["label_en"], o["en"], o["ru"]) for o in fork["options"]], [(o["key"], o["label_en"], o["en"], o["ru"]) for o in source["options"]])
        self.assertEqual([o["id"] for o in fork["options"]], ["fork-1-yes", "fork-1-no", "fork-1-not-sure"])
        self.assertEqual(fork["next_part"], 2)

    def test_unusable_fork_is_dropped_not_guessed(self):
        base = {"title_en": "T", "sentences": [{"en": "One.", "ru": "Один."}]}
        good = {"prompt": {"en": "Q?", "ru": "В?"}, "options": [{"key": "yes", "label_en": "Yes", "en": "A.", "ru": "А."}]}
        for fork in ("text", [], {}, {"prompt": {"en": ""}, "options": good["options"]}, {"prompt": good["prompt"], "options": []}, {"prompt": good["prompt"], "options": [{"key": "maybe", "label_en": "Maybe", "en": "A.", "ru": "А."}]}):
            with self.subTest(fork=fork):
                self.assertIsNone(reader_parts({"parts": [{**base, "fork": fork}]})[0]["fork"])
        last = reader_parts({"parts": [{**base, "fork": good}]})[0]["fork"]
        self.assertIsNone(last["next_part"])  # a fork after the last part leads nowhere

    def test_sentences_are_one_per_line_in_the_stored_order(self):
        parts = reader_parts(self.payload())
        self.assertEqual([s["en"] for s in parts[1]["sentences"]], [s["en"] for s in self.payload()["parts"][1]["sentences"]])
        self.assertEqual(parts[0]["sentences"][0]["id"], "gloss-1-1")
        self.assertEqual(parts[1]["sentences"][10]["id"], "gloss-2-11")

    def test_cover_lines_are_the_first_two_sentences(self):
        lines = cover_lines(reader_parts(self.payload()))
        self.assertEqual([line["en"] for line in lines], ["Two men talk.", "Their names are Socrates and Laches."])
        self.assertEqual([line["id"] for line in lines], ["cover-line-1", "cover-line-2"])
        self.assertEqual(cover_lines(reader_parts({"parts": [{"sentences": [{"en": "Only.", "ru": "Одно."}]}]})), [{"en": "Only.", "ru": "Одно.", "id": "cover-line-1"}])
        self.assertEqual(cover_lines([]), [])

    def test_titles_fall_back_to_russian(self):
        self.assertEqual(titles(self.payload()), {"title_en": "What does it mean to be brave?", "title_ru": "Что значит быть смелым?", "title_lang": "en"})
        self.assertEqual(titles({"title_ru": "Вопросы Сократа"}), {"title_en": "Вопросы Сократа", "title_ru": "", "title_lang": "ru"})
        for odd in (None, [], "text", {"title_en": 5}):
            self.assertEqual(titles(odd), {"title_en": "", "title_ru": "", "title_lang": "en"})

    def test_odd_stored_payload_does_not_break_a_page(self):
        sentence = {"en": "One.", "ru": "Один."}
        option = {"key": "yes", "label_en": "Yes", "en": "A.", "ru": "А."}
        for payload in (
            None, [], "text", {"parts": 5}, {"parts": "abc"}, {"parts": [5, None]},
            {"parts": [{"sentences": 5}]}, {"parts": [{"sentences": [5, {"en": 7}]}]},
            {"parts": [{"sentences": [sentence], "fork": {"prompt": {"en": "Q?"}, "options": 5}}]},
            {"terms": 5, "glossary": "x"},
        ):
            with self.subTest(payload=payload):
                parts = reader_parts(payload)
                self.assertEqual(cover_lines(parts)[:1], [{**sentence, "id": "cover-line-1"}] if parts and parts[0]["sentences"] else [])
                self.assertEqual(reader_terms(payload), [])
                self.assertEqual(about_text(payload), {"byline_en": "", "source_en": "", "note_ru": ""})
        twice = reader_parts({"parts": [{"sentences": [sentence], "fork": {"prompt": {"en": "Q?"}, "options": [option, {**option, "en": "B."}]}}]})
        self.assertEqual([o["id"] for o in twice[0]["fork"]["options"]], ["fork-1-yes"])  # ids stay unique

    def test_about_block_states_only_what_the_text_says(self):
        about = about_text(self.payload())
        self.assertEqual(about["byline_en"], "Written and checked by AI.")
        self.assertEqual(about["source_en"], "Plato, Laches")
        self.assertEqual(about["note_ru"], self.payload()["simplification_note_ru"])
        self.assertEqual(about_text({"title_ru": "Старый текст"}), {"byline_en": "", "source_en": "", "note_ru": ""})
        self.assertEqual(about_text({"authorship": "human"})["byline_en"], "")


class SourceContractTests(SimpleTestCase):
    """What a browser test would see, checked on the source: the suite has
    no browser, and the server has no Node."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from django.conf import settings

        base = settings.BASE_DIR
        cls.script = (base / "static" / "js" / "reader.js").read_text(encoding="utf-8")
        cls.styles = (base / "static" / "css" / "main.css").read_text(encoding="utf-8")
        cls.templates = {path: path.read_text(encoding="utf-8") for path in (base / "templates").rglob("*.html")}

    def test_script_sends_and_stores_nothing_but_the_caption_switch(self):
        for call in ("fetch(", "XMLHttpRequest", "sendBeacon", "WebSocket", "EventSource", "document.cookie", "sessionStorage", "indexedDB", "import("):
            self.assertNotIn(call, self.script, call)
        self.assertEqual(self.script.count("localStorage"), 5)  # caption pref (read+write) and fork choices (read+write)
        self.assertEqual(self.script.count("RU_KEY"), 3)  # declared, read, written
        self.assertIn("var RU_KEY = 'pr-ui-ru';", self.script)
        # The fork keeps its choices on this device (localStorage) and never
        # sends them: no network, no forms, no scoring.
        fork = self.script[self.script.index("function initForks"):self.script.index("function init()")]
        for call in ("writePref", "location", "submit", "RU_KEY", "fetch(", "XMLHttpRequest", "sendBeacon"):
            self.assertNotIn(call, fork, call)

    def test_nothing_is_loaded_from_another_host(self):
        for path, source in self.templates.items():
            for match in re.findall(r'(?:src|href)="(https?://[^"]*)"', source):
                self.fail(f"{path.name}: {match}")
            self.assertEqual(len(re.findall(r"<script\b[^>]*\bsrc=", source)), 1 if path.name == "base.html" else 0, path.name)
        self.assertNotRegex(self.styles, r"url\(\s*['\"]?(https?:)?//")
        self.assertNotIn("@import", self.styles)

    def test_template_comments_stay_on_one_line(self):
        # Django's {# #} comment ends at the line break; a longer one is printed on the page.
        for path, source in self.templates.items():
            for line in source.splitlines():
                self.assertEqual(line.count("{#"), line.count("#}"), f"{path.name}: {line.strip()[:60]}")

    def test_templates_never_switch_escaping_off(self):
        for path, source in self.templates.items():
            for marker in ("|safe", "autoescape off", "mark_safe"):
                self.assertNotIn(marker, source, f"{path.name}: {marker}")

    def test_page_returns_to_plain_reading_unless_the_script_reports_in(self):
        bases = [source for path, source in self.templates.items() if path.name in ("base.html", "base_reading.html")]
        self.assertEqual(len(bases), 2)
        for source in bases:
            self.assertIn("hasAttribute('data-js-ready')", source)
            self.assertIn("DOMContentLoaded", source)
            self.assertIn("setTimeout(back,3000)", source)
            self.assertNotIn("filename", source)  # the file name is hashed in production
        self.assertIn("onerror=\"document.documentElement.classList.remove('js','ui-ru-off')\"", "".join(bases))
        ready = self.script.index("root.setAttribute('data-js-ready', '')")
        self.assertLess(self.script.index("initForks();"), ready)  # only after every step worked
        self.assertIn("root.removeAttribute('data-js-ready')", self.script)

    def test_translations_and_fork_lines_hide_only_with_working_script(self):
        hiding = 0
        for selector in (".gloss", ".choice__option"):
            # Top-level rules only; the print rules sit inside @media and are indented.
            for rule in re.findall(r"(?m)^([^\s{}][^{}\n]*" + re.escape(selector) + r"\b[^{}\n]*)\{[^}]*display:\s*none", self.styles):
                self.assertIn(".js ", rule, rule)
                hiding += 1
        self.assertEqual(hiding, 2)
        self.assertNotRegex(self.styles, r"(?m)^\.s\s*\{[^}]*text-decoration:\s*underline")
        self.assertNotRegex(self.styles, r"(?m)^\.js \.s(\[data-gloss\])?\s*\{[^}]*text-decoration:\s*underline")

    def test_only_the_closed_palette_and_local_fonts(self):
        for tone in ("--cover-clay: #8a3a26", "--cover-sea: #1f4d5c", "--cover-olive: #4a5626"):
            self.assertIn(tone, self.styles)
        for gone in ("--rubric", "--night"):
            self.assertNotIn(gone, self.styles)
        families = set(re.findall(r'font-family:\s*"([^"]+)"', (self.templates and __import__("django.conf").conf.settings.BASE_DIR / "static" / "css" / "fonts.css").read_text(encoding="utf-8")))
        self.assertEqual(families, {"Literata", "Golos Text"})


class FirstTextFileStillPinnedTests(SimpleTestCase):
    def test_tone_lives_outside_the_payload(self):
        self.assertNotIn("cover", text_data()["payload"])
        self.assertTrue(TEXT_FILE.is_file())


# --- pages around the reading path -----------------------------------------------------------


class ServicePagesTests(ReadingBase):
    def setUp(self):
        self.user = User.objects.create_user(login="reader", password="TestPassword123!")

    def test_workshop_form_moved_from_the_home_page_to_its_own_page(self):
        self.assertEqual(self.client.get("/projects/new/").status_code, 302)  # guests are sent to sign in
        self.client.force_login(self.user)
        put_book_on_shelf()  # the form is offered only when there is a book to ask about
        response = self.client.get("/projects/new/")
        content = page(response)
        self.assertRegex(content, r'<html[^>]*\blang="ru"')
        self.assertIn('data-cover="clay"', content)
        self.assertNotIn("data-ru-toggle", content)
        self.assertIn("Ask about a book on our shelf.", content)
        self.assertIn("Ваш вопрос о книге", content)
        self.assertIn('name="topic"', content)
        self.assertIn("csrfmiddlewaretoken", content)
        self.assertIn(f"<strong>{response.context['costs']['generate']}</strong>", content)
        self.assertIn('href="/projects/new/"', page(self.client.get("/account/")))  # the «Мастерская» link

    def test_workshop_errors_keep_the_page_whole(self):
        self.client.force_login(self.user)
        response = self.client.post("/projects/new/", {"mode": "explain_topic", "topic": ""})
        self.assertEqual(response.status_code, 200)
        self.assertIn("Введите вопрос о книге", page(response))
        self.assertIn("costs", response.context)

    def test_favourites_list_only_texts_that_still_open(self):
        from apps.library.models import Favorite

        self.client.force_login(self.user)
        Favorite.objects.create(user=self.user, publication=self.publication)
        self.assertIn(f'href="{READ_URL}"', page(self.client.get("/account/favorites/")))
        Publication.objects.filter(pk=self.publication.pk).update(status=Publication.Status.WITHDRAWN)
        self.assertNotIn(f'href="{READ_URL}"', page(self.client.get("/account/favorites/")))
        self.assertEqual(self.client.get(READ_URL).status_code, 404)
