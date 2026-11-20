"""Vocabulary gates on fork lines and the capital-letter rule (20 Nov 2026).

The question and the three answer lines of every fork go through the same
three gates as the sentences of the parts. A capital letter marks a name
only inside a sentence: after a colon and after an opening quote the word
is checked on the list.
"""

import copy
import json
from io import StringIO
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.test import SimpleTestCase

from apps.ai.services import canonical_hash
from apps.library.editorial import (
    LONG_SENTENCE_WORDS,
    check_editorial_payload,
    measured_sentences,
    profile_vocabulary,
    vocabulary_errors,
)

from .test_editorial_path import FIRST_TEXT_HASH, EditorialBase, text_data

TEXTS = Path(settings.BASE_DIR) / "content" / "texts"
# The published payloads: a change needs a new paid review, so it must be deliberate.
PUBLISHED_HASHES = {
    "what-does-it-mean-to-be-brave": FIRST_TEXT_HASH,
    "why-do-people-live-in-cities": "d533bf30503d99187cacbd308973efbcb09380f198d3e6d63096c7fc2904072f",
}


def one_line(text):
    """A minimal payload with one sentence, to look at single words."""
    return {"glossary": [], "parts": [{"sentences": [{"en": text, "ru": "—"}]}]}


def above_a1(text):
    return profile_vocabulary(one_line(text))["above_a1"]


class PublishedFilesTests(SimpleTestCase):
    def test_every_text_file_is_pinned_and_passes_the_gates(self):
        files = sorted(TEXTS.glob("*.json"))
        self.assertEqual([path.stem for path in files], sorted(PUBLISHED_HASHES))
        for path in files:
            with self.subTest(text=path.stem):
                payload = json.loads(path.read_text(encoding="utf-8"))["payload"]
                self.assertEqual(canonical_hash(payload), PUBLISHED_HASHES[path.stem])
                self.assertEqual(check_editorial_payload(payload)[0], [])
                self.assertEqual(vocabulary_errors(payload), [])
                profile = profile_vocabulary(payload)
                self.assertGreater(profile["fork_words"], 100)  # three forks are counted
                self.assertGreaterEqual(profile["share"]["A1"], 90.0)

    def test_command_reports_forks_and_the_verdict(self):
        for path in sorted(TEXTS.glob("*.json")):
            with self.subTest(text=path.stem):
                out = StringIO()
                call_command("vocabulary_profile", str(path), stdout=out)
                self.assertIn("в частях и развилках", out.getvalue())
                self.assertIn("Итог: словарная проверка пройдена", out.getvalue())

    def test_command_fails_when_a_gate_refuses(self):
        import tempfile

        data = text_data()
        data["payload"]["parts"][0]["fork"]["prompt"]["en"] = "Do they abandon the men?"
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "text.json"
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            out = StringIO()
            with self.assertRaises(SystemExit) as ctx:
                call_command("vocabulary_profile", str(path), stdout=out)
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("Итог: подача будет отклонена", out.getvalue())
        self.assertIn("abandon", out.getvalue())


