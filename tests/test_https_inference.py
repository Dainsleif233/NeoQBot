from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from neoqbot.app import create_app
from neoqbot.config import Settings
from neoqbot.security import (
    host_matches_allowed,
    request_appears_secure,
)


class HostMatcherTests(unittest.TestCase):
    def test_exact_match(self) -> None:
        self.assertTrue(host_matches_allowed("neoqbot.ustb.world", ["neoqbot.ustb.world"]))

    def test_wildcard_subdomain(self) -> None:
        self.assertTrue(host_matches_allowed("a.neoqbot.ustb.world", ["*.ustb.world"]))
        self.assertTrue(host_matches_allowed("b.c.ustb.world", ["*.ustb.world"]))
        self.assertFalse(host_matches_allowed("ustb.world", ["*.ustb.world"]))

    def test_ip_literal_with_allow_ip_hosts(self) -> None:
        # Any IP literal is allowed when ``allow_ip_hosts`` is on, regardless
        # of whether it appears in the configured list — mirrors the
        # pre-existing behaviour of ``HostValidationMiddleware``.
        self.assertTrue(host_matches_allowed("208.68.182.185", ["208.68.182.185"], True))
        self.assertTrue(host_matches_allowed("208.68.182.185", ["208.68.182.185"], False))
        self.assertTrue(host_matches_allowed("208.68.182.185", ["neoqbot.ustb.world"], True))
        self.assertFalse(host_matches_allowed("208.68.182.185", ["neoqbot.ustb.world"], False))

    def test_global_wildcard_only_matches_non_empty_hosts(self) -> None:
        # The original ``HostValidationMiddleware._allowed`` returns False for
        # an empty host even when ``*`` is configured — keep that contract so
        # the matcher and middleware stay aligned.
        self.assertTrue(host_matches_allowed("anything.tld", ["*"]))
        self.assertFalse(host_matches_allowed("", ["*"]))

    def test_empty_host_never_matches(self) -> None:
        self.assertFalse(host_matches_allowed("", ["anything"]))
        self.assertFalse(host_matches_allowed("", []))


class RequestAppearsSecureTests(unittest.TestCase):
    def test_https_scheme_is_secure(self) -> None:
        self.assertTrue(
            request_appears_secure(
                "https",
                "neoqbot.ustb.world",
                ["neoqbot.ustb.world"],
            )
        )

    def test_configured_domain_over_http_is_secure(self) -> None:
        self.assertTrue(
            request_appears_secure(
                "http",
                "neoqbot.ustb.world",
                ["neoqbot.ustb.world"],
            )
        )

    def test_configured_domain_with_port_over_http_is_secure(self) -> None:
        self.assertTrue(
            request_appears_secure(
                "http",
                "neoqbot.ustb.world:6688",
                ["neoqbot.ustb.world"],
            )
        )

    def test_loopback_is_not_secure_even_when_whitelisted(self) -> None:
        # ``localhost`` may appear in ``allowed_hosts`` for some operators, but
        # inferring HTTPS from it would mask real plaintext traffic.
        self.assertFalse(
            request_appears_secure(
                "http",
                "localhost:6688",
                ["localhost"],
            )
        )
        self.assertFalse(
            request_appears_secure(
                "http",
                "127.0.0.1:6688",
                ["127.0.0.1"],
            )
        )

    def test_ip_literal_is_not_secure(self) -> None:
        self.assertFalse(
            request_appears_secure(
                "http",
                "208.68.182.185",
                ["208.68.182.185"],
            )
        )

    def test_unknown_domain_is_not_secure(self) -> None:
        self.assertFalse(
            request_appears_secure(
                "http",
                "attacker.example.com",
                ["neoqbot.ustb.world"],
            )
        )

    def test_wildcard_subdomain_matches(self) -> None:
        self.assertTrue(
            request_appears_secure(
                "http",
                "admin.ustb.world",
                ["*.ustb.world"],
            )
        )

    def test_empty_host_is_not_secure(self) -> None:
        self.assertFalse(request_appears_secure("http", "", ["anything"]))


def _build_client(
    allowed_hosts: list[str], require_https: bool
) -> tuple[TestClient, tempfile.TemporaryDirectory]:
    directory = tempfile.TemporaryDirectory(dir=Path.cwd())
    settings = Settings.model_validate(
        {
            "app": {
                "require_https": require_https,
                "allowed_hosts": allowed_hosts,
                "allow_ip_hosts": True,
                "database_path": str(Path(directory.name) / "test.db"),
                "message_archive_path": str(Path(directory.name) / "messages"),
            },
            "gui": {"enabled": False},
            "qq": {"bots": []},
            "feishu": {"bots": []},
        }
    )
    app = create_app(settings)
    return TestClient(app), directory


class RequireHttpsMiddlewareTests(unittest.TestCase):
    """End-to-end checks that go through the real middleware chain.

    Note on ordering: Starlette wraps middleware added via ``@app.middleware``
    (used by ``security_headers``) *outside* of those registered via
    ``app.add_middleware`` (used by ``HostValidationMiddleware``), so the
    HTTPS check fires before host validation. Functionally both reject the
    request, so the order is harmless — the tests below assert the actual
    response codes.

    ``fastapi.testclient.TestClient`` always sends ``Host: testserver`` and
    cannot be coerced into sending a custom Host header, so the integration
    tests configure ``allowed_hosts`` to include ``testserver`` where they
    need to reach the route handler. Edge cases for the Host validator and
    the loopback / IP-literal filters are covered by the unit tests above.
    """

    def test_whitelisted_host_over_http_is_allowed(self) -> None:
        client, directory = _build_client(["testserver"], require_https=True)
        try:
            response = client.get("/gui/")
            self.assertEqual(response.status_code, 404)  # GUI disabled, but past security checks
        finally:
            client.close()
            directory.cleanup()

    def test_unknown_host_over_http_is_rejected_with_426(self) -> None:
        # ``testserver`` isn't in the configured list, so the HTTPS check
        # can't confirm the request is secure — it returns 426 before the
        # Host validator ever runs.
        client, directory = _build_client(["neoqbot.ustb.world"], require_https=True)
        try:
            response = client.get("/gui/")
            self.assertEqual(response.status_code, 426)
            self.assertEqual(
                response.json(),
                {"detail": "HTTPS is required for the management interface"},
            )
        finally:
            client.close()
            directory.cleanup()

    def test_invalid_host_with_https_disabled_is_rejected_with_400(self) -> None:
        # With ``require_https`` off, the HTTPS check is a no-op and the Host
        # validator gets to reject the request with 400.
        client, directory = _build_client(["neoqbot.ustb.world"], require_https=False)
        try:
            response = client.get("/gui/")
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.text, "Invalid host header")
        finally:
            client.close()
            directory.cleanup()

    def test_require_https_disabled_emits_no_hsts_on_plain_http(self) -> None:
        client, directory = _build_client(["testserver"], require_https=False)
        try:
            response = client.get("/healthz")
            self.assertEqual(response.status_code, 200)
            # HSTS is only emitted for *actual* HTTPS, even when the Host
            # header would let us infer it — emitting it on inferred HTTPS
            # would lock users out if the operator's reverse proxy is
            # misconfigured.
            self.assertNotIn("Strict-Transport-Security", response.headers)
        finally:
            client.close()
            directory.cleanup()


if __name__ == "__main__":
    unittest.main()
