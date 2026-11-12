"""WSGI config exposing static files on loopback without a second proxy.

The deployed topology has no reverse proxy in front of Gunicorn yet, so the
static files middleware serves /static/ directly. Once nginx is agreed, this
wrapper is removed and nginx serves staticfiles/ itself.
"""

import os

from django.conf import settings
from django.contrib.staticfiles.handlers import StaticFilesHandler
from django.core.wsgi import get_wsgi_application
from django.views.static import serve as plain_serve

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.prod")

application = get_wsgi_application()


class ProductionStaticHandler(StaticFilesHandler):
    """Serve the collected (hashed) files from STATIC_ROOT.

    The stock StaticFilesHandler resolves URLs through the finders (source
    dirs), so it misses post-processed files like css/main.a05562340b6a.css
    that only exist in STATIC_ROOT after collectstatic. Serving STATIC_ROOT
    directly keeps the manifest URLs valid.
    """

    def serve(self, request):
        return plain_serve(
            request, self.file_path(request.path), document_root=settings.STATIC_ROOT
        )


handler = ProductionStaticHandler(application)
