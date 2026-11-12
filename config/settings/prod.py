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

# With DEBUG=False Django silences everything below CRITICAL by default; route
# app errors to stderr so they land in journalctl via the systemd units.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "plain": {"format": "{levelname} {name} {message}", "style": "{"},
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "plain",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "INFO",
    },
    "loggers": {
        "django": {"level": "INFO"},
        "django.request": {"level": "ERROR"},
    },
}

# Serve static files with Gunicorn on loopback until a reverse proxy is agreed.
# Django 5.2 ignores the legacy STATICFILES_STORAGE setting; use the STORAGES dict.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.ManifestStaticFilesStorage"
    },
}
