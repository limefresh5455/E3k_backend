import hashlib
import logging
import tempfile
import unittest
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

import pandas as pd
import requests

from app.email_automation.database import EmailAutomationLock, JobAlreadyRunning
from app.email_automation.email_processor import process_emails
from app.email_automation.imap_client import ImapMessage, encode_modified_utf7
from app.email_automation.pcloud_upload_client import (
    PCloudClient,
    PCloudError,
    UploadResult,
    safe_cloud_name,
)
from app.email_automation.repository import UploadHistory
from app.email_automation.supplier_mapping import load_supplier_mapping


class InMemoryHistory:
    def __init__(self):
        self.uploads = {}
        self.attachments = {}
        self.messages = set()

    def is_message_processed(self, message_key):
        return message_key in self.messages

    def is_attachment_processed(self, message_key, attachment_key):
        return (message_key, attachment_key) in self.attachments

    def find_upload(self, supplier, sha256, sha1=""):
        exact = self.uploads.get((supplier, sha256))
        if exact is not None:
            return exact
        if sha1:
            return next(
                (
                    row
                    for (row_supplier, row_sha256), row in self.uploads.items()
                    if row_supplier == supplier
                    and row_sha256.startswith("sha1:")
                    and row["sha1"] == sha1
                ),
                None,
            )
        return None

    def record_upload(
        self,
        sha256,
        sha1,
        original_filename,
        supplier,
        cloud_path,
        pcloud_file_id,
        size,
    ):
        self.uploads.setdefault(
            (supplier, sha256),
            {
                "sha256": sha256,
                "sha1": sha1,
                "original_filename": original_filename,
                "supplier": supplier,
                "cloud_path": cloud_path,
                "pcloud_file_id": pcloud_file_id,
                "size": size,
            },
        )

    def record_attachment(self, message_key, attachment_key, sha256, cloud_path):
        self.attachments[(message_key, attachment_key)] = {
            "sha256": sha256,
            "cloud_path": cloud_path,
        }

    def record_message(self, message_key):
        self.messages.add(message_key)


class FakeImap:
    def __init__(self, messages):
        self.messages = messages
        self.seen = []

    def unseen_uids(self):
        return list(self.messages)

    def fetch(self, uid):
        return ImapMessage(uid, "42", self.messages[uid], "test-mailbox")

    def mark_seen(self, uid):
        self.seen.append(uid)


class FakePCloud:
    def __init__(self, fail_on_call=None):
        self.calls = []
        self.fail_on_call = fail_on_call

    def upload_pdf(self, file_path, supplier_name, cloud_filename, sha1, sha256=""):
        self.calls.append((supplier_name, cloud_filename, Path(file_path).read_bytes()))
        if self.fail_on_call == len(self.calls):
            raise RuntimeError("simulated upload failure")
        return UploadResult(
            "uploaded",
            len(self.calls),
            f"/Orders/{supplier_name}/{cloud_filename}",
            Path(file_path).stat().st_size,
        )


class FakeResponse:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        return None

    def json(self):
        return self.data


