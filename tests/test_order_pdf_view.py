import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.orders import router
from app.services.auth_service import create_token
from app.services.order_service import (
    OrderPdfNotAvailableError,
    get_order_pdf_view,
)
from app.services.pcloud_service import PCloudViewLinkError, pcloud_get_direct_url


class PCloudDirectUrlTests(unittest.TestCase):
    @patch("app.services.pcloud_service.requests.get")
    def test_builds_direct_url_from_pcloud_response(self, get_mock):
        response = Mock()
        response.json.return_value = {
            "result": 0,
            "hosts": ["c123.pcloud.com"],
            "path": "/hash/supplier-order.pdf",
        }
        get_mock.return_value = response

        result = pcloud_get_direct_url("123456789")

        self.assertEqual(result, "https://c123.pcloud.com/hash/supplier-order.pdf")
        response.raise_for_status.assert_called_once_with()

    @patch("app.services.pcloud_service.requests.get")
    def test_rejects_untrusted_download_host(self, get_mock):
        response = Mock()
        response.json.return_value = {
            "result": 0,
            "hosts": ["example.com"],
            "path": "/supplier-order.pdf",
        }
        get_mock.return_value = response

        with self.assertRaises(PCloudViewLinkError):
            pcloud_get_direct_url("123456789")


class OrderPdfViewServiceTests(unittest.TestCase):
    @patch("app.services.order_service.pcloud_get_direct_url")
    @patch("app.services.order_service.get_order")
    def test_returns_reconstructed_pcloud_view_url(self, get_order_mock, url_mock):
        get_order_mock.return_value = {
            "id": 42,
            "file_id": "123456789",
            "file_name": "supplier-order.pdf",
            "pdf_url": "https://e.pcloud.com/#page=publink&code=old&fileid=123456789",
        }
        url_mock.return_value = "https://c123.pcloud.com/direct/supplier-order.pdf"

        result = get_order_pdf_view(42)

        self.assertEqual(
            result,
            {
                "order_id": 42,
                "file_name": "supplier-order.pdf",
                "view_url": "https://c123.pcloud.com/direct/supplier-order.pdf",
            },
        )
        url_mock.assert_called_once_with("123456789")

    @patch("app.services.order_service.get_order", return_value=None)
    def test_returns_none_for_unknown_order(self, _get_order_mock):
        self.assertIsNone(get_order_pdf_view(999))

    @patch("app.services.order_service.get_order")
    def test_rejects_non_pcloud_pdf(self, get_order_mock):
        get_order_mock.return_value = {
            "id": 42,
            "file_id": "abc123",
            "file_name": "manual.pdf",
            "pdf_url": "upload://manual.pdf",
        }

        with self.assertRaises(OrderPdfNotAvailableError):
            get_order_pdf_view(42)

    @patch("app.services.order_service.get_order")
    def test_rejects_invalid_pcloud_file_id(self, get_order_mock):
        get_order_mock.return_value = {
            "id": 42,
            "file_id": "not-a-pcloud-id",
            "file_name": "supplier-order.pdf",
            "pdf_url": "https://e.pcloud.com/#page=publink",
        }

        with self.assertRaises(OrderPdfNotAvailableError):
            get_order_pdf_view(42)


class OrderPdfViewEndpointTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)

    @patch("app.api.orders.get_order_pdf_view")
    def test_endpoint_requires_authentication(self, view_mock):
        response = self.client.get("/api/orders/42/pdf-view-url")

        self.assertEqual(response.status_code, 401)
        view_mock.assert_not_called()

    @patch("app.api.orders.get_order_pdf_view")
    def test_endpoint_returns_view_metadata(self, view_mock):
        view_mock.return_value = {
            "order_id": 42,
            "file_name": "supplier-order.pdf",
            "view_url": "https://e.pcloud.com/#page=publink&code=test&fileid=123",
        }

        response = self.client.get(
            "/api/orders/42/pdf-view-url",
            headers={"Authorization": f"Bearer {create_token('test-user')}"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), view_mock.return_value)

    @patch("app.api.orders.get_order_pdf_view", return_value=None)
    def test_endpoint_returns_not_found(self, _view_mock):
        response = self.client.get(
            "/api/orders/999/pdf-view-url",
            headers={"Authorization": f"Bearer {create_token('test-user')}"},
        )

        self.assertEqual(response.status_code, 404)

    @patch(
        "app.api.orders.get_order_pdf_view",
        side_effect=OrderPdfNotAvailableError("This order's PDF is not stored in pCloud."),
    )
    def test_endpoint_rejects_non_pcloud_order(self, _view_mock):
        response = self.client.get(
            "/api/orders/42/pdf-view-url",
            headers={"Authorization": f"Bearer {create_token('test-user')}"},
        )

        self.assertEqual(response.status_code, 409)


if __name__ == "__main__":
    unittest.main()
