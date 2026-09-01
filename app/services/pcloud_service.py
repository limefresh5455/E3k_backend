import hashlib
import os
import time
from urllib.parse import urlencode

import requests

from app.config import (
    LOCAL_PDF_FOLDER,
    PCLOUD_ACCESS_TOKEN,
    PCLOUD_API_HOST,
    PCLOUD_BASE_URL,
    PCLOUD_CODE,
)


class PCloudConfigurationError(RuntimeError):
    """Raised when a pCloud viewer URL cannot be generated safely."""


class PCloudViewLinkError(RuntimeError):
    """Raised when pCloud cannot resolve a requested file link."""


def _pcloud_get_public_metadata() -> dict:
    public_link_code = str(PCLOUD_CODE or "").strip()
    if not public_link_code:
        raise PCloudConfigurationError("PCLOUD_CODE is not configured")

    try:
        response = requests.get(
            f"{PCLOUD_BASE_URL}/showpublink",
            params={"code": public_link_code},
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as error:
        raise PCloudViewLinkError("Could not read the pCloud folder") from error

    if data.get("result") != 0:
        message = data.get("error", "unknown pCloud error")
        raise PCloudViewLinkError(f"pCloud could not read the folder: {message}")

    metadata = data.get("metadata")
    if not isinstance(metadata, dict):
        raise PCloudViewLinkError("pCloud returned invalid folder metadata")
    return metadata


def pcloud_get_folders():
    return _pcloud_get_public_metadata().get("contents", [])


def pcloud_download_pdf(file_id: str) -> bytes:
    max_retries = 3

    for attempt in range(max_retries):
        try:
            direct_url = pcloud_get_direct_url(file_id)
            file_response = requests.get(direct_url, timeout=60)

            if file_response.status_code == 200:
                return file_response.content

            raise Exception(f"Download failed with status {file_response.status_code}")
        except Exception as error:
            if attempt < max_retries - 1:
                print(f"Retry {attempt + 1} for file {file_id}: {error}")
                time.sleep(2)
                continue
            raise Exception(f"pCloud download error after retries: {error}") from error


def pcloud_get_view_url(file_id: str) -> str:
    public_link_code = str(PCLOUD_CODE or "").strip()
    if not public_link_code:
        raise PCloudConfigurationError("PCLOUD_CODE is not configured")

    query = urlencode({"code": public_link_code, "fileid": str(file_id)})
    return f"https://e.pcloud.com/#page=publink&{query}"


def _find_public_file(item: dict, file_id: str):
    if not item.get("isfolder"):
        return item if str(item.get("fileid", "")) == file_id else None
    for child in item.get("contents", []):
        if not isinstance(child, dict):
            continue
        match = _find_public_file(child, file_id)
        if match:
            return match
    return None


def pcloud_get_file_location_url(file_id: str) -> str:
    """Open the exact file in its pCloud folder without an IP-bound URL."""
    normalized_file_id = str(file_id or "").strip()
    if not normalized_file_id.isdecimal():
        raise PCloudViewLinkError("The pCloud file ID is invalid")

    metadata = _pcloud_get_public_metadata()
    file_metadata = _find_public_file(metadata, normalized_file_id)
    if not file_metadata:
        raise PCloudViewLinkError("The PDF was not found in the configured pCloud folder")

    parent_folder_id = str(file_metadata.get("parentfolderid", "")).strip()
    if not parent_folder_id.isdecimal():
        raise PCloudViewLinkError("pCloud did not return the PDF's folder location")

    query = urlencode(
        {
            "folder": parent_folder_id,
            "file": f"f{normalized_file_id}",
            "prev": "1",
        }
    )
    return f"https://e.pcloud.com/#/filemanager?{query}"


def _pcloud_authenticated_request(endpoint: str, params=None) -> dict:
    api_host = str(PCLOUD_API_HOST or "").strip().rstrip("/")
    access_token = str(PCLOUD_ACCESS_TOKEN or "").strip()
    if not api_host or not access_token:
        raise PCloudConfigurationError(
            "PCLOUD_API_HOST and PCLOUD_ACCESS_TOKEN must be configured"
        )

    try:
        response = requests.get(
            f"{api_host}/{endpoint}",
            params=params,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as error:
        raise PCloudViewLinkError("Could not create the pCloud PDF viewer") from error

    if data.get("result", 0) != 0:
        message = data.get("error", "unknown pCloud error")
        raise PCloudViewLinkError(f"pCloud could not open the PDF: {message}")
    return data


def _pcloud_file_public_url(public_link: dict) -> str:
    code = str(public_link.get("code") or "").strip()
    if not code:
        raise PCloudViewLinkError("pCloud returned an invalid PDF viewer link")
    return "https://e.pcloud.link/publink/show?" + urlencode({"code": code})


def pcloud_get_file_preview_url(file_id: str) -> str:
    """Open exactly one PDF in pCloud's standalone document viewer."""
    normalized_file_id = str(file_id or "").strip()
    if not normalized_file_id.isdecimal():
        raise PCloudViewLinkError("The pCloud file ID is invalid")

    existing_links = _pcloud_authenticated_request("listpublinks").get("publinks", [])
    for public_link in existing_links:
        if not isinstance(public_link, dict) or public_link.get("isfolder"):
            continue
        metadata = public_link.get("metadata")
        metadata_file_id = metadata.get("fileid") if isinstance(metadata, dict) else None
        linked_file_id = public_link.get("fileid", metadata_file_id)
        if str(linked_file_id or "") == normalized_file_id:
            return _pcloud_file_public_url(public_link)

    created_link = _pcloud_authenticated_request(
        "getfilepublink", params={"fileid": normalized_file_id}
    )
    return _pcloud_file_public_url(created_link)


def pcloud_get_direct_url(file_id: str) -> str:
    """Ask pCloud for a temporary URL that opens the requested file itself."""
    public_link_code = str(PCLOUD_CODE or "").strip()
    if not public_link_code:
        raise PCloudConfigurationError("PCLOUD_CODE is not configured")

    try:
        response = requests.get(
            f"{PCLOUD_BASE_URL}/getpublinkdownload",
            params={
                "code": public_link_code,
                "fileid": str(file_id),
                "contenttype": "application/pdf",
            },
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as error:
        raise PCloudViewLinkError("Could not request the PDF link from pCloud") from error

    if data.get("result") != 0:
        message = data.get("error", "unknown pCloud error")
        raise PCloudViewLinkError(f"pCloud could not open this PDF: {message}")

    hosts = data.get("hosts") or []
    path = str(data.get("path") or "")
    host = str(hosts[0]).strip().lower() if hosts else ""
    if not host.endswith(".pcloud.com") or not path.startswith("/"):
        raise PCloudViewLinkError("pCloud returned an invalid PDF link")

    return f"https://{host}{path}"


def get_local_pdfs():
    files = []
    base_path = os.path.abspath(LOCAL_PDF_FOLDER)

    if not os.path.exists(base_path):
        raise Exception(f"Folder not found: {base_path}")

    for root, _, filenames in os.walk(base_path):
        folder_name = os.path.basename(root)
        for filename in filenames:
            if not filename.lower().endswith(".pdf"):
                continue
            file_path = os.path.join(root, filename)
            file_id = hashlib.md5(file_path.encode()).hexdigest()
            files.append(
                {
                    "file_id": file_id,
                    "file_name": filename,
                    "folder_name": folder_name,
                    "file_path": file_path,
                }
            )

    return files
