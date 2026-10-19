import unittest
from datetime import timedelta
from unittest.mock import MagicMock, patch
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app import config
from app.api.finance_auth import router
from app.finance_auth.dependencies import require_finance_admin
from app.finance_auth.errors import (
    FinanceAuthConfigurationError,
    InvalidCredentialsError,
    InvalidTokenError,
    PasswordPolicyError,
    UserAlreadyExistsError,
)
from app.finance_auth.email_sender import send_password_reset_otp
from app.finance_auth.security import (
    IssuedToken,
    decode_token,
    hash_password,
    issue_access_token,
    issue_password_reset_token,
    issue_refresh_token,
    otp_digest,
    token_digest,
    utc_now,
    validate_finance_auth_configuration,
    verify_otp_digest,
    verify_password,
)
from app.finance_auth.service import AuthTokens
from app.finance_auth import service


TEST_ACCESS_SECRET = "a" * 64
TEST_REFRESH_SECRET = "b" * 64
TEST_OTP_PEPPER = "c" * 64


class FinanceSecurityTests(unittest.TestCase):
    def setUp(self):
        self.patches = [
            patch.object(config, "FINANCE_JWT_ACCESS_SECRET", TEST_ACCESS_SECRET),
            patch.object(config, "FINANCE_JWT_REFRESH_SECRET", TEST_REFRESH_SECRET),
            patch.object(config, "FINANCE_OTP_PEPPER", TEST_OTP_PEPPER),
        ]
        for config_patch in self.patches:
            config_patch.start()

    def tearDown(self):
        for config_patch in reversed(self.patches):
            config_patch.stop()

    def test_password_is_argon2_hashed_and_verified(self):
        password = "correct horse battery staple"
        password_hash = hash_password(password)

        self.assertTrue(password_hash.startswith("$argon2id$"))
        self.assertNotIn(password, password_hash)
        self.assertTrue(verify_password(password, password_hash))
        self.assertFalse(verify_password("wrong-password-value", password_hash))

    def test_short_password_is_rejected(self):
        with self.assertRaises(PasswordPolicyError):
            hash_password("too-short")

    def test_access_and_refresh_tokens_are_type_isolated(self):
        user = {
            "id": 17,
            "email": "r.kurmann@tebo.ag",
            "role": "finance_user",
            "token_version": 2,
        }
        access = issue_access_token(user)
        refresh = issue_refresh_token(user)

        access_payload = decode_token(access.value, "access")
        refresh_payload = decode_token(refresh.value, "refresh")
        self.assertEqual(access_payload["sub"], "17")
        self.assertEqual(refresh_payload["sid"], str(refresh.family_id))
        with self.assertRaises(InvalidTokenError):
            decode_token(refresh.value, "access")
        with self.assertRaises(InvalidTokenError):
            decode_token(access.value, "refresh")

    def test_reset_token_cannot_be_used_as_access_token(self):
        user = {
            "id": 17,
            "email": "r.kurmann@tebo.ag",
            "role": "finance_user",
            "token_version": 2,
        }
        reset = issue_password_reset_token(user, uuid4())

        self.assertEqual(decode_token(reset.value, "password_reset")["sub"], "17")
        with self.assertRaises(InvalidTokenError):
            decode_token(reset.value, "access")

    def test_otp_digest_is_challenge_bound_and_not_plaintext(self):
        first_challenge = uuid4()
        second_challenge = uuid4()
        otp = "123456"
        digest = otp_digest(first_challenge, otp)

        self.assertNotIn(otp, digest)
        self.assertTrue(verify_otp_digest(first_challenge, otp, digest))
        self.assertFalse(verify_otp_digest(first_challenge, "654321", digest))
        self.assertFalse(verify_otp_digest(second_challenge, otp, digest))

    def test_token_digest_does_not_store_raw_refresh_token(self):
        raw_token = "sensitive-refresh-token"
        digest = token_digest(raw_token)
        self.assertNotEqual(digest, raw_token)
        self.assertEqual(len(digest), 64)

    def test_finance_secrets_must_be_independent(self):
        with patch.object(config, "FINANCE_JWT_REFRESH_SECRET", TEST_ACCESS_SECRET):
            with self.assertRaises(FinanceAuthConfigurationError) as raised:
                validate_finance_auth_configuration()
        self.assertIn("three different values", str(raised.exception))


