import hashlib
import os
import time
from urllib.parse import urlencode

import requests

from app.config import LOCAL_PDF_FOLDER, PCLOUD_BASE_URL, PCLOUD_CODE


class PCloudConfigurationError(RuntimeError):
    """Raised when a pCloud viewer URL cannot be generated safely."""


class PCloudViewLinkError(RuntimeError):
    """Raised when pCloud cannot generate a direct file link."""


def pcloud_get_folders():
    response = requests.get(
        f"{PCLOUD_BASE_URL}/showpublink",
        params={"code": PCLOUD_CODE},
        timeout=30,
    )
    data = response.json()
    if data.get("result") != 0:
        raise Exception(f"pCloud API error: {data}")
    return data["metadata"]["contents"]


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

