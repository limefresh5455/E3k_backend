import hashlib
import os
import time

import requests

from app.config import LOCAL_PDF_FOLDER, PCLOUD_BASE_URL, PCLOUD_CODE


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
            response = requests.get(
                f"{PCLOUD_BASE_URL}/getpublinkdownload",
                params={"code": PCLOUD_CODE, "fileid": file_id},
                timeout=30,
            )
            data = response.json()

            if data.get("result") != 0:
                raise Exception(data)

            host = data["hosts"][0]
            path = data["path"]
            file_response = requests.get(f"https://{host}{path}", timeout=60)

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
    return f"https://e.pcloud.com/#page=publink&code={PCLOUD_CODE}&fileid={file_id}"


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

