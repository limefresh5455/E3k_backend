"""One-time pCloud OAuth code-flow helper."""

import argparse
import getpass
import os
import secrets
from urllib.parse import urlencode

import requests


def main() -> int:
    parser = argparse.ArgumentParser(description="Authorize the pCloud application once")
    parser.add_argument(
        "--client-id",
        default=os.getenv("PCLOUD_CLIENT_ID", ""),
        help="pCloud App Key (or PCLOUD_CLIENT_ID)",
    )
    parser.add_argument("--redirect-uri", default="")
    args = parser.parse_args()
    client_id = args.client_id or input("pCloud App Key: ").strip()
    client_secret = os.getenv("PCLOUD_CLIENT_SECRET", "") or getpass.getpass(
        "pCloud App Secret: "
    )
    if not client_id or not client_secret:
        raise SystemExit("Both pCloud App Key and App Secret are required")

    state = secrets.token_urlsafe(24)
    parameters = {"client_id": client_id, "response_type": "code", "state": state}
    if args.redirect_uri:
        parameters["redirect_uri"] = args.redirect_uri
    print("Open this URL, approve access, and copy the returned code and hostname:")
    print("https://my.pcloud.com/oauth2/authorize?" + urlencode(parameters))
    code = input("Authorization code: ").strip()
    hostname = input("Returned hostname (api.pcloud.com or eapi.pcloud.com): ").strip()
    if hostname not in {"api.pcloud.com", "eapi.pcloud.com"}:
        raise SystemExit("Unexpected pCloud hostname")

    response = requests.post(
        f"https://{hostname}/oauth2_token",
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
        },
        timeout=60,
    )
    response.raise_for_status()
    data = response.json()
    if data.get("result", 0) != 0 or not data.get("access_token"):
        raise SystemExit(
            f"pCloud authorization failed: {data.get('error', 'unknown error')}"
        )
    print("\nAdd these values to the server secret configuration:")
    print("Warning: protect this console output because the access token grants file access.")
    print(f"PCLOUD_API_HOST=https://{hostname}")
    print(f"PCLOUD_ACCESS_TOKEN={data['access_token']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