class FakeSession:
    def __init__(self, responses):
        self.headers = {}
        self.responses = list(responses)
        self.requests = []

    def request(self, method, url, timeout, **kwargs):
        self.requests.append((method, url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return FakeResponse(response)

    def close(self):
        return None


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return None

    def execute(self, query, parameters):
        self.connection.queries.append((query, parameters))

    def fetchone(self):
        return {"acquired": self.connection.lock_available}


class FakeLockConnection:
    def __init__(self, lock_available):
        self.lock_available = lock_available
        self.queries = []
        self.closed = False

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        self.closed = True


class RecordingCursor:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return None

    def execute(self, query, parameters):
        self.connection.queries.append((query, parameters))

    def fetchone(self):
        return self.connection.result


class RecordingConnection:
    def __init__(self, result=None):
        self.result = result
        self.queries = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self):
        return RecordingCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


def make_email(sender="orders@supplier.test", filenames=("order.pdf",)):
    message = EmailMessage()
    message["From"] = sender
    message["To"] = "office@example.test"
    message["Subject"] = "Order confirmation"
    message["Date"] = datetime(2026, 8, 24, tzinfo=timezone.utc)
    message.set_content("Attached")
    for index, filename in enumerate(filenames):
        message.add_attachment(
            b"%PDF-1.4 test-content-" + str(index).encode(),
            maintype="application",
            subtype="pdf",
            filename=filename,
        )
    return message.as_bytes()


class AutomationTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.history = InMemoryHistory()
        self.logger = logging.getLogger(f"test-{id(self)}")
        self.logger.addHandler(logging.NullHandler())

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_modified_utf7_folder_encoding(self):
        self.assertEqual(
            encode_modified_utf7("Auftragsbestätigung"),
            "Auftragsbest&AOQ-tigung",
        )

    def test_message_identity_is_scoped_to_mailbox(self):
        first = ImapMessage("7", "42", b"message", "mailbox-a")
        second = ImapMessage("7", "42", b"message", "mailbox-b")
        self.assertNotEqual(first.key, second.key)

    def test_postgres_lock_rejects_overlapping_instance(self):
        connection = FakeLockConnection(lock_available=False)
        with self.assertRaises(JobAlreadyRunning):
            with EmailAutomationLock(lambda: connection):
                self.fail("Unavailable advisory lock should not be acquired")
        self.assertTrue(connection.closed)

    def test_postgres_lock_is_released(self):
        connection = FakeLockConnection(lock_available=True)
        with EmailAutomationLock(lambda: connection):
            self.assertFalse(connection.closed)
        self.assertTrue(connection.closed)
        self.assertTrue(any("pg_advisory_unlock" in query for query, _ in connection.queries))

    def test_postgres_history_uses_supplier_scoped_upsert(self):
        connection = RecordingConnection()
        history = UploadHistory(connection)
        history.record_upload(
            "sha256",
            "sha1",
            "order.pdf",
            "Supplier A",
            "/Supplier A/order.pdf",
            77,
            123,
        )
        query, parameters = connection.queries[0]
        self.assertIn("email_automation_uploads", query)
        self.assertIn("ON CONFLICT (supplier, sha256) DO NOTHING", query)
        self.assertEqual(parameters[:3], ("Supplier A", "sha256", "sha1"))
        self.assertEqual(connection.commits, 1)

    def test_postgres_history_sha1_fallback_query(self):
        expected = {"cloud_path": "/Supplier A/order.pdf"}
        connection = RecordingConnection(result=expected)
        history = UploadHistory(connection)
        result = history.find_upload("Supplier A", "sha256", "sha1")
        query, parameters = connection.queries[0]
        self.assertIn("sha256 LIKE 'sha1:%%'", query)
        self.assertEqual(parameters, ("Supplier A", "sha256", "sha1"))
        self.assertIs(result, expected)

    def test_cloud_name_is_sanitized_and_bounded(self):
        value = "../" + ("ä" * 300) + ".pdf"
        sanitized = safe_cloud_name(value)
        self.assertNotIn("/", sanitized)
        self.assertLessEqual(len(sanitized.encode("utf-8")), 200)
        self.assertTrue(sanitized.endswith(".pdf"))

    def test_uploads_pdf_and_marks_message_seen(self):
        imap = FakeImap({"7": make_email()})
        pcloud = FakePCloud()
        stats = process_emails(
            imap,
            {"orders@supplier.test": "Supplier A"},
            {},
            pcloud,
            self.history,
            self.logger,
        )
        self.assertEqual(stats["pdfs_uploaded"], 1)
        self.assertEqual(stats["errors"], 0)
        self.assertEqual(imap.seen, ["7"])
        self.assertEqual(len(pcloud.calls), 1)

    def test_same_content_in_second_message_is_not_uploaded_twice(self):
        pcloud = FakePCloud()
        supplier_map = {"orders@supplier.test": "Supplier A"}
        process_emails(
            FakeImap({"7": make_email()}),
            supplier_map,
            {},
            pcloud,
            self.history,
            self.logger,
        )
        second = FakeImap({"8": make_email()})
        stats = process_emails(
            second,
            supplier_map,
            {},
            pcloud,
            self.history,
            self.logger,
        )
        self.assertEqual(len(pcloud.calls), 1)
        self.assertEqual(stats["duplicates"], 1)
        self.assertEqual(second.seen, ["8"])

    def test_sha1_index_fallback_prevents_upload(self):
        payload = b"%PDF-1.4 pre-existing"
        sha1 = hashlib.sha1(payload).hexdigest()
        self.history.record_upload(
            sha256=f"sha1:{sha1}",
            sha1=sha1,
            original_filename="existing.pdf",
            supplier="Supplier A",
            cloud_path="/Orders/Supplier A/existing.pdf",
            pcloud_file_id=99,
            size=len(payload),
        )
        row = self.history.find_upload(
            "Supplier A",
            hashlib.sha256(payload).hexdigest(),
            sha1,
        )
        self.assertEqual(row["pcloud_file_id"], 99)

    def test_same_content_for_different_supplier_uploads_twice(self):
        pcloud = FakePCloud()
        suppliers = {"a@example.test": "Supplier A", "b@example.test": "Supplier B"}
        process_emails(
            FakeImap({"11": make_email(sender="a@example.test")}),
            suppliers,
            {},
            pcloud,
            self.history,
            self.logger,
        )
        stats = process_emails(
            FakeImap({"12": make_email(sender="b@example.test")}),
            suppliers,
            {},
            pcloud,
            self.history,
            self.logger,
        )
        self.assertEqual(len(pcloud.calls), 2)
        self.assertEqual(stats["pdfs_uploaded"], 1)

    def test_mislabeled_non_pdf_is_rejected_and_left_unseen(self):
        message = EmailMessage()
        message["From"] = "orders@supplier.test"
        message["To"] = "office@example.test"
        message.set_content("Attached")
        message.add_attachment(
            b"not actually a PDF",
            maintype="application",
            subtype="pdf",
            filename="fake.pdf",
        )
        imap = FakeImap({"13": message.as_bytes()})
        stats = process_emails(
            imap,
            {"orders@supplier.test": "Supplier A"},
            {},
            FakePCloud(),
            self.history,
            self.logger,
        )
        self.assertEqual(stats["errors"], 1)
        self.assertEqual(imap.seen, [])

    def test_partial_failure_retry_uploads_only_missing_pdf(self):
        raw = make_email(filenames=("one.pdf", "two.pdf"))
        first_imap = FakeImap({"9": raw})
        first_stats = process_emails(
            first_imap,
            {"orders@supplier.test": "Supplier A"},
            {},
            FakePCloud(fail_on_call=2),
            self.history,
            self.logger,
        )
        self.assertEqual(first_stats["errors"], 1)
        self.assertEqual(first_imap.seen, [])

        retry_imap = FakeImap({"9": raw})
        retry_cloud = FakePCloud()
        retry_stats = process_emails(
            retry_imap,
            {"orders@supplier.test": "Supplier A"},
            {},
            retry_cloud,
            self.history,
            self.logger,
        )
        self.assertEqual(retry_stats["errors"], 0)
        self.assertEqual(len(retry_cloud.calls), 1)
        self.assertEqual(retry_imap.seen, ["9"])

    def test_unmatched_supplier_remains_unseen(self):
        imap = FakeImap({"10": make_email(sender="unknown@example.test")})
        stats = process_emails(
            imap,
            {},
            {},
            FakePCloud(),
            self.history,
            self.logger,
        )
        self.assertEqual(stats["skipped_no_supplier"], 1)
        self.assertEqual(imap.seen, [])

    def test_pcloud_upload_uses_nopartial_without_rename(self):
        pdf = Path(self.temporary_directory.name) / "test.pdf"
        pdf.write_bytes(b"%PDF-test")
        session = FakeSession(
            [
                {"result": 0, "metadata": {"folderid": 55}},
                {"result": 0, "metadata": {"contents": []}},
                {
                    "result": 0,
                    "metadata": [
                        {
                            "fileid": 77,
                            "path": "/Supplier/test.pdf",
                            "size": pdf.stat().st_size,
                        }
                    ],
                },
            ]
        )
        client = PCloudClient("https://eapi.pcloud.com", "secret", 10, session=session)
        result = client.upload_pdf(pdf, "Supplier", "test.pdf", "unused")
        upload_data = session.requests[2][2]["data"]
        self.assertEqual(result.file_id, 77)
        self.assertEqual(upload_data["nopartial"], 1)
        self.assertNotIn("renameifexists", upload_data)

    def test_pcloud_upload_accepts_metadata_without_optional_path(self):
        pdf = Path(self.temporary_directory.name) / "test.pdf"
        pdf.write_bytes(b"%PDF-test")
        session = FakeSession(
            [
                {"result": 0, "metadata": {"folderid": 55}},
                {"result": 0, "metadata": {"contents": []}},
                {
                    "result": 0,
                    "metadata": [
                        {
                            "fileid": 77,
                            "name": "test.pdf",
                            "size": pdf.stat().st_size,
                        }
                    ],
                },
            ]
        )
        client = PCloudClient("https://eapi.pcloud.com", "secret", 10, session=session)

        result = client.upload_pdf(pdf, "Supplier", "test.pdf", "unused")

        self.assertEqual(result.status, "uploaded")
        self.assertEqual(result.file_id, 77)
        self.assertEqual(result.cloud_path, "test.pdf")
        self.assertEqual(result.size, pdf.stat().st_size)

    def test_email_is_completed_when_upload_metadata_omits_path(self):
        payload = b"%PDF-1.4 test-content-0"
        cloud_filename = (
            f"2026-08-24_{hashlib.sha256(payload).hexdigest()[:12]}_order.pdf"
        )
        session = FakeSession(
            [
                {"result": 0, "metadata": {"folderid": 55}},
                {"result": 0, "metadata": {"contents": []}},
                {
                    "result": 0,
                    "metadata": [
                        {
                            "fileid": 77,
                            "name": cloud_filename,
                            "size": len(payload),
                        }
                    ],
                },
            ]
        )
        client = PCloudClient("https://eapi.pcloud.com", "secret", 10, session=session)
        imap = FakeImap({"7": make_email()})

        stats = process_emails(
            imap,
            {"orders@supplier.test": "Supplier A"},
            {},
            client,
            self.history,
            self.logger,
        )

        self.assertEqual(stats["pdfs_uploaded"], 1)
        self.assertEqual(stats["duplicates"], 0)
        self.assertEqual(stats["errors"], 0)
        self.assertEqual(stats["saved_paths"], [cloud_filename])
        self.assertEqual(imap.seen, ["7"])
        self.assertEqual(len(self.history.uploads), 1)
        self.assertIn(imap.fetch("7").key, self.history.messages)

    def test_pcloud_upload_still_requires_file_id_and_size(self):
        pdf = Path(self.temporary_directory.name) / "test.pdf"
        pdf.write_bytes(b"%PDF-test")
        session = FakeSession(
            [
                {"result": 0, "metadata": {"folderid": 55}},
                {"result": 0, "metadata": {"contents": []}},
                {"result": 0, "metadata": [{"name": "test.pdf"}]},
            ]
        )
        client = PCloudClient("https://eapi.pcloud.com", "secret", 10, session=session)

        with self.assertRaisesRegex(PCloudError, "required file ID or size"):
            client.upload_pdf(pdf, "Supplier", "test.pdf", "unused")

    def test_pcloud_rejects_same_name_with_different_checksum(self):
        pdf = Path(self.temporary_directory.name) / "test.pdf"
        pdf.write_bytes(b"%PDF-local")
        session = FakeSession(
            [
                {"result": 0, "metadata": {"folderid": 55}},
                {
                    "result": 0,
                    "metadata": {
                        "contents": [
                            {
                                "fileid": 77,
                                "name": "test.pdf",
                                "path": "/Supplier/test.pdf",
                                "size": 10,
                                "isfolder": False,
                            }
                        ]
                    },
                },
                {"result": 0, "sha1": "different"},
            ]
        )
        client = PCloudClient("https://eapi.pcloud.com", "secret", 10, session=session)
        with self.assertRaises(PCloudError):
            client.upload_pdf(pdf, "Supplier", "test.pdf", "local")

    def test_upload_timeout_reconciles_without_second_upload(self):
        pdf = Path(self.temporary_directory.name) / "test.pdf"
        pdf.write_bytes(b"%PDF-local")
        session = FakeSession(
            [
                {"result": 0, "metadata": {"folderid": 55}},
                {"result": 0, "metadata": {"contents": []}},
                requests.Timeout("response lost"),
                {
                    "result": 0,
                    "metadata": {
                        "contents": [
                            {
                                "fileid": 77,
                                "name": "test.pdf",
                                "path": "/Supplier/test.pdf",
                                "size": pdf.stat().st_size,
                                "isfolder": False,
                            }
                        ]
                    },
                },
                {"result": 0, "sha1": "local", "sha256": "local256"},
            ]
        )
        client = PCloudClient("https://eapi.pcloud.com", "secret", 10, session=session)
        result = client.upload_pdf(pdf, "Supplier", "test.pdf", "local", "local256")
        self.assertEqual(result.status, "duplicate")
        upload_requests = [request for request in session.requests if request[1].endswith("/uploadfile")]
        self.assertEqual(len(upload_requests), 1)

    def test_ambiguous_supplier_domain_fallback_is_disabled(self):
        excel = Path(self.temporary_directory.name) / "suppliers.xlsx"
        pd.DataFrame(
            [
                ["Folder Name (supplier name)", "(email addresses)"],
                ["Supplier A", "orders@example.test"],
                ["Supplier B", "invoices@example.test"],
            ]
        ).to_excel(excel, header=False, index=False)
        exact, domains = load_supplier_mapping(excel, self.logger)
        self.assertEqual(len(exact), 2)
        self.assertNotIn("example.test", domains)

    def test_capitalization_variants_use_first_folder_spelling(self):
        excel = Path(self.temporary_directory.name) / "suppliers.xlsx"
        pd.DataFrame(
            [
                ["Folder Name (supplier name)", "(email addresses)"],
                ["HIFI Filter SA", "info.zu@hifi-filter.ch"],
                ["HIFI FILTER SA", "info@hifi-filter.ch"],
            ]
        ).to_excel(excel, header=False, index=False)

        exact, domains = load_supplier_mapping(excel, self.logger)

        self.assertEqual(exact["info.zu@hifi-filter.ch"], "HIFI Filter SA")
        self.assertEqual(exact["info@hifi-filter.ch"], "HIFI Filter SA")
        self.assertEqual(domains["hifi-filter.ch"], "HIFI Filter SA")

    def test_same_email_with_capitalization_variant_is_not_conflicting(self):
        excel = Path(self.temporary_directory.name) / "suppliers.xlsx"
        pd.DataFrame(
            [
                ["Folder Name (supplier name)", "(email addresses)"],
                ["Supplier Name", "orders@supplier.test"],
                ["SUPPLIER NAME", "orders@supplier.test"],
            ]
        ).to_excel(excel, header=False, index=False)

        exact, domains = load_supplier_mapping(excel, self.logger)

        self.assertEqual(exact["orders@supplier.test"], "Supplier Name")
        self.assertEqual(domains["supplier.test"], "Supplier Name")

    def test_conflicting_email_mapping_is_disabled(self):
        excel = Path(self.temporary_directory.name) / "suppliers.xlsx"
        pd.DataFrame(
            [
                ["Folder Name (supplier name)", "(email addresses)"],
                ["Supplier A", "orders@supplier.test"],
                ["Supplier B", "orders@supplier.test"],
                ["Supplier E", "valid@another.test"],
            ]
        ).to_excel(excel, header=False, index=False)
        exact, domains = load_supplier_mapping(excel, self.logger)
        self.assertNotIn("orders@supplier.test", exact)
        self.assertNotIn("supplier.test", domains)
        self.assertEqual(exact["valid@another.test"], "Supplier E")


if __name__ == "__main__":
    unittest.main()
