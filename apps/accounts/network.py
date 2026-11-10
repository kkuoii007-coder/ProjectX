"""Pseudonymised network attributes for abuse limits.

The hash is an HMAC with daily key rotation, so the stored value is not
reversible to an IP address and rotates out automatically after ~24 hours.
This is minimisation, not a promise of anonymity.
"""

import hashlib
import hmac

from django.conf import settings


def client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "")


def network_hash(request):
    ip = client_ip(request)
    day = timezone_day_tag()
    key_material = f"{settings.SECRET_KEY}:network-hash:{day}".encode()
    return hmac.new(key_material, ip.encode(), hashlib.sha256).hexdigest()


def timezone_day_tag():
    from django.utils import timezone

    return timezone.now().strftime("%Y%m%d")
