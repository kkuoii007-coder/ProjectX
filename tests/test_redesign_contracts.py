"""Rendered UI contracts for the editorial redesign, without browser dependencies."""

from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from apps.accounts.models import User
from apps.ai.models import AIJob
from apps.credits.models import Wallet, grant_welcome_credits
from apps.library.models import Favorite, Publication
from apps.library.templatetags.ui import ru_plural
from apps.workshop.models import Project, Revision


class Element:
    def __init__(self, tag, attrs, ancestors):
        self.tag = tag
        self.attrs = dict(attrs)
        self.ancestors = tuple(ancestors)
        self.text = ""


class Markup(HTMLParser):
    """Small stdlib parser that retains element ancestry for form assertions."""

    VOID_TAGS = {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }

    def __init__(self, response):
        super().__init__(convert_charrefs=True)
        self.elements = []
        self.stack = []
        self.feed(response.content.decode())

    def handle_starttag(self, tag, attrs):
        element = Element(tag, attrs, self.stack)
        self.elements.append(element)
        if tag not in self.VOID_TAGS:
            self.stack.append(element)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        for element in self.stack:
            element.text += data

    def find(self, tag=None, css_class=None, within=None, **attrs):
        return [
            element for element in self.elements
            if (tag is None or element.tag == tag)
            and (css_class is None or css_class in element.attrs.get("class", "").split())
            and (within is None or within in element.ancestors)
            and all(element.attrs.get(key) == value for key, value in attrs.items())
        ]


