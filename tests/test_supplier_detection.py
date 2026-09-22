import unittest

from app.services.extraction_service import _apply_customer_number_supplier_override


class CustomerNumberSupplierOverrideTests(unittest.TestCase):
    def test_cleanfix_customer_number_replaces_incorrect_supplier(self):
        extracted = {"CustomerNumber": "35228", "Supplier": "Zuschlag"}

        result = _apply_customer_number_supplier_override(extracted)

        self.assertEqual(result["Supplier"], "Cleanfix Reinigungssysteme AG")

    def test_cleanfix_customer_number_tolerates_spaces(self):
        extracted = {"CustomerNumber": "35 228", "Supplier": None}

        result = _apply_customer_number_supplier_override(extracted)

        self.assertEqual(result["Supplier"], "Cleanfix Reinigungssysteme AG")

    def test_other_customer_number_does_not_change_supplier(self):
        extracted = {"CustomerNumber": "99999", "Supplier": "Another Supplier AG"}

        result = _apply_customer_number_supplier_override(extracted)

        self.assertEqual(result["Supplier"], "Another Supplier AG")


if __name__ == "__main__":
    unittest.main()
