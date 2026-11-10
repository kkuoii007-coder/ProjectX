from .base import *  # noqa: F403

DEBUG = False
SECRET_KEY = "test-only-secret-key"
AI_PROVIDER = "fake"
LIVE_AI_ENABLED = False
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
DATABASES["default"]["TEST"] = {"NAME": "test_philosophy_reader"}  # noqa: F405
DATABASES["default"]["CONN_MAX_AGE"] = 0  # noqa: F405
REGISTRATION_MODE = "open"
