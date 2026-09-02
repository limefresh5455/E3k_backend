import importlib
import unittest
from unittest.mock import AsyncMock, Mock, patch

from app.services.erp_service import _update_article_sales_prices, push_to_erp
from app.services.extraction_service import (
    _apply_mcc_table_validation,
    _calculate_mcc_sales_prices,
    _is_mcc_order_confirmation,
    _mcc_line_total_matches,
)
from app.services.order_service import _run_pipeline


MCC_TEXT = "MCC Millennium Coupling Netto Preis Gesamtpreis Preis/ME"
sync_api = importlib.import_module("app.api.sync")


class MccSalesPriceCalculationTests(unittest.TestCase):
    def test_recognizes_spacing_variations_in_mcc_headers(self):
        self.assertTrue(
            _is_mcc_order_confirmation(
                "MCC  Millennium Coupling NettoPreis Gesamt Preis Preis / ME"
            )
        )

    def test_uses_supplied_55_percent_discount(self):
        result = _calculate_mcc_sales_prices(10.0, 4.5, 55.0)

        self.assertEqual(result["discount_percent"], 55.0)
        self.assertFalse(result["default_discount_applied"])
        self.assertFalse(result["discount_differs_from_default"])
        self.assertEqual(result["sales_price_net"], 18.0)
        self.assertEqual(result["sales_price_gross"], 19.5)

    def test_defaults_to_55_percent_when_discount_is_missing(self):
        result = _calculate_mcc_sales_prices(10.0, 4.5, None)

        self.assertEqual(result["discount_percent"], 55.0)
        self.assertTrue(result["default_discount_applied"])
        self.assertEqual(result["sales_price_net"], 18.0)

    def test_uses_valid_non_default_discount_and_marks_it(self):
        result = _calculate_mcc_sales_prices(10.0, 5.0, 50.0)

        self.assertEqual(result["discount_percent"], 50.0)
        self.assertTrue(result["discount_differs_from_default"])
        self.assertEqual(result["sales_price_net"], 20.0)
        self.assertEqual(result["sales_price_gross"], 21.7)

    def test_rejects_discount_that_does_not_match_printed_net_price(self):
        self.assertIsNone(_calculate_mcc_sales_prices(10.0, 5.0, 55.0))

    def test_rounds_each_sales_price_up_to_ten_rappen(self):
        result = _calculate_mcc_sales_prices(8.233333, 3.705, 55.0)

        self.assertEqual(result["sales_price_net"], 14.9)
        self.assertEqual(result["sales_price_gross"], 16.2)

    def test_uses_exact_discounted_price_not_rounded_printed_net(self):
        result = _calculate_mcc_sales_prices(5.5567, 2.50, 55.0)

        self.assertAlmostEqual(result["purchase_net_price"], 2.500515)
        self.assertEqual(result["sales_price_net"], 10.1)
        self.assertEqual(result["sales_price_gross"], 11.0)

    def test_matches_client_sfbr25s_example(self):
        result = _calculate_mcc_sales_prices(6.54, 2.94, 55.0)

        self.assertAlmostEqual(result["purchase_net_price"], 2.943)
        self.assertEqual(result["sales_price_net"], 11.8)
        self.assertEqual(result["sales_price_gross"], 12.8)

    def test_validates_pdf_total_from_exact_discounted_price(self):
        self.assertTrue(_mcc_line_total_matches(2, 2.943, 5.89))
        self.assertFalse(_mcc_line_total_matches(2, 2.943, 5.88))

    def test_does_not_use_displayed_unit_price_for_larger_quantities(self):
        # Exact: 6.57 * 45% = 2.9565; 25 pieces total 73.91.
        self.assertTrue(_mcc_line_total_matches(25, 2.9565, 73.91))
        self.assertFalse(_mcc_line_total_matches(25, 2.96, 73.91))


