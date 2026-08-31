"""Authenticated pCloud client used only by the email upload job."""

import re
from dataclasses import dataclass
from pathlib import Path

import requests


class PCloudError(RuntimeError):
    """Raised for transport or pCloud API errors."""


@dataclass(frozen=True)
class UploadResult:
    status: str
    file_id: int
    cloud_path: str
    size: int


def safe_cloud_name(value: str, fallback: str = "Unknown", max_bytes: int = 200) -> str:
    """Remove path separators and control characters from a pCloud item name."""
    cleaned = re.sub(r"[\\/\x00-\x1f\x7f]", "_", value).strip().strip(".")
    cleaned = cleaned or fallback
    encoded = cleaned.encode("utf-8")
    if len(encoded) <= max_bytes:
        return cleaned
    suffix = Path(cleaned).suffix[:16]
    suffix_bytes = suffix.encode("utf-8")
    stem_budget = max(1, max_bytes - len(suffix_bytes))
    stem = encoded[:stem_budget].decode("utf-8", errors="ignore").rstrip()
    return (stem + suffix) or fallback


class PCloudClient:
    def __init__(
        self,
        api_host: str,
        access_token: str,
        root_folder_id: int,
        timeout: int = 60,
        session=None,
    ):
        self.api_host = api_host.rstrip("/")
        self.root_folder_id = root_folder_id
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {access_token}"})
        self._folder_cache: dict[str, int] = {}

    def test_connection(self) -> dict:
        return self._json_request("GET", "userinfo")

    def get_supplier_folder(self, supplier_name: str) -> int:
        folder_name = safe_cloud_name(supplier_name, "Unknown Supplier")
        if folder_name in self._folder_cache:
            return self._folder_cache[folder_name]
        data = self._json_request(
            "POST",
            "createfolderifnotexists",
            data={"folderid": self.root_folder_id, "name": folder_name},
        )
        try:
            folder_id = int(data["metadata"]["folderid"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PCloudError("pCloud folder response did not contain a folder ID") from exc
        self._folder_cache[folder_name] = folder_id
        return folder_id

    def list_tree(self) -> dict:
        return self._json_request(
            "GET",
            "listfolder",
            params={"folderid": self.root_folder_id, "recursive": 1},
        )["metadata"]

    def checksums(self, file_id: int) -> dict:
        return self._json_request("GET", "checksumfile", params={"fileid": file_id})

    def upload_pdf(
        self,
        file_path: Path,
        supplier_name: str,
        cloud_filename: str,
        sha1: str,
        sha256: str = "",
    ) -> UploadResult:
        folder_id = self.get_supplier_folder(supplier_name)
        cloud_filename = safe_cloud_name(cloud_filename, "attachment.pdf")
        existing = self._find_named_file(folder_id, cloud_filename)
        if existing:
            return self._resolve_existing(existing, sha1, sha256)

        try:
            with file_path.open("rb") as file_handle:
                data = self._json_request(
                    "POST",
                    "uploadfile",
                    data={
                        "folderid": folder_id,
                        "filename": cloud_filename,
                        "nopartial": 1,
                    },
                    files={"file": (cloud_filename, file_handle, "application/pdf")},
                )
        except (requests.Timeout, requests.ConnectionError) as exc:
            existing = self._find_named_file(folder_id, cloud_filename)
            if existing:
                return self._resolve_existing(existing, sha1, sha256)
            raise PCloudError(
                "pCloud upload outcome is unknown after a network failure; "
                "the next scheduled run will reconcile it"
            ) from exc

        try:
            metadata = data["metadata"][0]
            if not isinstance(metadata, dict):
                raise TypeError("file metadata was not an object")
        except (KeyError, IndexError, TypeError) as exc:
            raise PCloudError("pCloud upload response was missing file metadata") from exc

        try:
            file_id = int(metadata["fileid"])
            remote_size = int(metadata["size"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PCloudError(
                "pCloud upload response was missing required file ID or size metadata"
            ) from exc

        # pCloud may omit the optional full path when the destination is
        # identified by folder ID. The folder ID above already controls where
        # the file is stored, so use the returned/sent name for local history.
        cloud_path = str(
            metadata.get("path") or metadata.get("name") or cloud_filename
        )

        local_size = file_path.stat().st_size
        if remote_size != local_size:
            raise PCloudError(f"pCloud size verification failed ({remote_size} != {local_size})")
        return UploadResult("uploaded", file_id, cloud_path, remote_size)

    def _find_named_file(self, folder_id: int, filename: str):
        data = self._json_request("GET", "listfolder", params={"folderid": folder_id})
        contents = data.get("metadata", {}).get("contents", [])
        return next(
            (
                item
                for item in contents
                if not item.get("isfolder")
                and str(item.get("name", "")).casefold() == filename.casefold()
            ),
            None,
        )

    def _resolve_existing(
        self,
        metadata: dict,
        local_sha1: str,
        local_sha256: str = "",
    ) -> UploadResult:
        file_id = int(metadata["fileid"])
        checksums = self.checksums(file_id)
        remote_sha256 = checksums.get("sha256", "").lower()
        if local_sha256 and remote_sha256 and remote_sha256 != local_sha256.lower():
            raise PCloudError(
                f"A different file already exists at {metadata.get('path', metadata.get('name'))}"
            )
        if checksums.get("sha1", "").lower() != local_sha1.lower():
            raise PCloudError(
                f"A different file already exists at {metadata.get('path', metadata.get('name'))}"
            )
        return UploadResult(
            "duplicate",
            file_id,
            str(metadata.get("path", metadata.get("name", ""))),
            int(metadata.get("size", 0)),
        )

    def _json_request(self, method: str, endpoint: str, **kwargs) -> dict:
        try:
            response = self.session.request(
                method,
                f"{self.api_host}/{endpoint}",
                timeout=self.timeout,
                **kwargs,
            )
            response.raise_for_status()
            data = response.json()
        except (requests.Timeout, requests.ConnectionError):
            raise
        except (requests.RequestException, ValueError) as exc:
            raise PCloudError(f"pCloud request '{endpoint}' failed") from exc
        if not isinstance(data, dict):
            raise PCloudError(f"pCloud request '{endpoint}' returned invalid JSON")
        result = data.get("result", 0)
        if result != 0:
            error = data.get("error", "unknown pCloud error")
            raise PCloudError(f"pCloud request '{endpoint}' failed ({result}): {error}")
        return data

    def close(self) -> None:
        self.session.close()
