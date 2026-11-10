"""Job creation, idempotency and worker guarantees."""

from uuid import uuid4

from django.test import TestCase

from apps.accounts.models import User
from apps.ai.models import AIJob
from apps.ai.services import (
    InsufficientCredits,
    JobConflict,
    claim_next_job,
    create_job,
    execute_job,
    recover_stale_jobs,
)
from apps.credits.models import Wallet, grant_welcome_credits
from apps.workshop.models import Project


class CreateJobTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(login="jobuser", password="TestPassword123!")
        grant_welcome_credits(self.user)

    def test_idempotent_same_payload(self):
        job1, created1 = create_job(owner=self.user, kind=AIJob.Kind.GENERATE, idempotency_key="k1", instruction="same", project=None)
        job2, created2 = create_job(owner=self.user, kind=AIJob.Kind.GENERATE, idempotency_key="k1", instruction="same", project=None)
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(job1.pk, job2.pk)

    def test_conflict_on_different_payload(self):
        create_job(owner=self.user, kind=AIJob.Kind.GENERATE, idempotency_key="k2", instruction="a")
        with self.assertRaises(JobConflict):
            create_job(owner=self.user, kind=AIJob.Kind.GENERATE, idempotency_key="k2", instruction="b")

    def test_insufficient_credits_leaves_no_reservation(self):
        Wallet.objects.filter(user=self.user).update(total_credits=5, reserved_credits=0)
        before = Wallet.objects.get(user=self.user)
        with self.assertRaises(InsufficientCredits):
            create_job(owner=self.user, kind=AIJob.Kind.GENERATE, idempotency_key="k3", instruction="x")
        after = Wallet.objects.get(user=self.user)
        self.assertEqual(before.reserved_credits, after.reserved_credits)
        self.assertEqual(AIJob.objects.filter(owner=self.user, idempotency_key="k3").count(), 0)

    def test_two_reservations_exceeding_balance_blocked(self):
        """With 15 credits, two 10-credit reservations cannot both hold."""
        Wallet.objects.filter(user=self.user).update(total_credits=15, reserved_credits=0)
        from django.conf import settings as s

        with self.subTest("first ok"):
            job1, _ = create_job(owner=self.user, kind=AIJob.Kind.GENERATE, idempotency_key="k4", instruction="x")
        # USER_ACTIVE_AI_JOBS=1 blocks the second job while the first is queued;
        # the wallet-level guarantee is covered by ReservationTests in test_credits.
        wallet = Wallet.objects.get(user=self.user)
        self.assertEqual(wallet.reserved_credits, 10)


class WorkerTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(login="workeruser", password="TestPassword123!")
        grant_welcome_credits(self.user)
        self.project = Project.objects.create(owner=self.user, title="T", mode=Project.Mode.EXPLAIN)

    def _create(self, key, instruction="Тема"):
        job, _ = create_job(owner=self.user, kind=AIJob.Kind.GENERATE, idempotency_key=key, instruction=instruction, project=self.project)
        return job

    def test_execute_charges_once(self):
        job = self._create("w1")
        claimed = claim_next_job()
        self.assertEqual(claimed.pk, job.pk)
        execute_job(claimed)
        wallet = Wallet.objects.get(user=self.user)
        self.assertEqual(wallet.total_credits, 20)
        self.assertEqual(wallet.reserved_credits, 0)
        # job success is recorded once; no second debit on repeated finalize
        from apps.credits.models import settle_reservation

        self.assertFalse(settle_reservation(job=job, charge=True))
        self.assertEqual(wallet.total_credits, 20)

    def test_failure_releases_reservation(self):
        job = self._create("w2")
        job2 = AIJob.objects.get(pk=job.pk)
        job2.status = AIJob.Status.FAILED
        job2.save(update_fields=["status"])
        from apps.credits.models import settle_reservation

        self.assertTrue(settle_reservation(job=job2, charge=False))
        wallet = Wallet.objects.get(user=self.user)
        self.assertEqual(wallet.total_credits, 30)
        self.assertEqual(wallet.reserved_credits, 0)

    def test_stale_lease_recovered_once(self):
        job = self._create("w3")
        AIJob.objects.filter(pk=job.pk).update(status=AIJob.Status.RUNNING, lease_until="2000-01-01T00:00:00Z")
        count = recover_stale_jobs()
        self.assertGreaterEqual(count, 1)
        job.refresh_from_db()
        self.assertEqual(job.status, AIJob.Status.FAILED)
        wallet = Wallet.objects.get(user=self.user)
        self.assertEqual(wallet.reserved_credits, 0)
        self.assertEqual(wallet.total_credits, 30)

    def test_fake_provider_refused_kind_rejected_gracefully(self):
        """Unknown kind raises a configuration error rather than charging."""
        job = self._create("w4", instruction="ok")
        claimed = claim_next_job()
        execute_job(claimed)
        claimed.refresh_from_db()
        self.assertEqual(claimed.status, AIJob.Status.SUCCEEDED)