class MccValidatedLineTests(unittest.TestCase):
    @patch("app.services.extraction_service._extract_mcc_table_rows")
    def test_adds_sales_prices_only_to_verified_mcc_line(self, rows_mock):
        rows_mock.return_value = [
            {
                "number": "MCC-1",
                "quantity": 2.0,
                "unit": "Stk",
                "gross_price": 10.0,
                "net_price": 4.5,
                "discount_percent": 55.0,
                "default_discount_applied": False,
                "discount_differs_from_default": False,
                "purchase_net_price": 4.5,
                "sales_price_net": 18.0,
                "sales_price_gross": 19.5,
                "line_total": 9.0,
            }
        ]
        extracted = {"VoucherLines": [{"Number": "MCC-1"}]}

        result = _apply_mcc_table_validation(MCC_TEXT, b"pdf", extracted)

        line = result["VoucherLines"][0]
        self.assertTrue(result["IsMccOrderConfirmation"])
        self.assertEqual(line["MccSalesPriceNet"], 18.0)
        self.assertEqual(line["MccSalesPriceGross"], 19.5)

    @patch("app.services.extraction_service._extract_mcc_table_rows")
    def test_does_not_parse_or_modify_non_mcc_document(self, rows_mock):
        extracted = {"VoucherLines": [{"Number": "OTHER-1"}]}

        result = _apply_mcc_table_validation("Another supplier", b"pdf", extracted)

        rows_mock.assert_not_called()
        self.assertNotIn("MccSalesPriceNet", result["VoucherLines"][0])

    @patch("app.services.extraction_service._extract_mcc_table_rows")
    def test_keeps_freight_order_values_but_excludes_sales_prices(self, rows_mock):
        rows_mock.return_value = [
            {
                "number": "VERSCH",
                "quantity": 1.0,
                "unit": "Stk",
                "gross_price": 10.0,
                "net_price": 4.5,
                "discount_percent": 55.0,
                "default_discount_applied": False,
                "discount_differs_from_default": False,
                "purchase_net_price": 4.5,
                "sales_price_net": 18.0,
                "sales_price_gross": 19.5,
                "line_total": 4.5,
            }
        ]
        extracted = {"VoucherLines": [{"Number": "VERSCH"}]}

        result = _apply_mcc_table_validation(MCC_TEXT, b"pdf", extracted)

        line = result["VoucherLines"][0]
        self.assertEqual(line["GrossPrice"], 10.0)
        self.assertTrue(line["MccSalesPriceExcluded"])
        self.assertNotIn("MccSalesPriceNet", line)
        self.assertNotIn("MccSalesPriceGross", line)


class ErpArticleSalesPriceTests(unittest.TestCase):
    @patch("app.services.erp_service._erp_request")
    def test_updates_article_net_and_gross_fields(self, request_mock):
        update_response = Mock(ok=True)
        update_response.json.return_value = "42"
        verify_response = Mock(ok=True)
        verify_response.json.return_value = {"F032": "18.00", "F033": "19.50"}
        request_mock.side_effect = [update_response, verify_response]

        result = _update_article_sales_prices(
            article_number="121-A20",
            sales_price_net=18.0,
            sales_price_gross=19.5,
        )

        self.assertEqual(result, "42")
        self.assertEqual(request_mock.call_count, 2)
        _, update_url = request_mock.call_args_list[0].args
        self.assertTrue(update_url.endswith("/api/Article/Update"))
        self.assertEqual(
            request_mock.call_args_list[0].kwargs["json"],
            {"F001": "121-A20", "F032": "18.00", "F033": "19.50"},
        )
        _, verify_url = request_mock.call_args_list[1].args
        self.assertTrue(verify_url.endswith("/api/Article/Key/121-A20"))
        self.assertEqual(request_mock.call_args_list[1].kwargs["json"], ["F032", "F033"])

    @patch("app.services.erp_service._erp_request")
    def test_treats_zero_record_id_as_failed_update(self, request_mock):
        response = Mock(ok=True)
        response.json.return_value = "0"
        request_mock.return_value = response

        with self.assertRaisesRegex(Exception, "invalid sales-price update result"):
            _update_article_sales_prices(
                article_number="121-A20",
                sales_price_net=18.0,
                sales_price_gross=19.5,
            )

    @patch("app.services.erp_service._erp_request")
    def test_rejects_unexpected_success_response(self, request_mock):
        response = Mock(ok=True)
        response.json.return_value = None
        request_mock.return_value = response

        with self.assertRaisesRegex(Exception, "invalid sales-price update result"):
            _update_article_sales_prices(
                article_number="121-A20",
                sales_price_net=18.0,
                sales_price_gross=19.5,
            )

    @patch("app.services.erp_service._erp_request")
    def test_rejects_read_after_write_mismatch(self, request_mock):
        update_response = Mock(ok=True)
        update_response.json.return_value = "42"
        verify_response = Mock(ok=True)
        verify_response.json.return_value = {"F032": "18.00", "F033": "19.40"}
        request_mock.side_effect = [update_response, verify_response]

        with self.assertRaisesRegex(Exception, "verification mismatch"):
            _update_article_sales_prices(
                article_number="121-A20",
                sales_price_net=18.0,
                sales_price_gross=19.5,
            )


