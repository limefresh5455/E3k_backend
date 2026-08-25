"""Small IMAP client used by the scheduled job."""

import base64
import hashlib
import imaplib
from dataclasses import dataclass


class ImapError(RuntimeError):
    """Raised when an IMAP operation fails."""


def encode_modified_utf7(value: str) -> str:
    """Encode an IMAP mailbox name using modified UTF-7 (RFC 3501)."""
    result: list[str] = []
    non_ascii: list[str] = []

    def flush() -> None:
        if not non_ascii:
            return
        raw = "".join(non_ascii).encode("utf-16-be")
        encoded = base64.b64encode(raw).decode("ascii").rstrip("=").replace("/", ",")
        result.append(f"&{encoded}-")
        non_ascii.clear()

    for character in value:
        if 0x20 <= ord(character) <= 0x7E:
            flush()
            result.append("&-" if character == "&" else character)
        else:
            non_ascii.append(character)
    flush()
    return "".join(result)


@dataclass(frozen=True)
class ImapMessage:
    uid: str
    uid_validity: str
    raw_message: bytes
    mailbox_key: str = ""

    @property
    def key(self) -> str:
        return f"{self.mailbox_key}|{self.uid_validity}:{self.uid}"


class ImapClient:
    def __init__(
        self,
        host,
        port,
        username,
        password="",
        oauth2_token="",
        auth_method="password",
        use_ssl=True,
        timeout=60,
    ):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.oauth2_token = oauth2_token
        self.auth_method = auth_method
        self.use_ssl = use_ssl
        self.timeout = timeout
        self.connection = None
        self.uid_validity = "unknown"
        self.mailbox_key = ""

    def connect(self, folder: str) -> None:
        connection_type = imaplib.IMAP4_SSL if self.use_ssl else imaplib.IMAP4
        self.connection = connection_type(self.host, self.port, timeout=self.timeout)
        if self.auth_method == "xoauth2":
            auth = f"user={self.username}\x01auth=Bearer {self.oauth2_token}\x01\x01"
            status, data = self.connection.authenticate("XOAUTH2", lambda _: auth.encode())
        else:
            status, data = self.connection.login(self.username, self.password)
        self._require_ok(status, data, "authenticate")

        encoded_folder = encode_modified_utf7(folder).replace("\\", "\\\\").replace('"', '\\"')
        status, data = self.connection.select(f'"{encoded_folder}"', readonly=False)
        self._require_ok(status, data, f"select folder '{folder}'")
        response = self.connection.response("UIDVALIDITY")
        if response and response[1] and response[1][0]:
            value = response[1][0]
            self.uid_validity = value.decode() if isinstance(value, bytes) else str(value)
        if self.uid_validity == "unknown":
            raise ImapError(
                "IMAP server did not provide UIDVALIDITY; safe deduplication is impossible"
            )
        identity = f"{self.host.lower()}|{self.username.lower()}|{folder}"
        self.mailbox_key = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]

    def unseen_uids(self) -> list[str]:
        self._ensure_connected()
        status, data = self.connection.uid("search", None, "UNSEEN")
        self._require_ok(status, data, "search unseen messages")
        if not data or not data[0]:
            return []
        return [item.decode("ascii") for item in data[0].split()]

    def fetch(self, uid: str) -> ImapMessage:
        self._ensure_connected()
        status, data = self.connection.uid("fetch", uid, "(BODY.PEEK[])")
        self._require_ok(status, data, f"fetch UID {uid}")
        raw = next(
            (item[1] for item in data if isinstance(item, tuple) and len(item) > 1),
            None,
        )
        if not isinstance(raw, bytes):
            raise ImapError(f"IMAP returned no message body for UID {uid}")
        return ImapMessage(
            uid=uid,
            uid_validity=self.uid_validity,
            raw_message=raw,
            mailbox_key=self.mailbox_key,
        )

    def mark_seen(self, uid: str) -> None:
        self._ensure_connected()
        status, data = self.connection.uid("store", uid, "+FLAGS.SILENT", "(\\Seen)")
        self._require_ok(status, data, f"mark UID {uid} seen")

    def close(self) -> None:
        if self.connection is None:
            return
        try:
            self.connection.close()
        except (imaplib.IMAP4.error, OSError):
            pass
        try:
            self.connection.logout()
        except (imaplib.IMAP4.error, OSError):
            pass
        self.connection = None

    def _ensure_connected(self) -> None:
        if self.connection is None:
            raise ImapError("IMAP client is not connected")

    @staticmethod
    def _require_ok(status, data, operation: str) -> None:
        if status != "OK":
            detail = data[0] if data else "unknown error"
            if isinstance(detail, bytes):
                detail = detail.decode(errors="replace")
            raise ImapError(f"Failed to {operation}: {detail}")
