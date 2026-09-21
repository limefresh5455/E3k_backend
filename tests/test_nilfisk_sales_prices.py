import unittest
from unittest.mock import Mock, patch

from app.services.erp_service import (
    _calculate_nilfisk_sales_price,
    _update_article_sales_price_net,
    push_to_erp,
)


class NilfiskSalesPriceCalculationTests(unittest.TestCase):
    def test_uplifts_purchase_price_below_fifty_chf(self):
        self.assertEqual(_calculate_nilfisk_sales_price(60.0, 20.0), 42.45)

    def test_keeps_published_price_at_threshold(self):
        self.assertEqual(_calculate_nilfisk_sales_price(65.0, 50.0), 65.0)

    def test_keeps_published_price_above_threshold(self):
        self.assertEqual(_calculate_nilfisk_sales_price(70.0, 50.01), 70.0)


class ErpArticleNetSalesPriceTests(unittest.TestCase):
    @patch("app.services.erp_service._erp_request")
    def test_updates_and_verifies_only_f032(self, request_mock):
        update_response = Mock(ok=True)
        update_response.json.return_value = "42"
        verify_response = Mock(ok=True)
        verify_response.json.return_value = {"F032": "25.40"}
        request_mock.side_effect = [update_response, verify_response]

        result = _update_article_sales_price_net(
            article_number="ERP-100",
            sales_price_net=25.4,
        )

        self.assertEqual(result, "42")
        self.assertEqual(
            request_mock.call_args_list[0].kwargs["json"],
            {"F001": "ERP-100", "F032": "25.40"},
        )
        self.assertNotIn("F033", request_mock.call_args_list[0].kwargs["json"])
        self.assertEqual(request_mock.call_args_list[1].kwargs["json"], ["F032"])


class NilfiskSalesPriceFlowTests(unittest.TestCase):
    def _erp_line(self):
        return {
            "Id": 7,
            "LineFlag": 1,
            "ArticleNumber": "ERP-100",
            "VoucherAddress": "NILFISK",
            "Quantity": 1.0,
        }

    def _extracted(self, supplier="Nilfisk AG"):
        return {
            "OurOrderNumber": "2601294",
            "Supplier": supplier,
            "VoucherLines": [
                {
                    "Number": "107413478",
                    "Quantity": 1.0,
                    "GrossPrice": 25.4,
                    "DiscountPercent": 40.0,
                    "LineTotal": 15.24,
                }
            ],
        }

    @patch("app.services.erp_service._update_article_sales_price_net", return_value="99")
    @patch("app.services.erp_service._update_voucher_line", return_value="11")
    @patch("app.services.erp_service._pick_best_erp_line")
    @patch("app.services.erp_service._get_purchase_order_lines")
    def test_nilfisk_uses_calculated_low_purchase_price_for_f070_and_f032(
        self,
        get_lines_mock,
        pick_line_mock,
        voucher_update_mock,
        article_update_mock,
    ):
        erp_line = self._erp_line()
        get_lines_mock.return_value = [erp_line]
        pick_line_mock.return_value = erp_line

        push_to_erp(self._extracted())

        self.assertEqual(voucher_update_mock.call_args.kwargs["unit_price"], 25.4)
        self.assertEqual(voucher_update_mock.call_args.kwargs["line_total"], 15.24)
        self.assertEqual(voucher_update_mock.call_args.kwargs["discount_percent"], 40.0)
        self.assertEqual(voucher_update_mock.call_args.kwargs["sales_price_net"], 33.47)
        article_update_mock.assert_called_once_with(
            article_number="ERP-100",
            sales_price_net=33.47,
        )

    @patch("app.services.erp_service._update_article_sales_price_net")
    @patch("app.services.erp_service._update_voucher_line", return_value="11")
    @patch("app.services.erp_service._pick_best_erp_line")
    @patch("app.services.erp_service._get_purchase_order_lines")
    def test_other_supplier_does_not_use_nilfisk_rule(
        self,
        get_lines_mock,
        pick_line_mock,
        voucher_update_mock,
        article_update_mock,
    ):
        erp_line = self._erp_line()
        get_lines_mock.return_value = [erp_line]
        pick_line_mock.return_value = erp_line

        push_to_erp(self._extracted(supplier="Other Supplier AG"))

        self.assertIsNone(voucher_update_mock.call_args.kwargs["sales_price_net"])
        article_update_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
