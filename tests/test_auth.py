import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from auth import (
    AuthConfig,
    Authenticator,
    LoginRateLimited,
    hash_password,
    load_password_hash,
    verify_password,
)


class PasswordTests(unittest.TestCase):
    def test_password_hash_is_salted_and_verifiable(self):
        first = hash_password("a-long-test-password", n=2**12)
        second = hash_password("a-long-test-password", n=2**12)
        self.assertNotEqual(first, second)
        self.assertTrue(verify_password("a-long-test-password", first))
        self.assertFalse(verify_password("a-different-password", first))

    def test_short_password_and_malformed_hash_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "between 12"):
            hash_password("too-short")
        self.assertFalse(verify_password("anything", "sha256$bad"))

    def test_world_readable_password_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "password-hash"
            path.write_text(hash_password("a-long-test-password", n=2**12))
            path.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "world-readable"):
                load_password_hash(str(path))

    def test_configuration_requires_a_password(self):
        with patch.dict(
            os.environ, {"CAMERA_PASSWORD_FILE": "/definitely/missing"}, clear=True
        ):
            with self.assertRaisesRegex(ValueError, "could not read"):
                AuthConfig.from_environment(secure_transport=False)

    def test_configuration_rejects_an_under_strength_hash(self):
        weak_hash = hash_password("a-long-test-password", n=2**12)
        with patch.dict(
            os.environ, {"CAMERA_PASSWORD_HASH": weak_hash}, clear=True
        ):
            with self.assertRaisesRegex(ValueError, "insufficient work factor"):
                AuthConfig.from_environment(secure_transport=False)


class AuthenticatorTests(unittest.TestCase):
    def setUp(self):
        self.authenticator = Authenticator(
            AuthConfig(
                "admin",
                hash_password("a-long-test-password", n=2**12),
                3600,
                True,
            )
        )

    def test_session_cookie_and_logout(self):
        session = self.authenticator.login(
            "admin", "a-long-test-password", "192.0.2.10"
        )
        self.assertIsNotNone(session)
        header = self.authenticator.set_cookie_header(session)
        self.assertIn("__Host-sentinel_session=", header)
        self.assertIn("Secure", header)
        self.assertIn("HttpOnly", header)
        self.assertIsNotNone(self.authenticator.session_from_cookie(header))
        self.authenticator.logout(session)
        self.assertIsNone(self.authenticator.session_from_cookie(header))

    def test_login_attempts_are_rate_limited(self):
        for _ in range(5):
            self.assertIsNone(
                self.authenticator.login("admin", "wrong-password", "192.0.2.11")
            )
        with self.assertRaises(LoginRateLimited):
            self.authenticator.login("admin", "wrong-password", "192.0.2.11")

    def test_session_expires_server_side(self):
        authenticator = Authenticator(
            AuthConfig(
                "admin",
                hash_password("a-long-test-password", n=2**12),
                0.01,
                False,
            )
        )
        session = authenticator.login(
            "admin", "a-long-test-password", "192.0.2.12"
        )
        self.assertTrue(authenticator.is_active(session))
        time.sleep(0.02)
        self.assertFalse(authenticator.is_active(session))
