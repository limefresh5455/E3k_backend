class FinanceAuthError(Exception):
    """Base error for Finanz-Cockpit authentication."""


class FinanceAuthConfigurationError(FinanceAuthError):
    """Raised when mandatory production configuration is missing or unsafe."""


class InvalidCredentialsError(FinanceAuthError):
    """Raised for invalid or unavailable user credentials."""


class InvalidTokenError(FinanceAuthError):
    """Raised for invalid, expired, or revoked tokens."""


class InvalidOtpError(FinanceAuthError):
    """Raised for invalid, expired, or consumed password-reset OTPs."""


class PasswordPolicyError(FinanceAuthError):
    """Raised when a password does not satisfy the password policy."""


class UserAlreadyExistsError(FinanceAuthError):
    """Raised when a Finanz-Cockpit account already uses an email address."""
