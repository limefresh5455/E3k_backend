from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


class FinanceLoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)


class RegisterFinanceUserRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=12, max_length=128)
    role: Literal["finance_user", "finance_admin"] = "finance_user"

    @field_validator("password")
    @classmethod
    def reject_surrounding_whitespace(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("Password must not start or end with whitespace")
        return value


class FinanceUserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: EmailStr
    role: str


class FinanceTokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: FinanceUserResponse


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class VerifyResetOtpRequest(BaseModel):
    email: EmailStr
    otp: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


class ResetGrantResponse(BaseModel):
    reset_token: str
    expires_in: int


class ResetPasswordRequest(BaseModel):
    reset_token: str = Field(min_length=1, max_length=4096)
    new_password: str = Field(min_length=12, max_length=128)

    @field_validator("new_password")
    @classmethod
    def reject_surrounding_whitespace(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("Password must not start or end with whitespace")
        return value


class MessageResponse(BaseModel):
    message: str
