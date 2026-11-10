from .base import *  # noqa: F403

DEBUG = True
SECRET_KEY = env("DJANGO_SECRET_KEY", "dev-only-insecure-key-change-me")  # noqa: F405
AI_PROVIDER = env("AI_PROVIDER", "fake")  # noqa: F405
ALLOWED_HOSTS = ["127.0.0.1", "localhost"]
INTERNAL_IPS = ["127.0.0.1"]