class ForkLinesTests(SimpleTestCase):
    def setUp(self):
        self.payload = text_data()["payload"]
        self.fork = self.payload["parts"][0]["fork"]

    def test_fork_lines_are_counted(self):
        without = copy.deepcopy(self.payload)
        for part in without["parts"]:
            part.pop("fork", None)
        full, bare = profile_vocabulary(self.payload), profile_vocabulary(without)
        self.assertEqual(bare["fork_words"], 0)
        self.assertEqual(full["total_words"], bare["total_words"] + full["fork_words"])
        # The fixed button words are interface, not text.
        lines = [self.fork["prompt"]["en"]] + [option["en"] for option in self.fork["options"]]
        one_fork = copy.deepcopy(without)
        one_fork["parts"][0]["fork"] = self.fork
        expected = sum(len(line.split()) for line in lines)
        self.assertEqual(profile_vocabulary(one_fork)["fork_words"], expected)

    def test_easy_fork_lines_do_not_pull_a_hard_text_over_the_gate(self):
        # Ten B1 words in the parts: under 90 % there, above 90 % with the forks.
        self.payload["parts"][3]["sentences"] = [
            {"en": "They abandon every answer and abandon the easy way.", "ru": "—"},
            {"en": "They abandon the fear and abandon the doubt.", "ru": "—"},
            {"en": "They abandon the old words and abandon the new.", "ru": "—"},
            {"en": "They abandon the road and abandon the field.", "ru": "—"},
            {"en": "They abandon the house and abandon the town.", "ru": "—"},
        ]
        self.payload["glossary"].append({"term_en": "abandon", "explanation_en": "To leave without help.", "translation_ru": "оставлять"})
        profile = profile_vocabulary(self.payload)
        self.assertLess(profile["parts_a1_share"], 90.0)
        self.assertGreaterEqual(profile["share"]["A1"], 90.0)
        errors = vocabulary_errors(self.payload)
        self.assertTrue(any("слов уровня A1 в частях" in error for error in errors), errors)

    def test_hard_fork_lines_are_refused_by_the_share(self):
        for part in self.payload["parts"][:3]:
            part["fork"]["prompt"]["en"] = "Do they abandon, abandon, abandon and abandon the men?"
            for option in part["fork"]["options"]:
                option["en"] = "You abandon it. They abandon it. We abandon it."
        self.payload["glossary"].append({"term_en": "abandon", "explanation_en": "To leave without help.", "translation_ru": "оставлять"})
        profile = profile_vocabulary(self.payload)
        self.assertGreaterEqual(profile["parts_a1_share"], 90.0)
        self.assertLess(profile["share"]["A1"], 90.0)
        errors = vocabulary_errors(self.payload)
        self.assertTrue(any("вместе с развилками" in error for error in errors), errors)

    def test_hard_word_in_the_question_is_refused(self):
        self.fork["prompt"]["en"] = "Do they abandon the men?"
        errors = vocabulary_errors(self.payload)
        self.assertTrue(any("выше A2 без словарика — abandon" in error for error in errors), errors)

    def test_hard_word_in_every_answer_line_is_refused(self):
        for index in range(3):
            with self.subTest(option=index):
                payload = copy.deepcopy(self.payload)
                payload["parts"][1]["fork"]["options"][index]["en"] = "You abandon the answer. Read on."
                errors = vocabulary_errors(payload)
                self.assertTrue(any("abandon" in error for error in errors), errors)

    def test_hard_word_in_a_fork_passes_with_the_glossary(self):
        self.fork["prompt"]["en"] = "Do they abandon the men?"
        self.payload["glossary"].append({"term_en": "abandon", "explanation_en": "To leave without help.", "translation_ru": "оставлять"})
        self.assertEqual(vocabulary_errors(self.payload), [])

    def test_fork_line_is_measured_sentence_by_sentence(self):
        # Three short sentences in one line: 18 words together, none above the limit.
        self.fork["prompt"]["en"] = "Think of a man who needs no other people. He lives alone. Is he like you and me?"
        self.assertGreater(len(self.fork["prompt"]["en"].split()), LONG_SENTENCE_WORDS)
        self.assertEqual(vocabulary_errors(self.payload), [])
        self.assertIn("He lives alone.", list(measured_sentences(self.payload)))

    def test_long_sentence_in_a_fork_is_refused(self):
        long_question = "Are the men who run away and then turn back and fight and win in the end brave?"
        self.assertGreater(len(long_question.split()), LONG_SENTENCE_WORDS)
        self.fork["prompt"]["en"] = long_question
        errors = vocabulary_errors(self.payload)
        self.assertTrue(any(f"предложение длиннее {LONG_SENTENCE_WORDS} слов" in error and "run away" in error for error in errors), errors)

    def test_a_mark_inside_a_fork_sentence_does_not_cut_it(self):
        for question in (
            "Is the man who runs away and then says “Why?” and turns back and fights and wins in the end brave?",
            "Is the man who runs away and then... turns back and fights and wins in the end still a brave man?",
            "Is the man who runs away and then… turns back and fights and wins in the end still a brave man?",
        ):
            with self.subTest(question=question):
                self.assertGreater(len(question.split()), LONG_SENTENCE_WORDS)
                payload = copy.deepcopy(self.payload)
                payload["parts"][0]["fork"]["prompt"]["en"] = question
                errors = vocabulary_errors(payload)
                self.assertTrue(any("предложение длиннее" in error for error in errors), errors)

    def test_quoted_sentences_in_a_fork_line_are_separate_sentences(self):
        line = "Two cities buy and sell. They say: “We will not hurt you.” Are they one city now?"
        self.fork["prompt"]["en"] = line
        self.assertEqual(vocabulary_errors(self.payload), [])
        self.assertIn("Are they one city now?", list(measured_sentences(self.payload)))

    def test_part_sentence_is_still_measured_whole(self):
        # An entry of a part is one sentence: two sentences in it do not get a longer limit.
        self.payload["parts"][3]["sentences"][0] = {"en": "He said one word and went home again. He said one more word and then went home.", "ru": "—"}
        errors = vocabulary_errors(self.payload)
        self.assertTrue(any("предложение длиннее" in error for error in errors), errors)


