"""Read-only Europa3000 API client for Planzer shipment discovery."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from app.services.erp_auth_alert_service import schedule_erp_unauthorized_alert

from .config import PlanzerSettings
from .schemas import DeliveryCandidate


DELIVERY_CANDIDATE_FIELDS = (
    "F055",  # delivery-note key
    "F003",  # document date
    "F011",  # delivery method
    "F023",  # total weight
    "F024",  # volume
    "F030",  # delivery address number
    "F050",  # warehouse
    "F053",  # status
    "F065",  # additional reference
    "F121",  # status per document
)


class EuropaApiError(RuntimeError):
    """Raised when Europa returns an unusable response."""


def _retrying_session() -> requests.Session:
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "POST"}),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


class EuropaClient:
    def __init__(
        self,
        settings: PlanzerSettings,
        session: requests.Session | None = None,
    ):
        self.settings = settings
        self.session = session or _retrying_session()
        self.session.headers.update({"Accept": "application/json"})
        self.session.auth = (settings.erp_username, settings.erp_password)

    def list_candidates(self) -> list[DeliveryCandidate]:
        payload = {
            "Fields": list(DELIVERY_CANDIDATE_FIELDS),
            "Filters": [
                {
                    "FieldNumber": 11,
                    "Value": self.settings.shipping_method,
                    "Combine": 1,
                    "Type": 1,
                }
            ],
            "Sortings": [{"FieldNumber": 3, "Descending": True}],
        }
        body = self._request_json(
            "POST",
            "/api/DeliveryNote/Custom",
            params={"top": self.settings.batch_size, "start": 0},
            json=payload,
        )
        if not isinstance(body, list):
            raise EuropaApiError("DeliveryNote/Custom did not return a list")
        return [DeliveryCandidate.from_api(item) for item in body]

    def get_delivery_note(self, key: str) -> dict[str, Any]:
        encoded_key = quote(key, safe="")
        body = self._request_json(
            "GET", f"/api/DeliveryNote/WithLines/Key/{encoded_key}"
        )
        if not isinstance(body, dict):
            raise EuropaApiError(f"Delivery note {key} did not return an object")
        return body

    def get_address(self, address_number: str) -> dict[str, Any]:
        encoded_number = quote(address_number, safe="")
        body = self._request_json("GET", f"/api/Address/Key/{encoded_number}")
        if not isinstance(body, dict):
            raise EuropaApiError(
                f"Address {address_number} did not return an object"
            )
        return body

    def _request_json(self, method: str, path: str, **kwargs):
        url = f"{self.settings.erp_base_url}{path}"
        try:
            response = self.session.request(
                method,
                url,
                timeout=self.settings.erp_timeout_seconds,
                **kwargs,
            )
            if response.status_code == 401:
                schedule_erp_unauthorized_alert(method, url)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as error:
            response = getattr(error, "response", None)
            detail = ""
            if response is not None:
                detail = f": {response.text[:500]}"
            raise EuropaApiError(
                f"Europa {method} {path} failed{detail}"
            ) from error
        except ValueError as error:
            raise EuropaApiError(
                f"Europa {method} {path} returned invalid JSON"
            ) from error
