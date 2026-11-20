"""The structural check after the backend review of PR 3: a quote stands
in the book as whole words, every kind of quotation mark is seen, the
Russian line, the titles and the glossary cannot carry an invented
quotation, and a value of the wrong kind is a violation, not an exception."""

import copy

from django.test import SimpleTestCase

from apps.ai.grounding import check_answer, find_verbatim, quoted_phrases

from .test_answer import BOOK, GOOD, OTHER, answer, fragment

REAL = "man is by nature a political animal"
INVENTED = "man is a wolf to man"


class WholeWordsTests(SimpleTestCase):
    def test_quote_starts_and_ends_at_the_border_of_a_word(self):
        for quote in (
            "an is by nature a political anim",  # cut at both ends
            "an is by nature a political animal",
            "man is by nature a political anim",
            "state is a natural growth",  # the book has «city-state»
            "the city is a natural growth",
        ):
            with self.subTest(quote=quote):
                self.assertIsNone(find_verbatim(BOOK, quote))
        self.assertIsNotNone(find_verbatim(BOOK, "the city-state is a natural growth"))
        self.assertIsNotNone(find_verbatim(BOOK, "clanless, lawless, hearthless"))  # after a quotation mark of the book
        self.assertIsNotNone(find_verbatim(BOOK, "reviled by Homer"))  # before a bracket
        self.assertIsNotNone(find_verbatim(BOOK, "for one by nature unsocial"))  # after a dash
        self.assertIsNone(find_verbatim("It is man's own speech.", "It is man"))
        self.assertIsNotNone(find_verbatim("The sons' sons live together.", "The sons"))
        # A later whole-word occurrence is found after a cut one.
        text = "The woman is by nature free. A man is by nature free."
        start, end = find_verbatim(text, "man is by nature free")
        self.assertEqual(start, text.index("man is by nature free", 10))

    def test_cut_quote_is_not_a_valid_source(self):
        supports = [fragment("S1", BOOK)]
        cut = {"id": "S1", "quote": "an is by nature a political anim"}
        checked = check_answer(answer([[cut], [GOOD], [GOOD]]), supports)
        self.assertFalse(checked.ok)
        self.assertIn("as whole words", checked.violations[0])
        # In quotation marks in the text: the same rule.
        checked = check_answer(answer([[GOOD]] * 3, extra_sentence="He says: “an is by nature a political anim”."), supports)
        self.assertFalse(checked.ok)


class QuotationMarksTests(SimpleTestCase):
    PAIRS = (
        ("“", "”"), ("“", '"'), ('"', "”"), ('"', '"'), ("‘", "'"), ("'", "’"), ("‘", "’"), ("'", "'"),
        ("»", "«"), ("«", "»"), ("‹", "›"), ("„", "“"), ("„", "”"), ("‟", "”"), ("`", "`"), ("`", "'"), ("``", "''"),
        ("＂", "＂"), ("＇", "＇"), ("「", "」"), ("『", "』"), ("« ", " »"),
    )

    def test_every_pair_of_marks_is_seen(self):
        for left, right in self.PAIRS:
            with self.subTest(marks=left + right):
                self.assertEqual(quoted_phrases(f"He says {left}{INVENTED}{right} and goes on."), [(INVENTED,)])
                self.assertEqual(quoted_phrases(f"{left}{INVENTED}{right}"), [(INVENTED,)])
                self.assertEqual(quoted_phrases(f"Он говорит: {left}человек человеку волк{right}."), [("человек человеку волк",)])

    def test_every_pair_is_checked_against_the_book(self):
        supports = [fragment("S1", BOOK), fragment("S2", OTHER)]
        for left, right in self.PAIRS:
            with self.subTest(marks=left + right):
                invented = check_answer(answer([[GOOD]] * 3, extra_sentence=f"Aristotle says {left}{INVENTED}{right} here."), supports)
                self.assertFalse(invented.ok)
                real = check_answer(answer([[GOOD]] * 3, extra_sentence=f"Aristotle says {left}{REAL}{right} here."), supports)
                self.assertTrue(real.ok, real.violations)

    def test_a_mark_without_a_pair_does_not_move_the_pairs(self):
        # The stray mark comes first: the invented quotation is still seen as it stands.
        for stray in ('" ', '"', "“ ", "' ", "« "):
            with self.subTest(stray=stray):
                found = quoted_phrases(f'{stray}Aristotle says "{INVENTED}" in his book.')
                self.assertIn((INVENTED,), found)
        found = quoted_phrases(f'Aristotle says "{INVENTED}" in " his book.')
        self.assertIn((INVENTED,), found)

    def test_unclosed_and_unopened_phrases_are_quotations(self):
        self.assertEqual(quoted_phrases(f"Aristotle says “{INVENTED}."), [(INVENTED,)])
        self.assertEqual(quoted_phrases(f'Aristotle says "{INVENTED}.'), [(INVENTED,)])
        self.assertEqual(quoted_phrases(f"Aristotle says ‘{INVENTED}."), [(INVENTED,)])
        self.assertEqual(quoted_phrases(f"{INVENTED}” says Aristotle."), [(INVENTED,)])
        self.assertEqual(quoted_phrases(f"{INVENTED}' says Aristotle."), [(INVENTED,)])
        supports = [fragment("S1", BOOK)]
        self.assertFalse(check_answer(answer([[GOOD]] * 3, extra_sentence=f"Aristotle says “{INVENTED}."), supports).ok)

    def test_nested_and_neighbouring_phrases(self):
        self.assertEqual(quoted_phrases("“He said ‘a b c’ to me”"), [("He said ‘a b c’ to me",), ("a b c",)])
        self.assertEqual(quoted_phrases('"one" is not the same thing as "two" here'), [])
        self.assertEqual(quoted_phrases('("a b c") and — "d e f" — more'), [("a b c",), ("d e f",)])
        self.assertEqual(quoted_phrases('He says: "...and so it goes."'), [("and so it goes",)])

    def test_words_after_a_possessive_looking_mark_are_not_hidden(self):
        # The first closing mark looks like `sons’ sons`; the real one closes invented words.
        self.assertEqual(quoted_phrases("‘the sons and sons’ invented words here’"), [("the sons and sons’ invented words here",)])
        self.assertEqual(quoted_phrases("it's the man's own sons' sons and the citizens’ rights"), [])


