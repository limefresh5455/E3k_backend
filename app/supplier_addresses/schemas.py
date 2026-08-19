from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _strip_optional(value):
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


class AddressFields(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    department_attention: Optional[str] = Field(None, max_length=255)
    street: Optional[str] = Field(None, max_length=255)
    postal_code: Optional[str] = Field(None, max_length=32)
    city: Optional[str] = Field(None, max_length=255)
    country: Optional[str] = Field(None, max_length=100)
    business_phone: Optional[str] = Field(None, max_length=100)
    private_phone: Optional[str] = Field(None, max_length=100)
    mobile: Optional[str] = Field(None, max_length=100)
    fax: Optional[str] = Field(None, max_length=100)
    email: Optional[str] = Field(None, max_length=320)

    _normalize_blanks = field_validator("*", mode="before")(_strip_optional)


class AddressCreate(AddressFields):
    pass


class AddressUpdate(AddressFields):
    id: Optional[int] = Field(None, ge=1)


class SupplierCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    supplier_number: str = Field(..., min_length=1, max_length=64)
    name: Optional[str] = Field(None, max_length=255)
    code_1: Optional[str] = Field(None, max_length=50)
    code_2: Optional[str] = Field(None, max_length=50)
    code_3: Optional[str] = Field(None, max_length=50)
    code_4: Optional[str] = Field(None, max_length=50)
    addresses: list[AddressCreate] = Field(default_factory=list, max_length=100)

    _normalize_blanks = field_validator("name", "code_1", "code_2", "code_3", "code_4", mode="before")(
        _strip_optional
    )


class SupplierUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    supplier_number: Optional[str] = Field(None, min_length=1, max_length=64)
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    code_1: Optional[str] = Field(None, max_length=50)
    code_2: Optional[str] = Field(None, max_length=50)
    code_3: Optional[str] = Field(None, max_length=50)
    code_4: Optional[str] = Field(None, max_length=50)
    addresses: Optional[list[AddressUpdate]] = Field(None, max_length=100)

    _normalize_blanks = field_validator("code_1", "code_2", "code_3", "code_4", mode="before")(
        _strip_optional
    )

    @field_validator("supplier_number", "addresses", mode="before")
    @classmethod
    def reject_null_required_updates(cls, value):
        if value is None:
            raise ValueError("field cannot be null when provided")
        return value


class AddressResponse(AddressFields):
    id: int
    position: int
    created_at: datetime
    updated_at: datetime


class SupplierResponse(BaseModel):
    supplier_number: str
    name: Optional[str]
    code_1: Optional[str]
    code_2: Optional[str]
    code_3: Optional[str]
    code_4: Optional[str]
    addresses: list[AddressResponse]
    created_at: datetime
    updated_at: datetime


class SupplierCreateResponse(SupplierResponse):
    message: str
    erp_synced: bool


class SupplierListItem(BaseModel):
    supplier_number: str
    name: Optional[str]
    code_1: Optional[str]
    code_2: Optional[str]
    code_3: Optional[str]
    code_4: Optional[str]
    address_count: int
    updated_at: datetime


class SupplierListResponse(BaseModel):
    items: list[SupplierListItem]
    total: int


class SupplierUploadResponse(BaseModel):
    message: str
    rows_read: int
    suppliers_in_file: int
    suppliers_added: int
    suppliers_already_present: int
    addresses_added: int
    erp_synced: int
    erp_failed: int
    erp_failed_supplier_numbers: list[str]
    excel_appended: int
    excel_already_present: int
