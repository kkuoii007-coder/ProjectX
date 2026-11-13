"""Account actions must return to real local pages without changing policy."""

from django.test import Client, TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.credits.models import grant_welcome_credits
from apps.library.models import Favorite, Publication, SavedWord
from apps.workshop.models import Project, Revision


class AccountNavigationRegressionTests(TestCase):
    password = "TestPassword123!"

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(login="navigationuser", password=cls.password)
        grant_welcome_credits(cls.user)
        cls.project = Project.objects.create(
            owner=cls.user, title="Навигация", mode=Project.Mode.EXPLAIN
        )
        revision = Revision.objects.create(
            project=cls.project,
            payload={
                "title_ru": "Проверка переходов",
                "parts": [{"title_en": "Questions", "sentences": [{"en": "Why?", "ru": "Почему?"}]}],
            },
        )
        cls.publication = Publication.objects.create(
            slug="navigation-regression", revision=revision, status=Publication.Status.PUBLISHED
        )

    def setUp(self):
        self.client.force_login(self.user)

    def test_password_change_returns_to_security_and_keeps_current_session(self):
        other_session = Client()
        other_session.force_login(self.user)
        original_epoch = self.user.auth_epoch
        new_password = "DifferentPassword456!"

        response = self.client.post(
            reverse("account:change_password"),
            {
                "current_password": self.password,
                "new_password": new_password,
                "new_password_confirm": new_password,
            },
        )

        self.assertRedirects(response, reverse("account:security"))
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(new_password))
        self.assertEqual(self.user.auth_epoch, original_epoch + 1)
        self.assertEqual(self.client.get(reverse("account:dashboard")).status_code, 200)
        self.assertEqual(other_session.get(reverse("account:dashboard")).status_code, 302)

    def test_invalid_current_password_returns_to_security_without_change(self):
        original_epoch = self.user.auth_epoch
        response = self.client.post(
            reverse("account:change_password"),
            {
                "current_password": "wrong",
                "new_password": "DifferentPassword456!",
                "new_password_confirm": "DifferentPassword456!",
            },
            follow=True,
        )

        self.assertRedirects(response, reverse("account:security"))
        self.assertContains(response, "Текущий пароль неверен")
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(self.password))
        self.assertEqual(self.user.auth_epoch, original_epoch)

    def test_mismatched_passwords_return_to_security_without_change(self):
        response = self.client.post(
            reverse("account:change_password"),
            {
                "current_password": self.password,
                "new_password": "DifferentPassword456!",
                "new_password_confirm": "DoesNotMatch789!",
            },
            follow=True,
        )

        self.assertRedirects(response, reverse("account:security"))
        self.assertContains(response, "Новые пароли не совпадают")
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(self.password))

    def test_invalid_delete_password_returns_to_security_without_purge(self):
        response = self.client.post(
            reverse("account:delete"), {"password": "wrong"}, follow=True
        )

        self.assertRedirects(response, reverse("account:security"))
        self.assertContains(response, "Пароль неверен")
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)
        self.assertTrue(self.user.check_password(self.password))
        self.assertTrue(Project.objects.filter(pk=self.project.pk, owner=self.user).exists())
        self.assertEqual(self.client.get(reverse("account:dashboard")).status_code, 200)

    def test_save_word_returns_to_words_and_preserves_existing_update_behavior(self):
        payload = {
            "term": "virtue",
            "translation_ru": "добродетель",
            "context_en": "What is virtue?",
            "publication_id": str(self.publication.pk),
        }
        response = self.client.post(reverse("account:words"), payload, follow=True)

        self.assertRedirects(response, reverse("account:words"))
        self.assertContains(response, "Слово сохранено")
        word = SavedWord.objects.get(user=self.user, term="virtue")
        self.assertEqual(word.translation_ru, payload["translation_ru"])
        self.assertEqual(word.context_en, payload["context_en"])
        self.assertEqual(word.source_reference, self.publication)

        payload["translation_ru"] = "добродетель, достоинство"
        response = self.client.post(reverse("account:words"), payload)
        self.assertRedirects(response, reverse("account:words"))
        self.assertEqual(SavedWord.objects.filter(user=self.user, term="virtue").count(), 1)
        word.refresh_from_db()
        self.assertEqual(word.translation_ru, payload["translation_ru"])

    def test_favorite_without_next_returns_to_favorites_after_add_and_remove(self):
        url = reverse("account:favorite_toggle", args=[self.publication.pk])
        response = self.client.post(url)
        self.assertRedirects(response, reverse("account:favorites"))
        self.assertTrue(Favorite.objects.filter(user=self.user, publication=self.publication).exists())

        response = self.client.post(url)
        self.assertRedirects(response, reverse("account:favorites"))
        self.assertFalse(Favorite.objects.filter(user=self.user, publication=self.publication).exists())

    def test_favorite_accepts_local_reader_return_path(self):
        next_url = reverse("library:read", args=[self.publication.slug]) + "?from=favorites#part-1"
        response = self.client.post(
            reverse("account:favorite_toggle", args=[self.publication.pk]), {"next": next_url}
        )

        self.assertRedirects(response, next_url)
        self.assertTrue(Favorite.objects.filter(user=self.user, publication=self.publication).exists())

    def test_favorite_accepts_same_host_urls_for_http_request(self):
        for next_url in (
            "http://testserver/account/favorites/",
            "https://testserver/account/favorites/",
            "//testserver/account/favorites/",
        ):
            with self.subTest(next_url=next_url):
                response = self.client.post(
                    reverse("account:favorite_toggle", args=[self.publication.pk]),
                    {"next": next_url},
                )
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response["Location"], next_url)

    def test_favorite_rejects_external_or_unsafe_return_urls(self):
        for next_url in (
            "",
            "   ",
            "https://outside.example/account/",
            "//outside.example/account/",
            "///outside.example/account/",
            r"/\outside.example/account/",
            "https://testserver.outside.example/account/",
            "https://testserver@outside.example/account/",
            "javascript:alert(1)",
        ):
            with self.subTest(next_url=next_url):
                response = self.client.post(
                    reverse("account:favorite_toggle", args=[self.publication.pk]),
                    {"next": next_url},
                )
                self.assertRedirects(response, reverse("account:favorites"))

    def test_favorite_https_request_rejects_insecure_return_urls(self):
        for next_url in (
            "http://testserver/account/favorites/",
            "//testserver/account/favorites/",
            "https://outside.example/account/",
        ):
            with self.subTest(next_url=next_url):
                response = self.client.post(
                    reverse("account:favorite_toggle", args=[self.publication.pk]),
                    {"next": next_url},
                    secure=True,
                )
                self.assertRedirects(response, reverse("account:favorites"))

    def test_favorite_https_request_accepts_relative_and_same_host_https_urls(self):
        for next_url in (
            "/account/favorites/",
            "https://testserver/account/favorites/",
        ):
            with self.subTest(next_url=next_url):
                response = self.client.post(
                    reverse("account:favorite_toggle", args=[self.publication.pk]),
                    {"next": next_url},
                    secure=True,
                )
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response["Location"], next_url)

    def test_favorite_still_requires_csrf_and_post(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        url = reverse("account:favorite_toggle", args=[self.publication.pk])

        self.assertEqual(client.get(url).status_code, 405)
        self.assertEqual(client.post(url).status_code, 403)
        self.assertFalse(Favorite.objects.filter(user=self.user, publication=self.publication).exists())

        client.get(reverse("library:read", args=[self.publication.slug]))
        response = client.post(url, {"csrfmiddlewaretoken": client.cookies["csrftoken"].value})
        self.assertRedirects(response, reverse("account:favorites"))
        self.assertTrue(Favorite.objects.filter(user=self.user, publication=self.publication).exists())