class FinanceAuthRouterTests(unittest.TestCase):
    def setUp(self):
        test_app = FastAPI()
        test_app.include_router(router)
        self.test_app = test_app
        self.client = TestClient(test_app)

    def _tokens(self) -> AuthTokens:
        expires_at = utc_now() + timedelta(minutes=15)
        user = {
            "id": 9,
            "email": "r.kurmann@tebo.ag",
            "role": "finance_user",
        }
        return AuthTokens(
            access=IssuedToken("access.jwt", uuid4(), expires_at),
            refresh=IssuedToken("refresh.jwt", uuid4(), expires_at, uuid4()),
            user=user,
        )

    @patch("app.api.finance_auth.service.authenticate")
    def test_login_returns_access_token_and_httponly_refresh_cookie(self, authenticate):
        authenticate.return_value = self._tokens()
        with patch.object(config, "FINANCE_COOKIE_SECURE", False):
            response = self.client.post(
                "/api/finance/auth/login",
                json={
                    "email": "r.kurmann@tebo.ag",
                    "password": "correct horse battery staple",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["access_token"], "access.jwt")
        cookie = response.headers["set-cookie"].lower()
        self.assertIn("httponly", cookie)
        self.assertNotIn("refresh.jwt", response.text)

    @patch("app.api.finance_auth.service.authenticate")
    def test_login_has_generic_invalid_credentials_response(self, authenticate):
        authenticate.side_effect = InvalidCredentialsError("internal detail")
        response = self.client.post(
            "/api/finance/auth/login",
            json={"email": "unknown@example.com", "password": "any-password"},
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"], "Invalid email or password")

    @patch("app.api.finance_auth.service.register_user")
    def test_finance_admin_can_register_user(self, register_user):
        self.test_app.dependency_overrides[require_finance_admin] = lambda: {
            "id": 1,
            "email": "admin@example.com",
            "role": "finance_admin",
        }
        register_user.return_value = {
            "id": 22,
            "email": "new.user@example.com",
            "role": "finance_user",
        }
        response = self.client.post(
            "/api/finance/auth/register",
            json={
                "email": "new.user@example.com",
                "password": "correct horse battery staple",
                "role": "finance_user",
            },
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["email"], "new.user@example.com")
        self.assertNotIn("password", response.text)

    @patch("app.api.finance_auth.service.register_user")
    def test_register_returns_conflict_for_duplicate_email(self, register_user):
        self.test_app.dependency_overrides[require_finance_admin] = lambda: {
            "id": 1,
            "email": "admin@example.com",
            "role": "finance_admin",
        }
        register_user.side_effect = UserAlreadyExistsError("already exists")
        response = self.client.post(
            "/api/finance/auth/register",
            json={
                "email": "r.kurmann@tebo.ag",
                "password": "correct horse battery staple",
            },
        )

        self.assertEqual(response.status_code, 409)

    @patch("app.api.finance_auth.service.refresh_tokens")
    def test_invalid_refresh_clears_cookie(self, refresh_tokens):
        refresh_tokens.side_effect = InvalidTokenError("expired")
        self.client.cookies.set(config.FINANCE_REFRESH_COOKIE_NAME, "expired.jwt")
        with patch.object(config, "FINANCE_COOKIE_SECURE", False):
            response = self.client.post("/api/finance/auth/refresh")

        self.assertEqual(response.status_code, 401)
        self.assertIn("max-age=0", response.headers["set-cookie"].lower())

    def test_cookie_endpoint_rejects_untrusted_browser_origin(self):
        self.client.cookies.set(config.FINANCE_REFRESH_COOKIE_NAME, "refresh.jwt")
        with patch.object(config, "CORS_ALLOWED_ORIGINS", ["https://finance.example"]):
            response = self.client.post(
                "/api/finance/auth/refresh",
                headers={"Origin": "https://attacker.example"},
            )

        self.assertEqual(response.status_code, 403)

    @patch("app.api.finance_auth.send_password_reset_otp_safely")
    @patch("app.api.finance_auth.service.request_password_reset")
    def test_forgot_password_response_does_not_enumerate_accounts(
        self, request_reset, send_email
    ):
        expected_message = None
        for result in (None, ("r.kurmann@tebo.ag", "123456")):
            request_reset.return_value = result
            response = self.client.post(
                "/api/finance/auth/forgot-password",
                json={"email": "r.kurmann@tebo.ag"},
            )
            self.assertEqual(response.status_code, 202)
            if expected_message is None:
                expected_message = response.json()
            else:
                self.assertEqual(response.json(), expected_message)

        send_email.assert_called_once_with("r.kurmann@tebo.ag", "123456")


class FinanceEmailTests(unittest.TestCase):
    @patch("app.finance_auth.email_sender.smtplib.SMTP")
    def test_smtp_sender_uses_starttls_and_authentication(self, smtp_class):
        smtp_context = MagicMock()
        smtp_server = smtp_context.__enter__.return_value
        smtp_class.return_value = smtp_context
        settings = (
            patch.object(config, "SMTP_HOST", "smtp.example.test"),
            patch.object(config, "SMTP_PORT", 587),
            patch.object(config, "SMTP_USERNAME", "sender@example.test"),
            patch.object(config, "SMTP_PASSWORD", "smtp-secret"),
            patch.object(config, "SMTP_FROM_EMAIL", "sender@example.test"),
            patch.object(config, "SMTP_USE_TLS", True),
            patch.object(config, "SMTP_USE_SSL", False),
        )
        for setting in settings:
            setting.start()
        try:
            send_password_reset_otp("r.kurmann@tebo.ag", "123456")
        finally:
            for setting in reversed(settings):
                setting.stop()

        smtp_class.assert_called_once_with(
            "smtp.example.test",
            587,
            timeout=config.SMTP_TIMEOUT_SECONDS,
        )
        smtp_server.starttls.assert_called_once()
        smtp_server.login.assert_called_once_with("sender@example.test", "smtp-secret")
        message = smtp_server.send_message.call_args.args[0]
        self.assertEqual(message["To"], "r.kurmann@tebo.ag")
        self.assertIn("123456", message.get_content())


class FinanceRegistrationTests(unittest.TestCase):
    def test_non_admin_role_is_forbidden(self):
        with self.assertRaises(HTTPException) as raised:
            require_finance_admin({"role": "finance_user"})
        self.assertEqual(raised.exception.status_code, 403)

    @patch("app.finance_auth.service._audit")
    @patch("app.finance_auth.service.repository.create_finance_user")
    def test_registration_hashes_password_before_storage(self, create_user, audit):
        create_user.side_effect = lambda email, password_hash, role: {
            "id": 23,
            "email": email,
            "password_hash": password_hash,
            "role": role,
        }
        raw_password = "correct horse battery staple"
        user = service.register_user(
            "new.user@example.com",
            raw_password,
            "finance_user",
            actor={"id": 1},
            ip_address="127.0.0.1",
            user_agent="test",
        )

        self.assertNotEqual(user["password_hash"], raw_password)
        self.assertTrue(verify_password(raw_password, user["password_hash"]))
        audit.assert_called_once()


if __name__ == "__main__":
    unittest.main()