class WhereAQuotationMayStandTests(SimpleTestCase):
    def setUp(self):
        self.supports = [fragment("S1", BOOK), fragment("S2", OTHER)]

    def series(self, en="Aristotle says that man lives in a city.", ru="Аристотель говорит, что человек живёт в городе."):
        series = answer([[GOOD]] * 3)
        series["parts"][1]["sentences"].append({"en": en, "ru": ru})
        return series

    def test_russian_line_needs_a_checked_quotation_in_its_english_sentence(self):
        ok = check_answer(self.series(en=f"He says: “{REAL}”.", ru="Он говорит: «человек по природе есть животное политическое»."), self.supports)
        self.assertTrue(ok.ok, ok.violations)
        # No quotation in the English sentence.
        bad = check_answer(self.series(ru="Он говорит: «человек человеку волк»."), self.supports)
        self.assertEqual(len(bad.violations), 1, bad.violations)
        self.assertIn("part 2, sentence 2: the Russian line has words in quotation marks", bad.violations[0])
        # An English quotation that is not in the book does not open the door.
        bad = check_answer(self.series(en=f"He says: “{INVENTED}”.", ru="Он говорит: «человек человеку волк»."), self.supports)
        self.assertEqual(len(bad.violations), 2, bad.violations)
        # A quotation in another sentence does not count.
        series = self.series(ru="Он говорит: «человек человеку волк».")
        series["parts"][1]["sentences"].insert(0, {"en": f"He says: “{REAL}”.", "ru": "Он так говорит."})
        self.assertFalse(check_answer(series, self.supports).ok)
        # Two words in marks are a term, in either language.
        ok = check_answer(self.series(en="He calls man a “political animal”.", ru="Он называет человека «политическим животным»."), self.supports)
        self.assertTrue(ok.ok, ok.violations)
        for marks in ('""', "“”", "„“", "''"):
            with self.subTest(marks=marks):
                bad = check_answer(self.series(ru=f"Он говорит: {marks[0]}человек человеку волк{marks[1]}."), self.supports)
                self.assertFalse(bad.ok)

    def test_titles_have_no_quotation_even_a_real_one(self):
        for key in ("title_en", "title_ru"):
            for phrase in (REAL, INVENTED):
                with self.subTest(key=key, phrase=phrase):
                    series = self.series()
                    series[key] = f"“{phrase}”"
                    checked = check_answer(series, self.supports)
                    self.assertEqual(len(checked.violations), 1, checked.violations)
                    self.assertIn(f"{key}: a phrase in quotation marks is not allowed here", checked.violations[0])
        series = self.series()
        series["parts"][2]["title_en"] = f"Why “{REAL}”"
        self.assertIn("part 3 title_en: a phrase in quotation marks is not allowed here", check_answer(series, self.supports).violations[0])
        series = self.series()
        series["title_en"] = "The “political animal”"  # a term
        self.assertTrue(check_answer(series, self.supports).ok)

    def test_glossary_has_no_quotation_in_any_field(self):
        for key in ("term_en", "explanation_en", "translation_ru", "note"):
            for phrase in (REAL, INVENTED, "человек человеку волк"):
                with self.subTest(key=key, phrase=phrase):
                    series = self.series()
                    series["glossary"] = [{"term_en": "city", "explanation_en": "A place.", "translation_ru": "город", key: f"He said: “{phrase}”."}]
                    checked = check_answer(series, self.supports)
                    self.assertEqual(len(checked.violations), 1, checked.violations)
                    self.assertIn(f"glossary {key}: a phrase in quotation marks is not allowed here", checked.violations[0])
        series = self.series()
        series["glossary"] = [{"term_en": "city-state", "explanation_en": "A city that is a “small state”.", "translation_ru": "город-государство"}]
        self.assertTrue(check_answer(series, self.supports).ok)