class RedesignContractsTests(TestCase):
    password = "RedesignContract123!"
    payload = {
        "title_ru": "Рабочий текст",
        "note": "Проверка сохранения примечания.",
        "parts": [
            {
                "title_en": "A first question",
                "sentences": [
                    {"en": "What is a good question?", "ru": "Что такое хороший вопрос?"},
                    {"en": "We can ask again.", "ru": "Мы можем спросить ещё раз."},
                ],
            },
            {
                "title_en": "A second question",
                "sentences": [
                    {"en": "Does the answer help?", "ru": "Помогает ли ответ?"},
                ],
            },
        ],
    }

    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", verbosity=0)
        cls.publication = Publication.objects.get(slug="socrates-questions")
        cls.user = User.objects.create_user(login="redesignreader", password=cls.password)
        grant_welcome_credits(cls.user)
        Favorite.objects.create(user=cls.user, publication=cls.publication)
        cls.project = Project.objects.create(
            owner=cls.user, title="Проверка мастерской", mode=Project.Mode.EXPLAIN
        )
        cls.previous = Revision.objects.create(
            project=cls.project, payload={"title_ru": "Предыдущая версия", "parts": []}
        )
        cls.revision = Revision.objects.create(
            project=cls.project, parent=cls.previous, payload=cls.payload
        )
        cls.project.head_revision = cls.revision
        cls.project.save(update_fields=["head_revision"])
        cls.job = AIJob.objects.create(
            owner=cls.user,
            project=cls.project,
            kind=AIJob.Kind.GENERATE,
            idempotency_key="redesign-render-contract",
            input_hash="a" * 64,
            policy_version="test",
            status=AIJob.Status.SUCCEEDED,
            outcome=AIJob.Outcome.DELIVERED,
        )

    def setUp(self):
        self.client.force_login(self.user)

    def project_response(self):
        return self.client.get(reverse("workshop:project", args=[self.project.pk]))

    def assert_post_forms_have_csrf(self, response):
        self.assertEqual(response.status_code, 200)
        markup = Markup(response)
        forms = markup.find("form", method="post")
        self.assertTrue(forms, "The fixture must exercise at least one POST form")
        for form in forms:
            with self.subTest(action=form.attrs.get("action"), css_class=form.attrs.get("class")):
                tokens = markup.find("input", within=form, name="csrfmiddlewaretoken")
                self.assertEqual(len(tokens), 1)
                self.assertEqual(tokens[0].attrs.get("type"), "hidden")
                self.assertTrue(tokens[0].attrs.get("value"))
                self.assertFalse(any(parent.tag == "form" for parent in form.ancestors))

    def test_every_post_form_contains_its_own_rendered_csrf_token(self):
        pages = [
            ("library:home", []),
            ("library:read", [self.publication.slug]),
            ("account:dashboard", []),
            ("account:credits", []),
            ("account:favorites", []),
            ("account:words", []),
            ("account:security", []),
            ("workshop:create_project", []),
            ("workshop:project", [self.project.pk]),
        ]
        for route, args in pages:
            with self.subTest(route=route):
                self.assert_post_forms_have_csrf(self.client.get(reverse(route, args=args)))
        guest = Client()
        for route in ("accounts:login", "accounts:register"):
            with self.subTest(route=route):
                self.assert_post_forms_have_csrf(guest.get(reverse(route)))

    def test_nested_workshop_edit_forms_keep_exact_targets_and_fields(self):
        markup = Markup(self.project_response())
        forms = markup.find("form", action=reverse("workshop:edit_fragment", args=[self.project.pk]))
        self.assertEqual(len(forms), 5)
        actual_targets = []
        for form in forms:
            fields = {
                element.attrs["name"]: element.attrs.get("value", "")
                for element in markup.find("input", within=form)
            }
            self.assertEqual(
                set(fields), {"csrfmiddlewaretoken", "kind", "target", "idempotency_key", "instruction"}
            )
            self.assertEqual(fields["idempotency_key"], "")
            actual_targets.append((fields["kind"], fields["target"]))
            self.assertTrue(any(parent.tag == "details" for parent in form.ancestors))
            instruction = markup.find("input", within=form, name="instruction")[0]
            self.assertEqual(instruction.attrs.get("maxlength"), "1000")
            self.assertIn("required", instruction.attrs)
        self.assertEqual(
            actual_targets,
            [
                ("edit_part", "0"),
                ("edit_sentence", "0:0"),
                ("edit_sentence", "0:1"),
                ("edit_part", "1"),
                ("edit_sentence", "1:0"),
            ],
        )
        disclosures = markup.find("details", css_class="workshop-edit")
        self.assertEqual(len(disclosures), 5)
        for disclosure in disclosures:
            self.assertNotIn("open", disclosure.attrs)
            self.assertEqual(len(markup.find("summary", within=disclosure)), 1)

    def test_workshop_translations_and_language_hooks_render_real_payload(self):
        response = self.project_response()
        markup = Markup(response)
        reading = markup.find("article", css_class="workshop-reading")
        self.assertEqual(len(reading), 1)
        sentences = markup.find("span", css_class="s", within=reading[0])
        expected = [sentence for part in self.payload["parts"] for sentence in part["sentences"]]
        self.assertEqual(len(sentences), len(expected))
        self.assertEqual(len(markup.find("div", css_class="workshop-sentence")), len(expected))
        ids = [element.attrs["id"] for element in markup.elements if "id" in element.attrs]
        self.assertEqual(len(ids), len(set(ids)))
        for sentence, content in zip(sentences, expected):
            self.assertEqual(sentence.text, content["en"])
            self.assertEqual(sentence.attrs.get("lang"), "en")
            # The server renders readable text, not inactive controls.
            # initGlosses adds button/keyboard semantics only when JS runs.
            for attribute in ("role", "tabindex", "aria-expanded", "aria-controls"):
                self.assertNotIn(attribute, sentence.attrs)
            target = sentence.attrs.get("data-gloss")
            self.assertTrue(target)
            translations = markup.find("span", css_class="gloss", id=target)
            self.assertEqual(len(translations), 1)
            self.assertEqual(translations[0].text, content["ru"])
            self.assertEqual(translations[0].attrs.get("role"), "note")
            self.assertEqual(translations[0].attrs.get("lang"), "ru")
            self.assertNotIn("hidden", translations[0].attrs)
            self.assertFalse(markup.find("p", within=sentence))
            wrapper = sentence.ancestors[-1]
            self.assertIn(wrapper, translations[0].ancestors)
            self.assertEqual(len(markup.find("details", within=wrapper)), 1)
        headings = markup.find("h3", within=reading[0])
        self.assertEqual([heading.text for heading in headings], [part["title_en"] for part in self.payload["parts"]])
        self.assertTrue(all(heading.attrs.get("lang") == "en" for heading in headings))
        self.assertContains(response, self.payload["note"])
        self.assertNotContains(response, "{{")
        self.assertNotContains(response, "{%")

    def test_workshop_revision_and_question_forms_keep_actions(self):
        markup = Markup(self.project_response())
        apply_forms = markup.find("form", action=reverse("workshop:apply_head", args=[self.project.pk]))
        self.assertEqual(len(apply_forms), 1)
        self.assertEqual(
            markup.find("input", within=apply_forms[0], name="revision_id")[0].attrs["value"],
            str(self.previous.pk),
        )
        question_forms = markup.find("form", action=reverse("workshop:ask", args=[self.project.pk]))
        self.assertEqual(len(question_forms), 1)
        question = markup.find("textarea", within=question_forms[0], name="instruction")[0]
        self.assertEqual(question.attrs["maxlength"], "2000")
        self.assertTrue(markup.find("input", within=question_forms[0], name="idempotency_key"))

    def test_workshop_mutations_still_require_post_and_csrf(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        for route in ("workshop:edit_fragment", "workshop:ask", "workshop:apply_head"):
            with self.subTest(route=route):
                url = reverse(route, args=[self.project.pk])
                self.assertEqual(client.get(url).status_code, 405)
                self.assertEqual(client.post(url, {"instruction": "Проверка"}).status_code, 403)

    def test_account_navigation_has_current_page_dashboard_and_export(self):
        names = ("dashboard", "credits", "favorites", "words", "security")
        for name in names:
            with self.subTest(page=name):
                markup = Markup(self.client.get(reverse(f"account:{name}")))
                nav = markup.find("nav", css_class="account-nav")
                self.assertEqual(len(nav), 1)
                links = markup.find("a", within=nav[0])
                self.assertEqual(
                    {link.attrs["href"] for link in links},
                    {reverse(f"account:{item}") for item in (*names, "export")},
                )
                current = [link for link in links if link.attrs.get("aria-current") == "page"]
                self.assertEqual(len(current), 1)
                self.assertEqual(current[0].attrs["href"], reverse(f"account:{name}"))

    def test_guest_auth_navigation_marks_the_current_page(self):
        guest = Client()
        for name in ("login", "register"):
            with self.subTest(page=name):
                markup = Markup(guest.get(reverse(f"accounts:{name}")))
                nav = markup.find("nav", css_class="account-nav")[0]
                current = markup.find("a", within=nav, **{"aria-current": "page"})
                self.assertEqual(len(current), 1)
                self.assertEqual(current[0].attrs["href"], reverse(f"accounts:{name}"))

    def test_secondary_pages_have_specific_document_titles(self):
        routes = [
            ("account:dashboard", [], "Кабинет"),
            ("account:credits", [], "Кредиты"),
            ("account:favorites", [], "Избранное"),
            ("account:words", [], "Сохранённые слова"),
            ("account:security", [], "Безопасность"),
            ("accounts:login", [], "Вход"),
            ("accounts:register", [], "Регистрация"),
            ("library:library", [], "Библиотека"),
            ("library:credits_help", [], "Как работают кредиты"),
            ("library:thinker", [self.publication.thinker.slug], self.publication.thinker.name_ru),
            ("workshop:create_project", [], "Новый проект"),
            ("workshop:project", [self.project.pk], self.project.title),
        ]
        collection = self.publication.collections.get()
        routes.append(("library:collection", [collection.slug], collection.title_ru))
        for route, args, title in routes:
            with self.subTest(route=route):
                response = self.client.get(reverse(route, args=args))
                self.assertEqual(response.status_code, 200)
                markup = Markup(response)
                self.assertEqual(len(markup.find("h1")), 1)
                self.assertTrue(markup.find("title")[0].text.startswith(f"{title} · "))
                self.assertEqual(markup.find("html")[0].attrs.get("lang"), "ru")

    def test_tables_are_named_focusable_scroll_regions(self):
        for route, args in [
            ("account:dashboard", []),
            ("account:credits", []),
            ("workshop:project", [self.project.pk]),
        ]:
            with self.subTest(route=route):
                markup = Markup(self.client.get(reverse(route, args=args)))
                tables = markup.find("table")
                self.assertTrue(tables)
                for table in tables:
                    wrapper = table.ancestors[-1]
                    self.assertIn("table-scroll", wrapper.attrs.get("class", "").split())
                    self.assertEqual(wrapper.attrs.get("tabindex"), "0")
                    self.assertEqual(wrapper.attrs.get("role"), "region")
                    self.assertTrue(wrapper.attrs.get("aria-label"))
                    for heading in markup.find("th", within=table):
                        self.assertEqual(heading.attrs.get("scope"), "col")
        dashboard = self.client.get(reverse("account:dashboard"))
        self.assertContains(dashboard, 'aria-live="polite"')
        self.assertContains(dashboard, self.job.status)

    def test_catalog_cards_show_only_real_metadata_and_links(self):
        collection = self.publication.collections.get()
        for route, args in [
            ("library:library", []),
            ("library:collection", [collection.slug]),
            ("library:thinker", [self.publication.thinker.slug]),
        ]:
            with self.subTest(route=route):
                response = self.client.get(reverse(route, args=args))
                markup = Markup(response)
                catalog = markup.find("div", css_class="catalog-list")
                self.assertEqual(len(catalog), 1)
                card = markup.find("article", css_class="publication-card", within=catalog[0])[0]
                self.assertIn(self.publication.revision.payload["title_ru"], card.text)
                self.assertIn(self.publication.revision.payload["lead_ru"], card.text)
                self.assertIn(self.publication.thinker.name_ru, card.text)
                count = len(self.publication.revision.payload["parts"])
                self.assertIn(ru_plural(count, "часть,части,частей"), card.text)
                self.assertTrue(markup.find("a", within=card, href=reverse("library:read", args=[self.publication.slug])))
                self.assertTrue(markup.find("a", within=card, href=reverse("library:thinker", args=[self.publication.thinker.slug])))
                self.assertNotIn("A0", card.text)

    def test_search_is_get_only_with_real_reset_and_distinct_empty_states(self):
        response = self.client.get(reverse("library:library"), {"q": "неттакоготекста"})
        markup = Markup(response)
        form = markup.find("form", css_class="search-form")[0]
        self.assertEqual(form.attrs.get("method"), "get")
        self.assertEqual([field.attrs.get("name") for field in markup.find("input", within=form)], ["q"])
        reset = markup.find("a", css_class="search-reset")
        self.assertEqual(reset[0].attrs["href"], reverse("library:library"))
        self.assertContains(response, "Ничего не найдено")
        self.assertNotContains(response, "Первые материалы готовятся")
        Publication.objects.update(status=Publication.Status.HIDDEN)
        empty = self.client.get(reverse("library:library"))
        self.assertContains(empty, "Первые материалы готовятся")
        self.assertNotContains(empty, "Ничего не найдено")
        self.assertNotContains(empty, 'class="publication-card"')

    def test_security_copy_and_password_autocomplete_match_existing_behavior(self):
        response = self.client.get(reverse("account:security"))
        self.assertContains(response, "аккаунт сразу деактивируется, а приватные данные удаляются")
        self.assertNotContains(response, "отдельной операцией")
        markup = Markup(response)
        expected = {
            "current_password": "current-password",
            "new_password": "new-password",
            "new_password_confirm": "new-password",
            "password": "current-password",
        }
        for name, autocomplete in expected.items():
            self.assertEqual(markup.find("input", name=name)[0].attrs.get("autocomplete"), autocomplete)
        guest = Client()
        for route, expected in [
            ("accounts:login", {"login": "username", "password": "current-password"}),
            ("accounts:register", {"login": "username", "password": "new-password", "password_confirm": "new-password"}),
        ]:
            markup = Markup(guest.get(reverse(route)))
            for name, autocomplete in expected.items():
                self.assertEqual(markup.find("input", name=name)[0].attrs.get("autocomplete"), autocomplete)

    def test_registration_renders_passed_throttle_error_and_form_validation(self):
        guest = Client()
        invalid = guest.post(reverse("accounts:register"), {"login": "x"})
        self.assertContains(invalid, 'class="errorlist"')
        with patch("apps.accounts.views._within_registration_limits", return_value=False):
            response = guest.post(
                reverse("accounts:register"),
                {
                    "login": "blockedregister",
                    "password": self.password,
                    "password_confirm": self.password,
                    "no_recovery_ack": "on",
                },
            )
        self.assertContains(response, "Превышен лимит регистраций", status_code=429)
        self.assertContains(response, 'role="alert"', status_code=429)
        self.assertNotContains(response, "Аккаунт создан", status_code=429)

    def test_registration_success_and_closed_pages_have_specific_titles(self):
        guest = Client()
        response = guest.post(
            reverse("accounts:register"),
            {
                "login": "createdreader",
                "password": self.password,
                "password_confirm": self.password,
                "no_recovery_ack": "on",
            },
        )
        self.assertContains(response, "<title>Аккаунт создан · Philosophy Reader</title>", html=True)
        self.assertContains(response, 'role="status"')
        self.assertContains(response, reverse("account:dashboard"))
        with override_settings(REGISTRATION_MODE="closed"):
            closed = Client().get(reverse("accounts:register"))
        self.assertContains(closed, "<title>Регистрация закрыта · Philosophy Reader</title>", status_code=403, html=True)

    def test_words_keeps_bound_form_validation(self):
        response = self.client.post(reverse("account:words"), {"term": "virtue", "translation_ru": ""})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="errorlist"')
        self.assertContains(response, 'value="virtue"')

    def test_credit_help_keeps_full_rules_and_settings_values(self):
        costs = {"generate": 19, "edit_series": 17, "edit_part": 11, "edit_sentence": 7, "ask": 3}
        with override_settings(WELCOME_CREDITS=47, CREDIT_COSTS=costs):
            response = self.client.get(reverse("library:credits_help"))
        body = Markup(response).find("article", css_class="prose")[0].text
        for text in [
            "47 бесплатных кредитов",
            "Отдельное подтверждение стоимости перед действием не требуется.",
            "Создание серии: 19",
            "Перегенерация серии: 17",
            "Правка части: 11",
            "Правка предложения: 7",
            "Вопрос: 3",
            "Чтение и готовый перевод: 0",
            "Успешно выполненная правка списывается даже если вы не применили её.",
            "Ошибка или недоставленный результат не оплачивается.",
            "Продажи кредитов сейчас не включены.",
            "кнопка покупки не имитируется.",
        ]:
            self.assertIn(text, body)

    def test_secondary_page_views_leave_learning_payload_and_credits_unchanged(self):
        before = Wallet.objects.get(user=self.user).total_credits
        original_hash = self.revision.content_hash
        for route, args in [
            ("workshop:project", [self.project.pk]),
            ("account:dashboard", []),
            ("account:credits", []),
            ("account:favorites", []),
            ("account:words", []),
            ("account:security", []),
            ("library:library", []),
        ]:
            self.assertEqual(self.client.get(reverse(route, args=args)).status_code, 200)
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.payload, self.payload)
        self.assertEqual(self.revision.content_hash, original_hash)
        self.assertEqual(Wallet.objects.get(user=self.user).total_credits, before)

    def test_assigned_templates_use_titles_shared_navigation_and_no_inline_styles(self):
        root = Path(settings.BASE_DIR) / "templates"
        pages = list((root / "accounts").glob("*.html")) + list((root / "workshop").glob("*.html"))
        pages += [root / "library" / f"{name}.html" for name in ("library", "collection", "thinker", "credits_help")]
        for page in pages:
            with self.subTest(template=page.name):
                source = page.read_text()
                self.assertIn("{% block title %}", source)
                self.assertNotIn("style=", source)
                if page.parent.name == "accounts":
                    self.assertIn("includes/account_nav.html", source)
