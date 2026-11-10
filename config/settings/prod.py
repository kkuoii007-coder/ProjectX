from .base import *  # noqa: F403

DEBUG = False
if not env("DJANGO_SECRET_KEY"):  # noqa: F405
    raise RuntimeError("DJANGO_SECRET_KEY must be set in production")
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = 31536000
SECURE_SSL_REDIRECT = env_bool("DJANGO_SECURE_SSL_REDIRECT", False)  # noqa: F405
if AI_PROVIDER == "fake" and LIVE_AI_ENABLED:  # noqa: F405
    raise RuntimeError("FakeProvider cannot serve live production generation")

# Serve static files with Gunicorn on loopback until a reverse proxy is agreed.
STATICFILES_STORAGE = "django.contrib.staticfiles.storage.ManifestStaticFilesStorage"
