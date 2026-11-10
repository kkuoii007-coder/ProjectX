import uuid

from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models


class UserManager(BaseUserManager):
    use_in_migrations = True

    def _create_user(self, login, password, **extra_fields):
        if not login:
            raise ValueError("Login is required")
        user = self.model(login=login.lower(), **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, login, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        return self._create_user(login, password, **extra_fields)

    def create_superuser(self, login, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("is_active", True)
        if not extra_fields["is_staff"] or not extra_fields["is_superuser"]:
            raise ValueError("Superuser must have is_staff and is_superuser")
        return self._create_user(login, password, **extra_fields)


class User(AbstractUser):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    username = None
    email = None
    first_name = None
    last_name = None
    login = models.CharField(max_length=32, unique=True)
    login_normalized = models.CharField(max_length=32, unique=True, editable=False)
    public_alias = models.CharField(max_length=40, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    auth_epoch = models.PositiveIntegerField(default=1)
    legal_ack_version = models.CharField(max_length=30, default="test-mode-v1")

    USERNAME_FIELD = "login"
    REQUIRED_FIELDS = []
    objects = UserManager()

    def save(self, *args, **kwargs):
        self.login = self.login.lower()
        self.login_normalized = self.login
        if not self.public_alias:
            self.public_alias = f"reader-{self.id.hex[:8]}"
        super().save(*args, **kwargs)

    def get_session_auth_hash(self):
        """Fold auth_epoch into the session hash: logout-all and password
        changes invalidate previously issued sessions."""
        import hashlib

        return hashlib.sha256(f"{self.password}${self.auth_epoch}".encode()).hexdigest()


class InviteCode(models.Model):
    code = models.CharField(max_length=64, unique=True)
    used_by = models.OneToOneField(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="invite_used")
    used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class RegistrationEvent(models.Model):
    network_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)


class LoginAttempt(models.Model):
    login = models.CharField(max_length=32, db_index=True)
    network_hash = models.CharField(max_length=64, db_index=True)
    succeeded = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
