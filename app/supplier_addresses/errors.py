class SupplierAddressError(Exception):
    """Base error for the supplier-address feature."""


class SupplierNotFoundError(SupplierAddressError):
    pass


class SupplierConflictError(SupplierAddressError):
    pass


class AddressNotFoundError(SupplierAddressError):
    pass


class ErpSupplierSyncError(SupplierAddressError):
    """The supplier was saved locally but could not be created in ERP."""


class SupplierWorkbookError(SupplierAddressError):
    """The supplier master workbook could not be initialized or updated."""
