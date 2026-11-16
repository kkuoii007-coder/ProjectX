from django.conf import settings


def features(request):
    """Feature switches that templates need on every render path."""
    return {"ask_enabled": settings.ASK_ENABLED}