class MissingWordListTests(EditorialBase):
    def test_submission_is_refused_when_the_word_list_is_missing(self):
        from unittest.mock import patch

        from apps.library.editorial import EditorialError
        from apps.workshop.models import Revision

        before = Revision.objects.count()
        with patch("apps.library.editorial.load_cefr_list", side_effect=EditorialError("Список словаря не найден: x.csv")):
            with self.assertRaises(EditorialError) as ctx:
                self.submit()
        self.assertTrue(any("проверка невозможна" in message for message in ctx.exception.messages), ctx.exception.messages)
        self.assertEqual(Revision.objects.count(), before)


class CapitalLetterTests(SimpleTestCase):
    def test_capital_inside_a_sentence_is_a_name(self):
        self.assertEqual(above_a1("He reads Politics every day."), {})
        self.assertEqual(above_a1("It is Laches's first answer."), {})
        self.assertEqual(above_a1("The book (Politics) is old."), {})

    def test_word_after_a_colon_is_checked(self):
        self.assertEqual(above_a1("He says: Abandon the city.")["abandon"]["level"], "B1")

    def test_word_after_an_opening_quote_is_checked(self):
        for text in (
            "He says “Abandon the city” again.",
            "He says ‘Abandon the city’ again.",
            'He says "Abandon the city" again.',
            "He says 'Abandon the city' again.",
            "He says «Abandon the city» again.",
            "They say: “Abandon the city.”",
        ):
            with self.subTest(text=text):
                self.assertEqual(above_a1(text)["abandon"]["level"], "B1")

    def test_straight_quote_after_a_bracket_a_dash_or_a_comma_opens(self):
        for text in (
            'He says ("Abandon the city") again.',
            'He says —"Abandon the city" again.',
            'He says,"Abandon the city" again.',
            "He says „Abandon the city“ again.",
            "He says ‹Abandon the city› again.",
        ):
            with self.subTest(text=text):
                self.assertEqual(above_a1(text)["abandon"]["level"], "B1")

    def test_word_after_an_ellipsis_is_checked(self):
        self.assertEqual(above_a1("He waits… Abandon the city.")["abandon"]["level"], "B1")
        self.assertEqual(above_a1("He waits... Abandon the city.")["abandon"]["level"], "B1")

    def test_repeated_first_word_with_a_capital_is_still_a_word(self):
        # The check before this change looked such a word up; it must not get weaker.
        payload = one_line("Abandon the men and then Abandon the men, he says.")
        payload["glossary"].append({"term_en": "abandon", "explanation_en": "To leave without help.", "translation_ru": "оставлять"})
        self.assertEqual(profile_vocabulary(payload)["above_a1"]["abandon"]["count"], 2)
        self.assertTrue(any("слов уровня A1" in error for error in vocabulary_errors(payload)))

    def test_word_at_a_sentence_start_inside_a_line_is_checked(self):
        self.assertEqual(above_a1("Read on. Abandon the plan.")["abandon"]["level"], "B1")
        self.assertEqual(above_a1("Is it so? Abandon the plan.")["abandon"]["level"], "B1")

    def test_name_after_a_colon_or_a_quote_is_still_a_name(self):
        # The list does not know these words, so the capital means a name.
        self.assertEqual(above_a1("He asks: Laches, is it so?"), {})
        self.assertEqual(above_a1("He says: “Socrates is here.”"), {})

    def test_closing_quote_does_not_open_a_sentence(self):
        # After a closing quote the sentence goes on: Politics is the book here.
        self.assertEqual(above_a1("He calls it “the book” Politics in the end."), {})
        self.assertEqual(above_a1("He calls it 'the book' Politics in the end."), {})
        self.assertEqual(above_a1('He calls it "the book" Politics in the end.'), {})

    def test_published_texts_have_no_capital_word_that_was_hidden(self):
        # Every capitalised word after a colon or a quote in both texts is on the list at A1/A2 or glossed.
        for path in sorted(TEXTS.glob("*.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))["payload"]
            uncovered = [word for word, info in profile_vocabulary(payload)["above_a2"].items() if not info["in_glossary"]]
            self.assertEqual(uncovered, [], path.stem)
