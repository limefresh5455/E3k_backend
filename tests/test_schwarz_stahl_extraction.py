import unittest
from unittest.mock import patch

from app.services.erp_service import _unit_factor, push_to_erp
from app.services.extraction_service import (
    _apply_schwarz_stahl_table_validation,
    _parse_schwarz_stahl_page_words,
    build_summary,
)
from app.services.order_service import _run_pipeline


def _word(text, x0, top):
    return {"text": text, "x0": float(x0), "top": float(top)}


def _schwarz_words():
    words = [
        _word("Menge", 319, 100),
        _word("Preis", 392, 100),
        _word("Netto-Preis", 435, 100),
        _word("Betrag", 540, 100),
    ]

    def add_row(article, y, quantity, gross, net, amount, discount, description):
        words.extend(
            [
                _word("Art.", 69, y),
                _word(article, 86, y),
                _word(description, 69, y + 10),
                _word(quantity, 330, y + 10),
                _word(gross, 392, y + 10),
                _word(f"{net}/", 446, y + 10),
                _word("STK", 475, y + 10),
                _word(amount, 541, y + 10),
                _word("Basisrabatt", 69, y + 20),
                _word(f"-{discount}%", 393, y + 20),
            ]
        )

    words.extend(
        [
            _word("Art.", 69, 120),
            _word("2066991", 86, 120),
            _word("Systemrohr", 69, 130),
            _word("Stange", 118, 130),
            _word("à", 160, 130),
            _word("6m", 170, 130),
            _word("2.00STG", 330, 130),
            _word("59.50", 392, 130),
            _word("26.77/", 446, 130),
            _word("M", 475, 130),
            _word("321.30", 541, 130),
            _word("12.00M", 325, 140),
            _word("Basisrabatt", 69, 150),
            _word("-55%", 393, 150),
        ]
    )
    add_row("2066728", 170, "2.00STK", "41.50", "23.24", "46.50", "44", "T-Stück")
    add_row("2066797", 210, "3.00STK", "55.00", "30.80", "92.40", "44", "Übergang")
    add_row("2066734", 250, "5.00STK", "46.10", "25.82", "129.10", "44", "Bogen")
    add_row("2071410", 290, "8.00STK", "5.70", "4.10", "32.85", "28", "BSA")
    add_row("3005988", 330, "4.00STK", "137.00", "76.72", "306.90", "44", "Übergang")
    return words


class SchwarzStahlTableTests(unittest.TestCase):
    def test_parses_all_rows_and_meter_conversion(self):
        rows = _parse_schwarz_stahl_page_words(_schwarz_words())

        self.assertEqual(
            [row["number"] for row in rows],
            ["2066991", "2066728", "2066797", "2066734", "2071410", "3005988"],
        )
        first = rows[0]
        self.assertEqual(first["quantity"], 2.0)
        self.assertEqual(first["quantity_unit"], "STG")
        self.assertEqual(first["billing_quantity"], 12.0)
        self.assertEqual(first["billing_unit"], "M")
        self.assertEqual(first["conversion_factor"], 6.0)
        self.assertEqual(first["gross_price"], 59.50)
        self.assertEqual(first["line_total"], 321.30)
        self.assertEqual(round(sum(row["line_total"] for row in rows), 2), 929.05)

    @patch("app.services.extraction_service._extract_schwarz_stahl_table_rows")
    def test_authoritative_rows_replace_incomplete_llm_result(self, extract_mock):
        parsed_rows = _parse_schwarz_stahl_page_words(_schwarz_words())
        extract_mock.return_value = parsed_rows
        extracted = {
            "VoucherLines": [
                {"Number": "2066991", "DeliveryDate": "15.10.2026"},
                {"Number": "2071410"},
            ]
        }

        result = _apply_schwarz_stahl_table_validation(
            "Schwarz Stahl AG Netto-Preis SUMME Positionen",
            b"pdf",
            extracted,
        )

        self.assertEqual(len(result["VoucherLines"]), 6)
        self.assertEqual(result["SchwarzStahlPositionTotal"], 929.05)
        first = result["VoucherLines"][0]
        self.assertEqual(first["Einheit"], 6.0)
        self.assertEqual(first["PricingUnitFactor"], 1.0)
        self.assertEqual(first["DeliveryDate"], "15.10.2026")
        self.assertEqual(_unit_factor(first), 1.0)

        summary = build_summary(result, "schwarz.pdf", "Schwarz Stahl AG")
        self.assertEqual(summary["line_count"], 6)
        self.assertEqual(summary["total_net"], 929.05)
        self.assertEqual(summary["lines"][0]["einheit"], 6.0)
        self.assertEqual(summary["lines"][0]["billing_quantity"], 12.0)


