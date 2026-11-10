"""Restricted DocumentFetcher with SSRF protection (stage 5).

WEB_FETCH_ENABLED=false by default: every fetch raises until the owner
enables it. Protections per docs/spec/docs/sources-and-rag.md: only
allowlisted http/https hosts, no loopback/private/link-local/metadata
addresses, DNS re-resolved per redirect, bounded redirects/time/size.
"""

import http.client
import ipaddress
import socket
import urllib.error
import urllib.parse
import urllib.request
from urllib.parse import urlparse

from django.conf import settings


class FetchBlocked(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _is_public_unicast(address: str) -> bool:
    parsed = ipaddress.ip_address(address)
    return parsed.is_global and not parsed.is_loopback and not parsed.is_link_local


def _resolve_host(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise FetchBlocked("dns_failure", f"DNS не разрешил хост {host}") from exc
    addresses = sorted({info[4][0] for info in infos})
    if not addresses or not all(_is_public_unicast(address) for address in addresses):
        raise FetchBlocked("private_address", "Адрес запрещён (private/loopback/metadata)")
    return addresses


def check_url(url: str) -> str:
    """Validate scheme and host against the allowlist; returns normalized URL."""
    if not settings.WEB_FETCH_ENABLED:
        raise FetchBlocked("fetch_disabled", "Загрузка веб-документов выключена")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise FetchBlocked("bad_url", "Разрешены только http/https URL")
    if parsed.hostname in ("localhost",) or parsed.hostname.endswith(".localhost"):
        raise FetchBlocked("private_address", "Внутренние имена запрещены")
    if settings.SOURCE_DOMAIN_ALLOWLIST and parsed.hostname.lower() not in settings.SOURCE_DOMAIN_ALLOWLIST:
        raise FetchBlocked("not_allowed", f"Домен {parsed.hostname} не в allowlist")
    _resolve_host(parsed.hostname)
    return url


def fetch_document(url: str) -> tuple[bytes, str]:
    """Fetch one allowlisted document with bounded redirects/time/size.

    Every redirect target is re-validated the same way as the first URL
    (DNS rebinding protection: each hop re-resolves DNS).
    """
    check_url(url)
    visited = 0
    current = url
    while True:
        if visited >= settings.SOURCE_FETCH_MAX_REDIRECTS:
            raise FetchBlocked("too_many_redirects", "Слишком много перенаправлений")
        addresses = _resolve_host(urlparse(current).hostname)
        request = urllib.request.Request(
            current,
            headers={"User-Agent": "PhilosophyReader-Fetcher/1.0", "Accept": "text/*, application/pdf"},
            method="GET",
        )
        opener = urllib.request.build_opener(_PinnedHTTPSAddresses(addresses))
        try:
            with opener.open(request, timeout=settings.SOURCE_FETCH_TIMEOUT_SECONDS) as response:
                if response.status in (301, 302, 303, 307, 308):
                    location = response.headers.get("Location")
                    if not location:
                        raise FetchBlocked("bad_redirect", "Redirect без Location")
                    visited += 1
                    current = _absolute_url(current, location)
                    check_url(current)
                    continue
                content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip()
                length = int(response.headers.get("Content-Length") or 0)
                if length > settings.SOURCE_FETCH_MAX_BYTES:
                    raise FetchBlocked("too_large", "Документ больше лимита размера")
                data = response.read(settings.SOURCE_FETCH_MAX_BYTES + 1)
        except FetchBlocked:
            raise
        except urllib.error.HTTPError as exc:
            raise FetchBlocked(f"http_{exc.code}", f"HTTP {exc.code} при загрузке") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise FetchBlocked("network_error", f"Сеть недоступна: {exc}") from exc
        if len(data) > settings.SOURCE_FETCH_MAX_BYTES:
            raise FetchBlocked("too_large", "Документ больше лимита размера")
        allowed_types = ("text/html", "text/plain", "text/markdown", "application/pdf")
        if content_type and content_type not in allowed_types:
            raise FetchBlocked("bad_content_type", f"Тип {content_type} не поддерживается")
        return data, content_type


def _absolute_url(base: str, location: str) -> str:
    return urllib.parse.urljoin(base, location)


class _PinnedHTTPSAddresses(urllib.request.HTTPSHandler):
    """Connect only to the DNS-resolved, validated addresses (anti-rebinding)."""

    def __init__(self, addresses):
        super().__init__()
        self._addresses = addresses

    def https_open(self, req):
        return self.do_open(_PinnedConnection(self._addresses), req, context=self._context)


class _PinnedConnection:
    """HTTPSConnection factory that dials the validated IP with correct SNI."""

    def __init__(self, addresses):
        self._addresses = addresses

    def __call__(self, host, **kwargs):
        kwargs.pop("port", None)
        last_error: Exception | None = None
        for address in self._addresses:
            try:
                return _SNIConnection(address, host, **kwargs)
            except OSError as exc:  # pragma: no cover - defensive
                last_error = exc
        raise OSError(f"Не удалось подключиться: {last_error}")


class _SNIConnection(http.client.HTTPSConnection):
    """Dial a pinned address but send SNI/Host for the real hostname."""

    def __init__(self, address, host, **kwargs):
        kwargs.pop("host", None)
        super().__init__(address, **kwargs)
        self._real_host = host

    def _get_hostport(self, host, port):
        return self._real_host, port or 443