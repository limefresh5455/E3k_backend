import unittest

from app.services.extraction_service import _correct_vat_code_misread_as_discount


class TubiNetPriceCorrectionTests(unittest.TestCase):
    def test_vat_code_49_is_not_applied_as_discount(self):
        pdf_text = (
            "Row Code Description Sizes Prcis M.U. Quantity Price Disco Amount VAT Delivery\n"
            "3 AL20A0-0250375A0A4 3 M 150,00 3,3500 502,50 49 03/09/26"
        )
        extracted = {
            "VoucherLines": [
                {
                    "Number": "AL20A0-0250375A0A4",
                    "Quantity": 150,
                    "GrossPrice": 3.35,
                    "DiscountPercent": 49,
                    "LineTotal": 256.27,
                }
            ]
        }

        result = _correct_vat_code_misread_as_discount(pdf_text, extracted)
        line = result["VoucherLines"][0]

        self.assertIsNone(line["DiscountPercent"])
        self.assertEqual(line["GrossPrice"], 3.35)
        self.assertEqual(line["LineTotal"], 502.50)

    def test_other_layouts_are_not_changed(self):
        extracted = {
            "VoucherLines": [
                {
                    "Quantity": 1,
                    "GrossPrice": 100,
                    "DiscountPercent": 49,
                    "LineTotal": 51,
                }
            ]
        }

        result = _correct_vat_code_misread_as_discount(
            "Quantity Price Discount Amount", extracted
        )

        self.assertEqual(result["VoucherLines"][0]["DiscountPercent"], 49)
        self.assertEqual(result["VoucherLines"][0]["LineTotal"], 51)


if __name__ == "__main__":
    unittest.main()
