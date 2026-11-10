"""WSGI config exposing static files on loopback without a second proxy.

The deployed topology has no reverse proxy in front of Gunicorn yet, so the
static files middleware serves /static/ directly. Once nginx is agreed, this
wrapper is removed and nginx serves staticfiles/ itself.
"""

import os

from django.core.wsgi import get_wsgi_application
from django.contrib.staticfiles.handlers import StaticFilesHandler

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.prod")

application = get_wsgi_application()
handler = StaticFilesHandler(application)
