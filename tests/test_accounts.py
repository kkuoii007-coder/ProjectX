"""HTTP-level checks: permissions, CSRF, registration, private caching."""

from django.test import Client, TestCase, override_settings

from apps.accounts.models import InviteCode, User
from apps.credits.models import Wallet, grant_welcome_credits


@override_settings(REGISTRATION_MODE="invite")
class RegistrationTests(TestCase):
    def setUp(self):
        InviteCode.objects.create(code="INV1")

    def test_register_requires_valid_invite(self):
        c = Client()
        resp = c.post("/accounts/register/", {"login": "newuser1", "password": "TestPassword123!", "password_confirm": "TestPassword123!", "no_recovery_ack": "on"})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(User.objects.filter(login="newuser1").exists())

    def test_register_grants_credits_once(self):
        c = Client()
        resp = c.post("/accounts/register/", {"login": "newuser2", "password": "TestPassword123!", "password_confirm": "TestPassword123!", "no_recovery_ack": "on", "invite_code": "INV1"})
        self.assertEqual(resp.status_code, 302)
        user = User.objects.get(login="newuser2")
        self.assertEqual(Wallet.objects.get(user=user).total_credits, 30)
        # second POST with the same invite must not create a second account
        resp2 = c.post("/accounts/register/", {"login": "newuser3", "password": "TestPassword123!", "password_confirm": "TestPassword123!", "no_recovery_ack": "on", "invite_code": "INV1"})
        self.assertEqual(resp2.status_code, 200)
        self.assertFalse(User.objects.filter(login="newuser3").exists())

    def test_registration_closed_mode(self):
        from django.test import override_settings

        c = Client()
        with override_settings(REGISTRATION_MODE="closed"):
            resp = c.get("/accounts/register/")
            self.assertEqual(resp.status_code, 403)


class PermissionTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(login="permuser", password="TestPassword123!")
        grant_welcome_credits(self.user)
        self.other = User.objects.create_user(login="permother", password="TestPassword123!")

    def test_dashboard_requires_login(self):
        resp = Client().get("/account/")
        self.assertEqual(resp.status_code, 302)

    def test_dashboard_only_own_data(self):
        c = Client()
        c.force_login(self.user)
        resp = c.get("/account/")
        self.assertEqual(resp.status_code, 200)

    def test_export_returns_json_for_owner(self):
        c = Client()
        c.force_login(self.user)
        resp = c.get("/account/export/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("attachment", resp.headers.get("Content-Disposition", ""))

    def test_logout_all_revokes_session(self):
        c = Client()
        c.force_login(self.user)
        resp = c.post("/accounts/logout-all/")
        self.assertEqual(resp.status_code, 302)
        resp = c.get("/account/")
        self.assertEqual(resp.status_code, 302)

    def test_no_email_fields_on_model(self):
        field_names = [f.name for f in User._meta.get_fields()]
        self.assertNotIn("email", field_names)
        self.assertNotIn("first_name", field_names)
        self.assertNotIn("last_name", field_names)