class ErpMccSalesPriceFlowTests(unittest.TestCase):
    def _extracted(self, *, is_mcc=True, include_prices=True):
        line = {
            "Number": "MCC-1",
            "Quantity": 2.0,
            "GrossPrice": 10.0,
            "DiscountPercent": 55.0,
            "LineTotal": 9.0,
        }
        if include_prices:
            line.update({"MccSalesPriceNet": 18.0, "MccSalesPriceGross": 19.5})
        return {
            "OurOrderNumber": "2601001",
            "Supplier": "MCC Millennium Coupling",
            "IsMccOrderConfirmation": is_mcc,
            "VoucherLines": [line],
        }

    @patch("app.services.erp_service._update_article_sales_prices")
    @patch("app.services.erp_service._update_voucher_line", return_value="11")
    @patch("app.services.erp_service._pick_best_erp_line")
    @patch("app.services.erp_service._get_purchase_order_lines")
    def test_mcc_flow_updates_matched_article_prices(
        self, get_lines_mock, pick_line_mock, _voucher_update_mock, article_update_mock
    ):
        erp_line = {
            "Id": 7,
            "LineFlag": 1,
            "ArticleNumber": "ERP-100",
            "VoucherAddress": "MCC",
            "Quantity": 2.0,
        }
        get_lines_mock.return_value = [erp_line]
        pick_line_mock.return_value = erp_line
        article_update_mock.return_value = "99"

        extracted = self._extracted()
        extracted["Currency"] = "EUR"
        result = push_to_erp(extracted)

        article_update_mock.assert_called_once_with(
            article_number="ERP-100",
            sales_price_net=18.0,
            sales_price_gross=19.5,
        )
        self.assertEqual(
            result["payload_sent"]["mcc_sales_price_updates"]["ERP-100"]["erp_record_id"],
            "99",
        )

    @patch("app.services.erp_service._update_article_sales_prices")
    @patch("app.services.erp_service._update_voucher_line", return_value="11")
    @patch("app.services.erp_service._pick_best_erp_line")
    @patch("app.services.erp_service._get_purchase_order_lines")
    def test_non_mcc_flow_never_updates_article_prices(
        self, get_lines_mock, pick_line_mock, _voucher_update_mock, article_update_mock
    ):
        erp_line = {
            "Id": 7,
            "LineFlag": 1,
            "ArticleNumber": "ERP-100",
            "VoucherAddress": "OTHER",
            "Quantity": 2.0,
        }
        get_lines_mock.return_value = [erp_line]
        pick_line_mock.return_value = erp_line

        push_to_erp(self._extracted(is_mcc=False))

        article_update_mock.assert_not_called()

    @patch("app.services.erp_service._update_article_sales_prices")
    @patch("app.services.erp_service._update_voucher_line", return_value="11")
    @patch("app.services.erp_service._pick_best_erp_line")
    @patch("app.services.erp_service._get_purchase_order_lines")
    def test_mcc_freight_line_does_not_update_article_sales_prices(
        self, get_lines_mock, pick_line_mock, _voucher_update_mock, article_update_mock
    ):
        erp_line = {
            "Id": 7,
            "LineFlag": 1,
            "ArticleNumber": "FREIGHT",
            "VoucherAddress": "MCC",
            "Quantity": 1.0,
        }
        get_lines_mock.return_value = [erp_line]
        pick_line_mock.return_value = erp_line
        extracted = self._extracted(include_prices=False)
        extracted["VoucherLines"][0].update(
            {"Number": "VERSCH", "MccSalesPriceExcluded": True}
        )

        result = push_to_erp(extracted)

        article_update_mock.assert_not_called()
        alert_types = {
            alert["type"] for alert in result["payload_sent"].get("alerts", [])
        }
        self.assertNotIn("mcc_sales_price_validation_failed", alert_types)

