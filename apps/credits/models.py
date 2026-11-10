from decimal import Decimal

from django.conf import settings
from django.db import models, transaction
from django.utils import timezone


class Wallet(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="wallet")
    total_credits = models.IntegerField(default=0)
    reserved_credits = models.IntegerField(default=0)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(total_credits__gte=0), name="wallet_total_nonnegative"),
            models.CheckConstraint(condition=models.Q(reserved_credits__gte=0), name="wallet_reserved_nonnegative"),
            models.CheckConstraint(condition=models.Q(reserved_credits__lte=models.F("total_credits")), name="wallet_reserved_lte_total"),
        ]

    @property
    def available_credits(self):
        return self.total_credits - self.reserved_credits


class CreditTransaction(models.Model):
    class Kind(models.TextChoices):
        GRANT = "grant"
        RESERVE = "reserve"
        RELEASE = "release"
        DEBIT = "debit"
        TOPUP = "topup"
        ADJUSTMENT = "adjustment"

    wallet = models.ForeignKey(Wallet, on_delete=models.PROTECT, related_name="transactions")
    kind = models.CharField(max_length=20, choices=Kind.choices)
    signed_amount = models.IntegerField()
    operation_key = models.CharField(max_length=100, unique=True)
    job = models.ForeignKey("ai.AIJob", null=True, blank=True, on_delete=models.PROTECT, related_name="credit_transactions")
    reason = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)


class CreditGrant(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="credit_grants")
    campaign = models.CharField(max_length=40)
    amount = models.PositiveIntegerField()
    transaction = models.OneToOneField(CreditTransaction, on_delete=models.PROTECT, related_name="grant")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "campaign"], name="unique_grant_per_campaign")]


def grant_welcome_credits(user):
    """Issue the one-time welcome grant. Safe to call without an outer transaction."""
    if CreditGrant.objects.filter(user=user, campaign=settings.WELCOME_CAMPAIGN).exists():
        return None
    with transaction.atomic():
        return _grant_welcome_credits_locked(user)


def _grant_welcome_credits_locked(user):
    wallet, _ = Wallet.objects.select_for_update().get_or_create(user=user)
    operation_key = f"welcome:{settings.WELCOME_CAMPAIGN}:{user.pk}"
    tx = CreditTransaction.objects.create(
        wallet=wallet,
        kind=CreditTransaction.Kind.GRANT,
        signed_amount=settings.WELCOME_CREDITS,
        operation_key=operation_key,
        reason="Бесплатный стартовый грант",
    )
    Wallet.objects.filter(pk=wallet.pk).update(total_credits=models.F("total_credits") + settings.WELCOME_CREDITS)
    return CreditGrant.objects.create(user=user, campaign=settings.WELCOME_CAMPAIGN, amount=settings.WELCOME_CREDITS, transaction=tx)


class CreditReservation(models.Model):
    class State(models.TextChoices):
        ACTIVE = "active"
        CAPTURED = "captured"
        RELEASED = "released"

    job = models.OneToOneField("ai.AIJob", on_delete=models.PROTECT, related_name="credit_reservation")
    wallet = models.ForeignKey(Wallet, on_delete=models.PROTECT, related_name="reservations")
    amount = models.PositiveIntegerField()
    state = models.CharField(max_length=12, choices=State.choices, default=State.ACTIVE)
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)


class BudgetBucket(models.Model):
    period = models.CharField(max_length=10, unique=True)
    limit_usd = models.DecimalField(max_digits=12, decimal_places=4)
    consumed_usd = models.DecimalField(max_digits=12, decimal_places=6, default=Decimal("0"))
    reserved_usd = models.DecimalField(max_digits=12, decimal_places=6, default=Decimal("0"))

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(consumed_usd__gte=0), name="budget_consumed_nonnegative"),
            models.CheckConstraint(condition=models.Q(reserved_usd__gte=0), name="budget_reserved_nonnegative"),
        ]


class BudgetReservation(models.Model):
    class State(models.TextChoices):
        ACTIVE = "active"
        CAPTURED = "captured"
        RELEASED = "released"

    # One job makes up to AI_MAX_PROVIDER_CALLS_PER_JOB paid calls, each
    # recording its own captured cost — ForeignKey, not OneToOne (spec:
    # "BudgetReservation: job/attempt, amount, state").
    job = models.ForeignKey("ai.AIJob", on_delete=models.PROTECT, related_name="budget_reservations")
    bucket = models.ForeignKey(BudgetBucket, null=True, on_delete=models.PROTECT, related_name="reservations")
    amount_usd = models.DecimalField(max_digits=12, decimal_places=6, null=True, blank=True)
    state = models.CharField(max_length=12, choices=State.choices, default=State.ACTIVE)
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)


def reserve_for_job(*, job, wallet, amount):
    """Lock wallet, reserve credits and ledger rows atomically."""
    with transaction.atomic():
        locked = Wallet.objects.select_for_update().get(pk=wallet.pk)
        if locked.available_credits < amount:
            return False
        Wallet.objects.filter(pk=locked.pk).update(reserved_credits=models.F("reserved_credits") + amount)
        reservation = CreditReservation.objects.create(job=job, wallet=locked, amount=amount)
        CreditTransaction.objects.create(
            wallet=locked,
            kind=CreditTransaction.Kind.RESERVE,
            signed_amount=-amount,
            operation_key=f"reserve:{job.pk}",
            job=job,
            reason=f"Резерв {reservation.amount} кредитов",
        )
        return True


def settle_reservation(*, job, charge):
    """Capture or release exactly once. Returns False if already settled."""
    with transaction.atomic():
        reservation = CreditReservation.objects.select_for_update().select_related("wallet").filter(job=job).first()
        if reservation is None or reservation.state != CreditReservation.State.ACTIVE:
            return False
        wallet = Wallet.objects.select_for_update().get(pk=reservation.wallet_id)
        updates = {"reserved_credits": models.F("reserved_credits") - reservation.amount}
        if charge:
            updates["total_credits"] = models.F("total_credits") - reservation.amount
        Wallet.objects.filter(pk=wallet.pk).update(**updates)
        CreditTransaction.objects.create(
            wallet=wallet,
            kind=CreditTransaction.Kind.DEBIT if charge else CreditTransaction.Kind.RELEASE,
            signed_amount=-reservation.amount if charge else reservation.amount,
            operation_key=f"{'debit' if charge else 'release'}:{job.pk}",
            job=job,
            reason="Списание за доставленный результат" if charge else "Резерв освобождён без списания",
        )
        reservation.state = CreditReservation.State.CAPTURED if charge else CreditReservation.State.RELEASED
        reservation.resolved_at = timezone.now()
        reservation.save(update_fields=["state", "resolved_at"])
        return True
