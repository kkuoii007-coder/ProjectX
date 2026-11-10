"""Credit ledger and reservation guarantees."""

from django.db import transaction
from django.test import TestCase

from apps.ai.models import AIJob
from apps.credits.models import (
    CreditGrant,
    CreditReservation,
    CreditTransaction,
    Wallet,
    grant_welcome_credits,
    reserve_for_job,
    settle_reservation,
)
from apps.accounts.models import User


class WelcomeGrantTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(login="grantuser", password="TestPassword123!")

    def test_grant_once_and_idempotent(self):
        grant_welcome_credits(self.user)
        grant_welcome_credits(self.user)
        wallet = Wallet.objects.get(user=self.user)
        self.assertEqual(wallet.total_credits, 30)
        self.assertEqual(wallet.reserved_credits, 0)
        self.assertEqual(CreditGrant.objects.filter(user=self.user).count(), 1)
        self.assertEqual(CreditTransaction.objects.filter(wallet=wallet, kind="grant").count(), 1)

    def test_no_grant_on_repeated_calls_in_transaction(self):
        with transaction.atomic():
            grant_welcome_credits(self.user)
            grant_welcome_credits(self.user)
        self.assertEqual(CreditGrant.objects.filter(user=self.user).count(), 1)


class ReservationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(login="resuser", password="TestPassword123!")
        grant_welcome_credits(self.user)
        self.job = AIJob.objects.create(
            owner=self.user,
            kind=AIJob.Kind.GENERATE,
            input_hash="x" * 64,
            idempotency_key="job-key-1",
            reserved_credits=10,
            policy_version="credits-v1",
        )

    def test_reserve_and_capture_once(self):
        wallet = Wallet.objects.get(user=self.user)
        self.assertTrue(reserve_for_job(job=self.job, wallet=wallet, amount=10))
        self.assertTrue(settle_reservation(job=self.job, charge=True))
        # Second settle must not duplicate the debit
        self.assertFalse(settle_reservation(job=self.job, charge=True))
        wallet = Wallet.objects.get(user=self.user)
        self.assertEqual(wallet.total_credits, 20)
        self.assertEqual(wallet.reserved_credits, 0)
        self.assertEqual(CreditTransaction.objects.filter(wallet=wallet, kind="debit").count(), 1)

    def test_reserve_and_release(self):
        wallet = Wallet.objects.get(user=self.user)
        self.assertTrue(reserve_for_job(job=self.job, wallet=wallet, amount=10))
        self.assertTrue(settle_reservation(job=self.job, charge=False))
        wallet = Wallet.objects.get(user=self.user)
        self.assertEqual(wallet.total_credits, 30)
        self.assertEqual(wallet.reserved_credits, 0)
        self.assertEqual(CreditTransaction.objects.filter(wallet=wallet, kind="release").count(), 1)

    def test_reserve_insufficient_funds(self):
        Wallet.objects.filter(user=self.user).update(total_credits=5, reserved_credits=0)
        wallet = Wallet.objects.get(user=self.user)
        self.assertFalse(reserve_for_job(job=self.job, wallet=wallet, amount=10))
        wallet = Wallet.objects.get(user=self.user)
        self.assertEqual(wallet.reserved_credits, 0)

    def test_late_result_does_not_double_charge(self):
        wallet = Wallet.objects.get(user=self.user)
        reserve_for_job(job=self.job, wallet=wallet, amount=10)
        settle_reservation(job=self.job, charge=False)  # job failed first
        # A late provider response arrives; capture must be refused.
        self.assertFalse(settle_reservation(job=self.job, charge=True))
        wallet = Wallet.objects.get(user=self.user)
        self.assertEqual(wallet.total_credits, 30)
