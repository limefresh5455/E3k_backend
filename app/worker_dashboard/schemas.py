"""HTTP request/response schemas for dashboard sessions and administration."""

from typing import Literal

from pydantic import (
    BaseModel,
    EmailStr,
    Field,
    computed_field,
    field_validator,
    model_validator,
)


class DashboardLoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)


class EntitlementInput(BaseModel):
    permission: Literal["worker.read", "finance.read", "users.manage"]
    company_scope: Literal["tebo", "ssb", "*"]

    @model_validator(mode="after")
    def valid_pair(self) -> "EntitlementInput":
        if self.permission == "users.manage" and self.company_scope != "*":
            raise ValueError("users.manage requires the global scope")
        if self.permission != "users.manage" and self.company_scope == "*":
            raise ValueError("read permissions require tebo or ssb scope")
        return self


class DashboardUserResponse(BaseModel):
    id: int
    email: EmailStr
    display_name: str
    entitlements: list[EntitlementInput]
    is_active: bool = True

    @computed_field
    @property
    def role(self) -> Literal["admin", "user"]:
        if any(
            item.permission == "users.manage" and item.company_scope == "*"
            for item in self.entitlements
        ):
            return "admin"
        return "user"


class CreateDashboardUserRequest(BaseModel):
    email: EmailStr
    display_name: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=12, max_length=128)
    entitlements: list[EntitlementInput] = Field(min_length=1, max_length=6)

    @field_validator("display_name", "password")
    @classmethod
    def no_surrounding_whitespace(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("value must not start or end with whitespace")
        return value


class UpdateDashboardUserRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=200)
    entitlements: list[EntitlementInput] = Field(max_length=6)

    @field_validator("display_name")
    @classmethod
    def no_surrounding_whitespace(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("value must not start or end with whitespace")
        return value


class SetUserActiveRequest(BaseModel):
    active: bool


class DashboardSessionResponse(BaseModel):
    user: DashboardUserResponse


class DashboardTokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
    user: DashboardUserResponse


class PublishSummaryResponse(BaseModel):
    published: int
    unchanged: int
    rejected: int
    errors: list[str]


class DashboardCompanyConfig(BaseModel):
    id: Literal["tebo", "ssb"]
    label: str
    workerDataUrl: str
    financeDataUrl: str | None


class DashboardConfigResponse(BaseModel):
    environment: Literal["sample", "production"]
    pollIntervalMs: int
    maxStaleMinutes: int
    companies: list[DashboardCompanyConfig]


class DashboardStatusResponse(BaseModel):
    audience: Literal["worker", "finance"]
    company: Literal["tebo", "ssb"]
    status: str
    source_as_of: str | None = None
    source_exported_at: str | None = None
    published_at: str | None = None
