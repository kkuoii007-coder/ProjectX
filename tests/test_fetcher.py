"""SSRF protection tests for the restricted DocumentFetcher."""

from unittest import mock

from django.test import TestCase, override_settings

from apps.sources.fetcher import FetchBlocked, check_url


class CheckURLTests(TestCase):
    @override_settings(WEB_FETCH_ENABLED=False)
    def test_disabled_by_default(self):
        with self.assertRaises(FetchBlocked) as caught:
            check_url("https://plato.stanford.edu/entries/plato/")
        self.assertEqual(caught.exception.code, "fetch_disabled")

    @override_settings(WEB_FETCH_ENABLED=True, SOURCE_DOMAIN_ALLOWLIST=["plato.stanford.edu"])
    def test_allowlist_enforced(self):
        check_url("https://plato.stanford.edu/entries/plato/")
        with self.assertRaises(FetchBlocked) as caught:
            check_url("https://evil.example.com/page")
        self.assertEqual(caught.exception.code, "not_allowed")

    @override_settings(WEB_FETCH_ENABLED=True, SOURCE_DOMAIN_ALLOWLIST=[])
    def test_non_http_scheme_rejected(self):
        for url in ("ftp://example.com/file", "file:///etc/passwd", "javascript:alert(1)"):
            with self.assertRaises(FetchBlocked) as caught:
                check_url(url)
            self.assertEqual(caught.exception.code, "bad_url")

    @override_settings(WEB_FETCH_ENABLED=True, SOURCE_DOMAIN_ALLOWLIST=[])
    def test_loopback_names_rejected(self):
        with self.assertRaises(FetchBlocked) as caught:
            check_url("http://localhost/admin")
        self.assertEqual(caught.exception.code, "private_address")

    @override_settings(WEB_FETCH_ENABLED=True, SOURCE_DOMAIN_ALLOWLIST=[])
    def test_private_dns_results_rejected(self):
        # Simulate DNS resolving to loopback / private / metadata addresses.
        cases = {
            "127.0.0.1": "loopback",
            "10.0.0.5": "private",
            "169.254.169.254": "link-local (cloud metadata)",
            "192.168.1.10": "private",
            "172.16.0.9": "private",
        }
        for address, _label in cases.items():
            with mock.patch("apps.sources.fetcher.socket.getaddrinfo") as resolver:
                resolver.return_value = [(2, 1, 6, "", (address, 443))]
                with self.assertRaises(FetchBlocked) as caught:
                    check_url("https://rebind.example.com/page")
                self.assertEqual(caught.exception.code, "private_address", address)


class FetchRedirectTests(TestCase):
    @override_settings(
        WEB_FETCH_ENABLED=True,
        SOURCE_DOMAIN_ALLOWLIST=[],
        SOURCE_FETCH_MAX_REDIRECTS=3,
    )
    def test_redirect_target_dns_rechecked_and_private_blocked(self):
        """Redirect hop re-resolves DNS; private result blocks the fetch."""
        from apps.sources.fetcher import fetch_document

        redirect_response = mock.MagicMock()
        redirect_response.status = 302
        redirect_response.headers = {"Location": "https://rebind.example.net/next"}

        def fake_getaddrinfo(host, *args, **kwargs):
            if host == "open.example.com":
                return [(2, 1, 6, "", ("93.184.216.34", 443))]
            # The redirect target resolves to the cloud metadata address.
            return [(2, 1, 6, "", ("169.254.169.254", 443))]

        with mock.patch("apps.sources.fetcher.socket.getaddrinfo", side_effect=fake_getaddrinfo):
            with mock.patch("apps.sources.fetcher.urllib.request.build_opener") as builder:
                builder.return_value.open.return_value.__enter__.return_value = redirect_response
                with self.assertRaises(FetchBlocked) as caught:
                    fetch_document("https://open.example.com/start")
                self.assertEqual(caught.exception.code, "private_address")

    @override_settings(
        WEB_FETCH_ENABLED=True,
        SOURCE_DOMAIN_ALLOWLIST=[],
        SOURCE_FETCH_MAX_REDIRECTS=1,
    )
    def test_redirect_limit_enforced(self):
        from apps.sources.fetcher import fetch_document

        redirect_response = mock.MagicMock()
        redirect_response.status = 302
        redirect_response.headers = {"Location": "https://open.example.com/loop"}

        def fake_getaddrinfo(host, *args, **kwargs):
            return [(2, 1, 6, "", ("93.184.216.34", 443))]

        with mock.patch("apps.sources.fetcher.socket.getaddrinfo", side_effect=fake_getaddrinfo):
            with mock.patch("apps.sources.fetcher.urllib.request.build_opener") as builder:
                builder.return_value.open.return_value.__enter__.return_value = redirect_response
                with self.assertRaises(FetchBlocked) as caught:
                    fetch_document("https://open.example.com/start")
                self.assertEqual(caught.exception.code, "too_many_redirects")