class MccOrderPipelineFailureTests(unittest.TestCase):
    @patch("app.services.order_service.mark_as_processed")
    @patch("app.services.order_service._save_success")
    @patch("app.services.order_service.build_summary", return_value={})
    @patch("app.services.order_service.push_to_erp")
    @patch("app.services.order_service.extract_order_data")
    @patch("app.services.order_service.extract_text_from_bytes", return_value=("PDF", False))
    def test_mcc_erp_price_failure_is_attention_and_retryable(
        self,
        _text_mock,
        extract_mock,
        push_mock,
        _summary_mock,
        _save_mock,
        processed_mock,
    ):
        extract_mock.return_value = {
            "OurOrderNumber": "2601001",
            "Supplier": "MCC",
            "VoucherLines": [],
        }
        push_mock.return_value = {
            "erp_record_id": "11",
            "voucher_number": "2601001",
            "supplier_number": "MCC",
            "erp_article_numbers": "ERP-100",
            "payload_sent": {
                "alerts": [
                    {
                        "type": "mcc_sales_price_update_failed",
                        "message": "ERP price write failed",
                        "lines": [],
                    }
                ],
                "requires_double_check": True,
            },
        }

        result = _run_pipeline(b"pdf", "1", "mcc.pdf", "MCC", "url")

        self.assertEqual(result["status"], "attention")
        self.assertTrue(result["retry_required"])
        processed_mock.assert_not_called()


class SyncProcessingOrderTests(unittest.IsolatedAsyncioTestCase):
    async def test_pcloud_files_are_processed_in_displayed_order(self):
        folders = [
            {
                "isfolder": True,
                "name": "MCC",
                "contents": [
                    {"isfolder": False, "name": "first.pdf", "fileid": 1},
                    {"isfolder": False, "name": "second.pdf", "fileid": 2},
                ],
            }
        ]
        processed = []

        def process_file(file_id, file_name, folder_name):
            processed.append((file_id, file_name, folder_name))
            return {"status": "success"}

        with (
            patch.object(sync_api, "OPENAI_API_KEY", "test-key"),
            patch.object(sync_api, "LOCAL_PDF_MODE", False),
            patch.object(sync_api, "get_pcloud_folders", AsyncMock(return_value=folders)),
            patch.object(sync_api, "is_already_processed", return_value=False),
            patch.object(sync_api, "process_file", side_effect=process_file),
        ):
            result = await sync_api.sync_pcloud()

        self.assertEqual(
            processed,
            [("1", "first.pdf", "MCC"), ("2", "second.pdf", "MCC")],
        )
        self.assertEqual(result["success"], 2)


if __name__ == "__main__":
    unittest.main()