class SchwarzStahlErpMatchingTests(unittest.TestCase):
    @patch("app.services.erp_service._update_voucher_line")
    @patch("app.services.erp_service._lookup_supplier_article_f028", return_value=None)
    @patch("app.services.erp_service._get_purchase_order_lines")
    def test_different_article_number_is_not_forced_to_erp_line(
        self,
        get_lines_mock,
        lookup_mock,
        update_mock,
    ):
        get_lines_mock.return_value = [
            {
                "Id": 1,
                "LineFlag": 1,
                "ArticleNumber": "2066991",
                "VoucherAddress": "SUPPLIER",
                "Quantity": 2,
            },
            {
                "Id": 2,
                "LineFlag": 1,
                "ArticleNumber": "2067128",
                "VoucherAddress": "SUPPLIER",
                "Quantity": 2,
            },
        ]
        extracted = {
            "OurOrderNumber": "2601510",
            "Supplier": "Schwarz Stahl AG",
            "IsSchwarzStahlOrderConfirmation": True,
            "VoucherLines": [
                {
                    "Number": "2066991",
                    "Quantity": 2,
                    "GrossPrice": 59.50,
                    "DiscountPercent": 55,
                    "LineTotal": 321.30,
                },
                {
                    "Number": "2066728",
                    "Quantity": 2,
                    "GrossPrice": 41.50,
                    "DiscountPercent": 44,
                    "LineTotal": 46.50,
                }
            ],
        }
        update_mock.return_value = "updated-1"

        result = push_to_erp(extracted)

        self.assertEqual(update_mock.call_count, 1)
        self.assertEqual(
            update_mock.call_args.kwargs["erp_article_number"],
            "2066991",
        )
        alert_types = {
            alert["type"] for alert in result["payload_sent"]["alerts"]
        }
        self.assertIn("fewer_lines_than_expected", alert_types)
        self.assertGreaterEqual(lookup_mock.call_count, 1)


class SchwarzStahlDashboardTests(unittest.TestCase):
    @patch("app.services.order_service.mark_as_processed")
    @patch("app.services.order_service._save_success")
    @patch("app.services.order_service.push_to_erp")
    @patch("app.services.order_service.extract_order_data")
    @patch("app.services.order_service.extract_text_from_bytes")
    def test_dashboard_keeps_only_existing_rule_erp_matches(
        self,
        text_mock,
        extract_mock,
        erp_mock,
        save_mock,
        mark_mock,
    ):
        text_mock.return_value = ("PDF", False)
        numbers = [
            "2066991",
            "2066728",
            "2066797",
            "2066734",
            "2071410",
            "3005988",
        ]
        extract_mock.return_value = {
            "Supplier": "Schwarz Stahl AG",
            "OurOrderNumber": "2601510",
            "Currency": "CHF",
            "VoucherLines": [
                {
                    "Number": number,
                    "Quantity": 1,
                    "GrossPrice": 10,
                    "LineTotal": 10,
                }
                for number in numbers
            ],
        }
        erp_mock.return_value = {
            "erp_record_id": "1",
            "voucher_number": "2601510",
            "supplier_number": "003989",
            "erp_article_numbers": "3989-2066991, 3989-2071410",
            "payload_sent": {
                "updated_pdf_numbers": ["2066991", "2071410"],
                "updated_line_totals": {"2066991": 10, "2071410": 10},
                "updated_line_quantities": {"2066991": 1, "2071410": 1},
                "updated_line_erp_article_numbers": {
                    "2066991": "3989-2066991",
                    "2071410": "3989-2071410",
                },
                "alerts": [
                    {
                        "type": "fewer_lines_than_expected",
                        "message": "Missing ERP matches",
                        "lines": [],
                    }
                ],
                "requires_double_check": True,
            },
        }

        result = _run_pipeline(
            b"pdf",
            "file-1",
            "schwarz.pdf",
            "manual_upload",
            "upload://schwarz.pdf",
        )

        self.assertEqual(result["status"], "attention")
        saved = save_mock.call_args.kwargs["extracted"]
        self.assertEqual(
            [line["Number"] for line in saved["VoucherLines"]],
            ["2066991", "2071410"],
        )
        self.assertEqual(
            saved["VoucherLines"][0]["ErpArticleNumber"],
            "3989-2066991",
        )
        self.assertEqual(
            saved["VoucherLines"][1]["ErpArticleNumber"],
            "3989-2071410",
        )
        mark_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
