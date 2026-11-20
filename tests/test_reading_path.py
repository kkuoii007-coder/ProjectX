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
from apps.library.presenters import about_text, cover_lines, fork_scope, reader_parts, reader_terms, titles
from apps.library.services import publish_revision
from apps.operations.models import AuditEvent

from .helpers import publish_demo_fixture
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

    def test_fork_hint_waits_for_the_script_and_the_chain_is_marked(self):
        content = page(self.client.get(READ_URL))
        self.assertIn("Choose an answer to read on. Any answer is fine.", content)
        self.assertIn("Выберите ответ, чтобы читать дальше", content)
        # The hint speaks of the buttons, so it is shown by the script with them.
        self.assertEqual(content.count('<p class="choice__hint" data-fork-hint hidden>'), 3)
        self.assertEqual(content.count("data-fork-hint"), 3)
        # The script reads the chain from the page: parts in order, a fork after each.
        self.assertEqual(re.findall(r'<section class="part" id="part-(\d)" data-part="(\d)"', content), [(n, n) for n in "1234"])
        self.assertEqual(content.count("<section class=\"choice\" data-fork "), 3)
        self.assertEqual(content.count('data-fork-key="yes"'), 3)
        # The old marks, one fork opening one part by itself, are gone.
        for gone in ("data-fork-id", "data-fork-next"):
            self.assertNotIn(gone, content)

    def test_reading_place_is_named_by_the_text_and_its_revision(self):
        content = page(self.client.get(READ_URL))
        scope = f"{SLUG}:{self.revision.content_hash[:16]}"
        self.assertIn(f'<article class="reader" data-reader data-fork-scope="{scope}">', content)
        self.assertEqual(content.count("data-fork-scope"), 1)
        self.assertEqual(len(self.revision.content_hash), 64)
        # No database ids in the name.
        self.assertNotIn(f'data-fork-scope="{SLUG}:{self.revision.pk}"', content)

    def test_new_revision_of_the_text_gets_a_new_reading_place(self):
        before = self.client.get(READ_URL).context["fork_scope"]
        data = text_data()
        data["payload"]["parts"][0]["sentences"][0]["ru"] = "Двое мужчин разговаривают."
        revision, _ = submit_text(data, actor="test")
        self.assertNotEqual(revision.pk, self.revision.pk)
        run_review(revision)
        publication, _ = publish_revision(revision=revision, admin=self.admin, slug=SLUG)
        self.assertEqual(publication.pk, self.publication.pk)  # the same text at the same address
        after = self.client.get(READ_URL).context["fork_scope"]
        self.assertEqual(after, f"{SLUG}:{revision.content_hash[:16]}")
        self.assertNotEqual(after, before)
        self.assertEqual(after.rsplit(":", 1)[0], before.rsplit(":", 1)[0])  # the script finds the old place by the address

    def test_another_text_has_its_own_reading_place_or_none(self):
        other = publish_demo_fixture()
        response = self.client.get(f"/read/{other.slug}/")
        scope = response.context["fork_scope"]
        # The demo text has no forks: it is never closed and has no place to keep.
        self.assertEqual(scope, "")
        self.assertNotIn("data-fork-scope", page(response))
        self.assertNotIn("data-fork-hint", page(response))
        self.assertEqual(fork_scope(other, [{"fork": {"prompt": {}}}]), f"{other.slug}:{other.revision.content_hash[:16]}")
        self.assertNotEqual(fork_scope(other, [{"fork": {}}, {"fork": {"prompt": {}}}]), self.client.get(READ_URL).context["fork_scope"])

    def test_without_script_the_whole_text_is_in_the_page_and_nothing_is_closed(self):
        content = page(self.client.get(READ_URL))
        article = content[content.index("<article"):content.index("</article>")]
        blocks = re.findall(r"<section\b[^>]*>", article)
        kinds = [re.search(r'class="([^"]*)"', tag).group(1) for tag in blocks]
        self.assertEqual(kinds, ["part", "choice", "part", "choice", "part", "choice", "part", "end-block terms", "end-block about", "end-block next"])
        for tag in blocks:
            for closing in (" hidden", "aria-hidden", "style=", "is-", "inert"):
                self.assertNotIn(closing, tag, tag)
        # Every sentence of every part and every line of every answer is there.
        for part in self.payload["parts"]:
            for sentence in part["sentences"]:
                self.assertIn(sentence["en"], article)
            for option in (part.get("fork") or {}).get("options", []):
                self.assertIn(option["en"], article)
        self.assertEqual(len(re.findall(r'<div class="choice__option" id="fork-\d-[a-z-]+-line" tabindex="-1">', article)), 9)
        # Only the script's own controls wait for it: the buttons and the hint about them.
        waiting = re.findall(r"<[^>]*\shidden\b[^>]*>", article)
        self.assertEqual(len(waiting), 6)
        for tag in waiting:
            self.assertTrue("data-fork-buttons" in tag or "data-fork-hint" in tag, tag)
        self.assertNotIn("is-reached", content)
        self.assertNotIn("is-locked", content)

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
        self.assertNotIn("data-fork-hint", content)  # nothing to read on: no hint about it

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

    def fork_script(self):
        return self.script[self.script.index("var FORK_PREFIX"):self.script.index("function initTopicExamples")]

    def function(self, name):
        """Source of one function of the fork script, to the next one at the same depth."""
        source = self.fork_script()
        start = source.index(f"function {name}(")
        indent = start - (source.rfind("\n", 0, start) + 1)
        return source[start:source.index("\n" + " " * indent + "}", start)]

    def test_script_sends_and_stores_nothing_but_the_caption_switch_and_the_reading_place(self):
        for call in ("fetch(", "XMLHttpRequest", "sendBeacon", "WebSocket", "EventSource", "document.cookie", "sessionStorage", "indexedDB", "import("):
            self.assertNotIn(call, self.script, call)
        # The caption switch (read, write) and the reading place (read, write, clean-up).
        calls = re.findall(r"window\.localStorage\b", self.script)
        self.assertEqual(len(calls), 5)
        self.assertEqual(len(re.findall(r"\blocalStorage\b", re.sub(r"(?s)/\*.*?\*/|//[^\n]*", "", self.script))), 5)
        self.assertEqual(self.script.count("RU_KEY"), 3)  # declared, read, written
        self.assertIn("var RU_KEY = 'pr-ui-ru';", self.script)
        # The fork keeps the reading place on this device and never sends it:
        # no network, no forms, no scoring. It reads the address (#part-N)
        # and never changes it.
        fork = self.fork_script()
        for call in ("writePref", "submit", "RU_KEY", "fetch(", "XMLHttpRequest", "sendBeacon", "history.", "score", "count", "streak"):
            self.assertNotIn(call, fork, call)
        self.assertEqual(fork.count("location"), 1)
        self.assertIn("exec(window.location.hash || '')", fork)

    def test_reading_place_is_kept_for_one_text_and_one_revision(self):
        fork = self.fork_script()
        # The old key, one for every text, is named once and only to be removed.
        self.assertEqual(self.script.count("pr-fork-choices"), 1)
        self.assertIn("var FORK_RETIRED_KEY = 'pr-fork-choices';", fork)
        self.assertEqual(fork.count("FORK_RETIRED_KEY"), 2)
        self.assertIn("var stale = [FORK_RETIRED_KEY];", self.function("dropStaleForkStates"))
        self.assertIn("storage.removeItem(name)", self.function("dropStaleForkStates"))
        for call in ("getItem", "setItem"):
            self.assertNotIn(call, self.function("dropStaleForkStates"))
        # The working key is built from the scope the page gives: slug and revision.
        self.assertIn("var FORK_PREFIX = 'pr-fork:';", fork)
        self.assertIn("var scope = reader.getAttribute('data-fork-scope');", fork)
        self.assertIn("var name = FORK_PREFIX + scope;", fork)
        self.assertEqual(fork.count("getItem("), 1)
        self.assertEqual(fork.count("setItem("), 1)
        self.assertIn("window.localStorage.getItem(name)", self.function("readForkState"))
        self.assertIn("window.localStorage.setItem(name, JSON.stringify(state))", self.function("writeForkState"))
        self.assertEqual(fork.count("readForkState("), 2)  # declared, read in one place
        self.assertIn("var stored = readForkState(name);", self.function("catchUp"))
        self.assertEqual(fork.count("writeForkState("), 3)  # declared; written after an answer and after a link
        for step in ("choose", "openFromHash"):
            self.assertEqual(self.function(step).count("writeForkState(name, state);"), 1)
        # No storage (private mode): every access is guarded, the state lives in the page.
        for name in ("readForkState", "writeForkState", "dropStaleForkStates"):
            self.assertRegex(self.function(name), r"(?s)try \{.*localStorage.*\} catch \(error\)")

    def test_the_whole_page_is_drawn_from_one_state(self):
        fork = self.fork_script()
        self.assertEqual(fork.count("function render()"), 1)
        render = self.function("render")
        # Nothing outside render() opens or closes anything.
        for mark in ("is-reached", "is-passed", "is-chosen", "aria-pressed", ".hidden = passed"):
            self.assertIn(mark, render, mark)
            self.assertEqual(fork.count(mark), render.count(mark) + fork[:fork.index("function initForks")].count(mark), mark)
        self.assertNotIn("classList.add", fork)
        self.assertNotIn("classList.remove", fork)
        # A part is open by its place in the chain, never by itself.
        self.assertIn("part.classList.toggle('is-reached', index < state.upto)", render)
        self.assertIn("fork.el.classList.toggle('is-reached', fork.after <= state.upto)", render)
        self.assertIn("block.classList.toggle('is-reached', state.upto >= total)", render)
        # What is open stays open: the count of open parts only grows.
        self.assertEqual(len(re.findall(r"state\.upto\s*(?:=|\+=|-=)[^=]", fork)), 3)
        self.assertIn("state.upto += 1;", self.function("advance"))
        self.assertIn("if (upto >= 1) { state.upto = Math.max(state.upto, Math.min(upto, total)); }", self.function("catchUp"))
        self.assertIn("var wasClosed = index >= state.upto;", self.function("openFromHash"))  # a link back closes nothing
        self.assertRegex(self.function("openFromHash"), r"if \(wasClosed\) \{\s*state\.upto = index \+ 1;")
        for old in ("is-locked", "has-choice", "data-fork-next", "data-fork-id"):
            self.assertNotIn(old, self.script, old)
            self.assertNotIn(old, self.styles, old)

    def test_a_link_to_a_part_opens_the_text_up_to_it(self):
        fork = self.fork_script()
        self.assertIn("window.addEventListener('hashchange', function () { openFromHash(false); });", fork)
        self.assertRegex(fork, r"refresh\(\);\s*openFromHash\(arrivedByLink\(\)\);")  # on load too
        opening = self.function("openFromHash")
        self.assertIn("/^#part-(\\d+)$/", opening)
        self.assertLess(opening.index("render();"), opening.index("target.scrollIntoView("))  # scroll once the part is open
        # The link answers nothing for the reader.
        self.assertNotIn("answers", opening)
        self.assertNotIn("choose(", opening)

    def test_a_page_never_writes_from_its_memory_alone(self):
        # The same text can be open in two tabs, or come back from the browser's
        # "back" cache: a page that is behind must not throw away what is kept.
        # The behaviour itself is checked in a browser (tools/qa/fork_steps.js,
        # steps 12 and 13, the `browser` job); here its wiring is pinned.
        fork = self.fork_script()
        catch_up = self.function("catchUp")
        self.assertNotIn("writeForkState", catch_up)  # catching up writes nothing
        self.assertNotRegex(catch_up, r"state\s*=")  # it adds to the state of the page, never replaces it
        for step in ("choose", "openFromHash"):
            body = self.function(step)
            self.assertEqual(body.count("catchUp();"), 1, step)
            self.assertLess(body.index("catchUp();"), body.index("writeForkState(name, state);"), step)
        choose = self.function("choose")
        # The kept answers first, then the answer of this press on top of them.
        self.assertLess(choose.index("catchUp();"), choose.index("state.answers[fork.id] = button.getAttribute('data-fork-key');"))
        self.assertRegex(self.function("refresh"), r"catchUp\(\);\s*render\(\);")
        self.assertNotIn("writeForkState", self.function("refresh"))
        self.assertIn("window.addEventListener('storage', function (event) { if (event.key === name) { refresh(); } });", fork)
        self.assertIn("window.addEventListener('pageshow', function (event) { if (event.persisted) { refresh(); } });", fork)

    def test_the_rules_of_the_chain_are_in_place(self):
        # Each line here is one rule of the fork. Removing any of them is seen
        # in a browser (fork_steps.js); these pins make the plain test suite
        # notice it too.
        advance = self.function("advance")
        self.assertIn("if (fork && !state.answers[fork.id]) { break; }", advance)  # no answer: the next part stays closed
        catch_up = self.function("catchUp")
        # Only an answer this page offers is taken from what is kept.
        self.assertIn("var known = fork.buttons.some(function (button) { return button.getAttribute('data-fork-key') === answers[fork.id]; });", catch_up)
        self.assertIn("if (known) { state.answers[fork.id] = answers[fork.id]; }", catch_up)
        self.assertEqual(catch_up.count("state.answers[fork.id] ="), 1)
        # An answer to a fork that is not reached is dropped, after the chain is walked.
        drop = "forks.forEach(function (fork) { if (fork.after > state.upto) { delete state.answers[fork.id]; } });"
        self.assertIn(drop, catch_up)
        self.assertLess(catch_up.index("advance();"), catch_up.index(drop))
        # Other revisions of this text are cleaned, other texts are left alone.
        stale = self.function("dropStaleForkStates")
        self.assertIn("var sameText = FORK_PREFIX + scope.slice(0, scope.lastIndexOf(':') + 1);", stale)
        self.assertIn("if (name && name.indexOf(sameText) === 0 && name !== FORK_PREFIX + scope) { stale.push(name); }", stale)
        self.assertIn("stale.forEach(function (name) { storage.removeItem(name); });", stale)
        self.assertNotIn("clear(", stale)
        self.assertRegex(self.fork_script(), r"dropStaleForkStates\(scope\);\s*refresh\(\);")
        # After an answer the focus goes to its line; after a link, to the part.
        self.assertRegex(self.function("choose"), r"render\(\);[^}]*var line = doc\.getElementById\(button\.getAttribute\('data-fork-choice'\)\);\s*if \(line\) \{ line\.focus\(\); \}")
        self.assertIn("target.focus({ preventScroll: true });", self.function("openFromHash"))
        # The end of the text is what the page marks so, and nothing else.
        self.assertIn("var ends = reader.querySelectorAll('[data-reader-end]');", self.fork_script())

    def test_only_the_marked_end_blocks_wait_for_the_last_part(self):
        # "Words" and the reader's own tools are on the page from the start.
        read = next(source for path, source in self.templates.items() if path.name == "read.html" and path.parent.name == "library")
        marked = re.findall(r"<(\w+)\b([^>]*\bdata-reader-end\b[^>]*)>", read)
        self.assertEqual([re.search(r'class="([^"]*)"', attrs).group(1) for _, attrs in marked], ["end-block about", "end-block next"])
        for block in ("terms", "reader-tools"):
            tag = re.search(r"<section\b[^>]*\bclass=\"end-block " + block + r"\"[^>]*>", read).group(0)
            self.assertNotIn("data-reader-end", tag, block)
        # In the stylesheet nothing but print leaves these blocks out.
        outside_print = self.styles
        for block in self.media_blocks("print"):
            outside_print = outside_print.replace(block, "")
        for selector in re.findall(r"([^{}]+)\{[^{}]*display:\s*none", outside_print):
            for kept in (".terms", ".end-block", ".reader-tools", ".reader__end", ".about", ".next"):
                self.assertNotRegex(selector, re.escape(kept) + r"(?![\w-])", selector.strip())
        # On screen exactly three kinds of things are closed until reached.
        screen = [block for block in self.media_blocks("screen") if "data-fork-scope" in block][0]
        closed = re.search(r"([^{}]*:not\(\.is-reached\)[^{}]*)\{\s*display:\s*none;\s*\}", screen).group(1)
        self.assertEqual(
            [selector.strip() for selector in closed.split(",")],
            [
                ".js [data-fork-scope] .part ~ .part:not(.is-reached)",
                ".js [data-fork-scope] .choice ~ .choice:not(.is-reached)",
                ".js [data-fork-scope] [data-reader-end]:not(.is-reached)",
            ],
        )
        self.assertEqual(self.styles.count("is-reached"), 3)

    def test_the_fork_hint_takes_the_colour_of_the_cover_text(self):
        # The hint stands on the cover tone. Any colour of its own (it was the
        # grey --ink-3: 1.3:1 on clay) is a contrast failure; the measured
        # ratios are in fork_steps.js, step 8.
        rules = re.findall(r"([^{}]+)\{([^{}]*)\}", re.sub(r"(?s)/\*.*?\*/", "", self.styles))
        hint = [(selector.strip(), body) for selector, body in rules if "choice__hint" in selector or "data-fork-hint" in selector]
        self.assertGreaterEqual(len(hint), 2)
        for selector, body in hint:
            for declaration in ("color", "opacity", "filter", "mix-blend-mode"):
                self.assertNotRegex(body, r"(?<![\w-])" + declaration + r"\s*:", f"{selector}: {declaration}")
            self.assertNotIn("--ink-3", body, selector)
        # The cover gives its text colour to everything inside the fork.
        choice = "".join(body for selector, body in rules if selector.strip() == ".choice")
        self.assertRegex(choice, r"(?<![\w-])color:\s*var\(--on-cover\)")
        self.assertIn("--on-cover: var(--paper);", self.styles)
        self.assertIn("--paper: #f7f5ef;", self.styles)

    def media_blocks(self, kind):
        blocks = []
        for match in re.finditer(r"@media " + kind + r"\s*\{", self.styles):
            depth, index = 1, match.end()
            while depth:
                depth += {"{": 1, "}": -1}.get(self.styles[index], 0)
                index += 1
            blocks.append(self.styles[match.end():index - 1])
        return blocks

    def test_closed_parts_are_closed_on_screen_only_and_printed_in_full(self):
        closing = r"([^{}]*(?:is-reached|\.choice__option|\.choice__label|\.choice__next)[^{}]*)\{[^{}]*display:\s*none"
        screen = [block for block in self.media_blocks("screen") if "data-fork-scope" in block]
        self.assertEqual(len(screen), 1)
        inside = re.findall(closing, screen[0])
        self.assertEqual(len(inside), 3)
        prints = "".join(self.media_blocks("print"))
        # Outside that block nothing closes a part, a fork or an answer line,
        # but print, which leaves out the "Read on" button.
        outside = self.styles.replace(screen[0], "")
        self.assertEqual([rule.strip() for rule in re.findall(closing, outside)], [rule.strip() for rule in re.findall(closing, prints)])
        self.assertIn(".js [data-fork-scope] .part ~ .part:not(.is-reached)", screen[0])
        self.assertIn(".js [data-fork-scope] .choice ~ .choice:not(.is-reached)", screen[0])
        self.assertIn(".js [data-fork-scope] [data-reader-end]:not(.is-reached)", screen[0])
        for selector in re.findall(r"(?m)^\s*([.\[][^{},]*)[,{]", "".join(f"{rule}{{" for rule in inside)):
            self.assertTrue(selector.startswith(".js "), selector)  # only under a working script
        # Print: controls are left out; no part, fork, answer line or end block is.
        hidden = re.search(r"([^{}]*\.choice__buttons[^{}]*)\{\s*display:\s*none\s*!important", prints).group(1)
        for selector in (".choice__buttons", ".choice__hint", ".choice__next"):
            self.assertIn(selector, hidden)
        for selector in re.findall(r"([^{}]+)\{[^{}]*display:\s*none", prints):
            for kept in (".part", ".choice__option", ".choice__label", ".choice__options", "data-reader-end", ".end-block", ".about", ".next", ".terms"):
                self.assertNotRegex(selector, re.escape(kept) + r"(?![\w-])", selector.strip())
            self.assertNotRegex(selector, r"\.choice(?![\w-])")

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

    def test_a_late_or_broken_script_closes_nothing(self):
        # Browser checks: fork_steps.js, steps 14 (late) and 15 (broken).
        bases = [source for path, source in self.templates.items() if path.name in ("base.html", "base_reading.html")]
        for source in bases:
            # Giving the page back is remembered on the page itself.
            self.assertIn("function back(){if(!r.hasAttribute('data-js-ready')){r.classList.remove('js','ui-ru-off');r.setAttribute('data-js-off','')}}", source)
        start = self.script.index("  function init() {")
        init = self.script[start:self.script.index("\n  }\n", start)]
        # A script that comes after that does nothing at all: the reader may be
        # in the middle of the whole text by now.
        guard = "if (root.hasAttribute('data-js-off')) { return; }"
        self.assertIn(guard, init)
        self.assertLess(init.index(guard), init.index("try {"))
        for step in ("initGlosses();", "initRuToggle();", "initMenu();", "initForks();", "root.classList.add('js');"):
            self.assertLess(init.index("try {"), init.index(step), step)
            self.assertLess(init.index(step), init.index("} catch (error) {"), step)
        # The js class is set in one place only, when every step has worked.
        self.assertEqual(self.script.count("classList.add('js')"), 1)
        self.assertNotIn("data-js-off", self.script.replace(guard, ""))  # the script never takes the mark back
        # An exception in any step: the js class goes, with everything it hides.
        handler = init[init.index("} catch (error) {"):]
        self.assertIn("plainPage();", handler)
        self.assertNotIn("classList.add", handler)
        start = self.script.index("  function plainPage() {")
        plain = self.script[start:self.script.index("\n  }\n", start)]
        for line in ("root.removeAttribute('data-js-ready');", "root.classList.remove('js');", "root.classList.remove('ui-ru-off');"):
            self.assertIn(line, plain)
            self.assertLess(plain.index(line), plain.index("try {"))  # before anything that could fail
        # Controls shown by the script go away with the js class.
        self.assertRegex(
            self.styles,
            r"html:not\(\.js\) \[data-fork-buttons\],\s*html:not\(\.js\) \[data-fork-hint\],\s*html:not\(\.js\) \[data-ru-toggle\] \{\s*display: none;\s*\}",
        )

    def test_translations_and_fork_lines_hide_only_with_working_script(self):
        hiding = 0
        screen = "".join(self.media_blocks("screen"))
        for selector, source in ((".gloss", self.styles), (".choice__option", screen)):
            # Top-level rules for the translation; the fork lines close inside
            # @media screen. The print rules sit inside @media print.
            lead = "" if source is self.styles else r"\s*"
            for rule in re.findall(r"(?m)^" + lead + r"([^\s{}][^{}\n]*" + re.escape(selector) + r"\b[^{}\n]*)\{[^}]*display:\s*none", source):
                self.assertIn(".js ", rule, rule)
                hiding += 1
        self.assertEqual(hiding, 2)
        self.assertNotRegex(self.styles, r"(?m)^[^\s{}][^{}\n]*\.choice__option\b[^{}\n]*\{[^}]*display:\s*none")
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
        response = self.client.get("/projects/new/")
        content = page(response)
        self.assertRegex(content, r'<html[^>]*\blang="ru"')
        self.assertIn('data-cover="clay"', content)
        self.assertNotIn("data-ru-toggle", content)
        self.assertIn("О какой философской идее вы хотите прочитать?", content)
        self.assertIn('name="topic"', content)
        self.assertIn("csrfmiddlewaretoken", content)
        self.assertIn(f"<strong>{response.context['costs']['generate']}</strong>", content)
        self.assertIn('href="/projects/new/"', page(self.client.get("/account/")))  # the «Мастерская» link

    def test_workshop_errors_keep_the_page_whole(self):
        self.client.force_login(self.user)
        response = self.client.post("/projects/new/", {"mode": "explain_topic", "topic": ""})
        self.assertEqual(response.status_code, 200)
        self.assertIn("Введите тему запроса", page(response))
        self.assertIn("costs", response.context)

    def test_favourites_list_only_texts_that_still_open(self):
        from apps.library.models import Favorite

        self.client.force_login(self.user)
        Favorite.objects.create(user=self.user, publication=self.publication)
        self.assertIn(f'href="{READ_URL}"', page(self.client.get("/account/favorites/")))
        Publication.objects.filter(pk=self.publication.pk).update(status=Publication.Status.WITHDRAWN)
        self.assertNotIn(f'href="{READ_URL}"', page(self.client.get("/account/favorites/")))
        self.assertEqual(self.client.get(READ_URL).status_code, 404)