class UntrustedShapeTests(SimpleTestCase):
    """The model's answer is data: a wrong kind of value is a violation
    for the correction attempt, never a TypeError."""

    def setUp(self):
        self.supports = [fragment("S1", BOOK)]

    def assert_violation(self, change, expected):
        series = copy.deepcopy(answer([[GOOD]] * 3))  # GOOD is shared by the tests
        change(series)
        checked = check_answer(series, self.supports)  # must not raise
        self.assertFalse(checked.ok)
        self.assertTrue(any(expected in violation for violation in checked.violations), checked.violations)

    def test_values_of_the_wrong_kind_are_violations(self):
        cases = (
            (lambda s: s["parts"][0]["sources"][0].update(id=["S1"]), "part 1: every source must be an object with a string id and a string quote"),
            (lambda s: s["parts"][0]["sources"][0].update(id={"a": 1}), "part 1: every source must be"),
            (lambda s: s["parts"][0]["sources"][0].update(id=1), "part 1: every source must be"),
            (lambda s: s["parts"][0]["sources"][0].update(quote=["man is by nature a political animal"]), "part 1: every source must be"),
            (lambda s: s["parts"][0]["sources"][0].update(quote=None), "part 1: every source must be"),
            (lambda s: s["parts"][0].update(sources=["S1"]), "part 1: every source must be"),
            (lambda s: s["parts"][0].update(sources={"id": "S1"}), "part 1: sources must be a list"),
            (lambda s: s["parts"][0].update(sources="S1"), "part 1: sources must be a list"),
            (lambda s: s["parts"][0].update(sources=None), "part 1: sources must be a list"),
            (lambda s: s.update(title_en=["A"]), "title_en must be a string"),
            (lambda s: s.update(title_en=5), "title_en must be a string"),
            (lambda s: s.update(title_ru={"x": 1}), "title_ru must be a string"),
            (lambda s: s.update(title_ru=None), "title_ru must be a string"),
            (lambda s: s["parts"][1].update(title_en=7), "part 2: title_en must be a string"),
            (lambda s: s["parts"][1].update(title_en=["x"]), "part 2: title_en must be a string"),
            (lambda s: s["parts"].__setitem__(2, "part"), "part 3: must be an object"),
            (lambda s: s["parts"].__setitem__(2, None), "part 3: must be an object"),
            (lambda s: s["parts"][0].update(sentences="x"), "part 1: 1 to 12 sentences are required"),
            (lambda s: s["parts"][0]["sentences"].__setitem__(0, "x"), "part 1: every sentence needs"),
            (lambda s: s["parts"][0]["sentences"][0].update(en=5), "part 1: every sentence needs"),
            (lambda s: s["parts"][0]["sentences"][0].update(ru=["x"]), "part 1: every sentence needs"),
            (lambda s: s.update(glossary="x"), "glossary must be a list"),
            (lambda s: s.update(glossary=None), "glossary must be a list"),
            (lambda s: s.update(glossary=["x"]), "glossary must be a list"),
            (lambda s: s.update(glossary=[{"term_en": 5}]), "glossary must be a list"),
            (lambda s: s.update(glossary=[{"term_en": "a", "explanation_en": ["x"]}]), "glossary must be a list"),
            (lambda s: s.update(parts={"a": 1}), "the answer must have 3 to 5 parts"),
        )
        for number, (change, expected) in enumerate(cases):
            with self.subTest(case=number, expected=expected):
                self.assert_violation(change, expected)
        for series in (None, [], "text", 5, {"parts": None}):
            with self.subTest(series=series):
                self.assertFalse(check_answer(series, self.supports).ok)

    def test_missing_optional_fields_are_fine(self):
        series = answer([[GOOD]] * 3)
        del series["glossary"], series["title_en"], series["title_ru"]
        del series["parts"][0]["title_en"]
        checked = check_answer(series, self.supports)
        self.assertTrue(checked.ok, checked.violations)
