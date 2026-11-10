"""Shared infrastructure for the S3-backed dashboard dataset loaders.

What this module provides (used by both the worker and the finance loader):

* ``VerifiedStreamReader`` - streams an S3 object, verifies its SHA-256 while it
  is read and gunzips it in bounded steps.  The full compressed object is never
  held in memory and decompression is capped, so a gzip bomb cannot exhaust
  memory.  Compressed and decompressed sizes have separate limits.
* ``parse_json_object`` - incremental JSON parsing (ijson, C backend) straight
  from that stream, falling back to ``json.loads`` when ijson is unavailable.
* ``dump_gzip`` - serialises the normalised dataset section by section, in row
  batches, directly into gzip, so the full JSON text is never built in memory.
* ``load_manifest`` - manifest download with a short TTL cache.
* ``DatasetCache`` - processed datasets are cached (gzip-compressed) in memory
  per process *and* on disk, so all Gunicorn workers on the host share the work.
  A cross-process file lock makes sure only one worker builds a generation.
* ``S3DatasetStore`` - the common flow of the worker and finance loaders.
"""

from __future__ import annotations

import contextlib
from collections import OrderedDict
from dataclasses import dataclass
import gzip
import hashlib
import hmac
import io
import json
import logging
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Any, Callable, Iterator
import zlib

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

try:  # POSIX only; without it the cache still works, just without the file lock.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

try:
    import ijson

    # The pure-Python ijson backends are much slower than json.loads, so the
    # incremental path is only used when a C backend is available.
    INCREMENTAL_AVAILABLE = ijson.backend in {"yajl2_c", "yajl2_cffi"}
except ImportError:  # pragma: no cover
    ijson = None  # type: ignore[assignment]
    INCREMENTAL_AVAILABLE = False

from .config import DashboardSettings
from .constants import COMPANIES


logger = logging.getLogger("worker_dashboard.s3_cache")

MANIFEST_MAX_BYTES = 1024 * 1024
# Bump whenever the normalisation output changes so stale cache files are ignored.
CACHE_VERSION = "v1"

_GZIP_MAGIC = b"\x1f\x8b"
_SOURCE_CHUNK = 1024 * 1024  # bytes requested from S3 per read
_DECODE_STEP = 256 * 1024  # max decompressed bytes produced per zlib call
_PARSE_BUFFER = 256 * 1024  # bytes handed to ijson per read
_GZIP_LEVEL = 5
_ROW_BATCH = 5000  # rows serialised per json.dumps call
_MEMORY_ENTRIES = 4
_LOCK_TIMEOUT_SECONDS = 180.0

# --------------------------------------------------------------------------- #
# S3 client
# --------------------------------------------------------------------------- #

_CLIENT_LOCK = threading.Lock()
_CLIENTS: dict[str, Any] = {}


def get_s3_client(settings: DashboardSettings):
    """One shared boto3 client per region (clients are thread-safe)."""

    with _CLIENT_LOCK:
        client = _CLIENTS.get(settings.s3_region)
        if client is None:
            client = boto3.client(
                "s3",
                region_name=settings.s3_region,
                config=Config(
                    signature_version="s3v4",
                    connect_timeout=5,
                    read_timeout=60,
                    retries={"max_attempts": 3, "mode": "standard"},
                    max_pool_connections=16,
                ),
            )
            _CLIENTS[settings.s3_region] = client
        return client


# --------------------------------------------------------------------------- #
# Manifest (TTL cache)
# --------------------------------------------------------------------------- #

_MANIFEST_LOCK = threading.Lock()
_MANIFEST_CACHE: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}


def load_manifest(s3, settings: DashboardSettings, error_cls: type[Exception]) -> dict[str, Any]:
    """Return the parsed manifest, downloading it at most once per TTL.

    The returned dict is shared between callers and must be treated as read-only.
    """

    cache_key = (settings.s3_bucket, settings.s3_manifest_key)
    ttl = settings.s3_manifest_ttl_seconds
    if ttl > 0:
        with _MANIFEST_LOCK:
            hit = _MANIFEST_CACHE.get(cache_key)
        if hit is not None and time.monotonic() - hit[0] < ttl:
            return hit[1]

    try:
        response = s3.get_object(
            Bucket=settings.s3_bucket,
            Key=settings.s3_manifest_key,
        )
    except (BotoCoreError, ClientError) as error:
        raise error_cls("Could not read S3 dashboard manifest") from error

    body = response.get("Body")
    if body is None or not hasattr(body, "read"):
        raise error_cls("S3 manifest has no readable body")
    try:
        raw = body.read(MANIFEST_MAX_BYTES + 1)
    except (BotoCoreError, OSError) as error:
        raise error_cls("Could not read S3 manifest body") from error
    if len(raw) > MANIFEST_MAX_BYTES:
        raise error_cls("S3 manifest exceeds configured size limit")
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise error_cls("S3 manifest is not valid JSON") from error
    if not isinstance(manifest, dict):
        raise error_cls("S3 manifest root must be an object")

    if ttl > 0:
        with _MANIFEST_LOCK:
            _MANIFEST_CACHE[cache_key] = (time.monotonic(), manifest)
    return manifest


# --------------------------------------------------------------------------- #
# Streaming download + verification + gunzip
# --------------------------------------------------------------------------- #


class VerifiedStreamReader:
    """File-like object over an S3 body: hash-verified, size-limited, gunzipped.

    ``gzip_mode="require"`` rejects objects that are not gzip; ``"auto"``
    accepts gzip or plain data.  The SHA-256 (of the stored bytes) is checked
    when the stream is exhausted - call ``verify()`` or ``read_all()`` before
    trusting anything parsed from it.
    """

    def __init__(
        self,
        body,
        *,
        expected_sha256: str,
        max_compressed: int,
        max_decompressed: int,
        gzip_mode: str,
        label: str,
        error_cls: type[Exception],
    ):
        self._body = body
        self._expected = expected_sha256
        self._max_compressed = max_compressed
        self._max_decompressed = max_decompressed
        self._gzip_mode = gzip_mode
        self._label = label
        self._error = error_cls
        self._hash = hashlib.sha256()
        self._compressed = 0
        self._decompressed = 0
        self._buffer = bytearray()
        self._pieces: Iterator[bytes] = self._decode()
        self._exhausted = False

    # -- public API -------------------------------------------------------- #

    def read(self, size: int | None = -1) -> bytes:
        if size is None or size < 0:
            while self._fill():
                pass
            data = bytes(self._buffer)
            self._buffer.clear()
            return data
        while len(self._buffer) < size and self._fill():
            pass
        data = bytes(self._buffer[:size])
        del self._buffer[:size]
        return data

    def read_all(self) -> bytearray:
        """Consume the whole stream, verify it and hand over the buffer (no copy)."""

        while self._fill():
            pass
        data, self._buffer = self._buffer, bytearray()
        return data

    def verify(self) -> None:
        """Drain whatever the parser did not read and check the checksum."""

        while True:
            if self._buffer.strip():
                raise self._error(f"{self._label} has unexpected data after the JSON document")
            self._buffer.clear()
            if not self._fill():
                return

    def close(self) -> None:
        close = getattr(self._body, "close", None)
        if callable(close):
            with contextlib.suppress(Exception):
                close()

    # -- internals --------------------------------------------------------- #

    def _fill(self) -> bool:
        """Decode a bit more. Returns False once the stream is exhausted (and verified)."""

        if self._exhausted:
            return False
        try:
            piece = next(self._pieces)
        except StopIteration:
            self._exhausted = True
            actual = self._hash.hexdigest()
            if not hmac.compare_digest(actual, self._expected):
                raise self._error(f"{self._label} checksum does not match manifest")
            return False
        self._decompressed += len(piece)
        if self._decompressed > self._max_decompressed:
            raise self._error(f"Decompressed {self._label} exceeds configured size limit")
        self._buffer += piece
        return True

    def _source(self) -> Iterator[bytes]:
        body = self._body
        if body is None or not hasattr(body, "read"):
            raise self._error(f"{self._label} has no readable body")
        while True:
            try:
                chunk = body.read(_SOURCE_CHUNK)
            except (BotoCoreError, OSError) as error:
                raise self._error(f"Could not read {self._label} body") from error
            if not chunk:
                return
            self._hash.update(chunk)
            self._compressed += len(chunk)
            if self._compressed > self._max_compressed:
                raise self._error(f"{self._label} exceeds configured size limit")
            yield chunk

    def _decode(self) -> Iterator[bytes]:
        mode: str | None = None
        decoder = None
        pending = b""  # undecided leading bytes or a split gzip member header
        for chunk in self._source():
            data = pending + chunk
            pending = b""
            if mode is None:
                if len(data) < 2:
                    pending = data
                    continue
                mode = "gzip" if data[:2] == _GZIP_MAGIC else "plain"
                if mode == "plain" and self._gzip_mode == "require":
                    raise self._error(f"{self._label} is not gzip encoded")
            if mode == "plain":
                yield data
                continue
            while data:
                if decoder is None:
                    if len(data) < 2:
                        pending = data
                        break
                    if data[:2] != _GZIP_MAGIC:
                        if data.strip(b"\x00"):
                            raise self._error(f"{self._label} has unexpected data after gzip stream")
                        break  # zero padding
                    decoder = zlib.decompressobj(zlib.MAX_WBITS | 16)
                while True:
                    try:
                        out = decoder.decompress(data, _DECODE_STEP)
                    except zlib.error as error:
                        raise self._error(f"{self._label} is not valid gzip data") from error
                    if out:
                        yield out
                    data = decoder.unconsumed_tail
                    if decoder.eof:
                        data = decoder.unused_data
                        decoder = None
                        break
                    if not data and len(out) < _DECODE_STEP:
                        break
        # ---- end of input ----
        if mode is None:
            if pending:
                if self._gzip_mode == "require":
                    raise self._error(f"{self._label} is not gzip encoded")
                yield pending
            elif self._gzip_mode == "require":
                raise self._error(f"{self._label} is empty")
            return
        if mode == "gzip":
            if decoder is not None:
                raise self._error(f"{self._label} is not valid gzip data (truncated)")
            if pending.strip(b"\x00"):
                raise self._error(f"{self._label} has unexpected data after gzip stream")


def parse_json_object(
    reader: VerifiedStreamReader,
    *,
    incremental: bool,
    label: str,
    error_cls: type[Exception],
) -> dict[str, Any]:
    """Parse the stream into a dict of top-level sections, checksum verified."""

    if incremental and INCREMENTAL_AVAILABLE:
        result: dict[str, Any] = {}
        try:
            # use_float keeps JSON decimals as float (not Decimal), exactly like json.loads.
            for key, value in ijson.kvitems(
                reader, "", use_float=True, buf_size=_PARSE_BUFFER
            ):
                result[key] = value
        except (ijson.JSONError, ValueError) as error:
            raise error_cls(f"{label} is not valid UTF-8 JSON") from error
        reader.verify()
        return result

    data = reader.read_all()  # checksum verified before parsing
    try:
        payload = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise error_cls(f"{label} is not valid UTF-8 JSON") from error
    finally:
        del data
    if not isinstance(payload, dict):
        raise error_cls(f"{label} JSON root must be an object")
    return payload


# --------------------------------------------------------------------------- #
# Serialisation
# --------------------------------------------------------------------------- #


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _write_value(gz, value: Any) -> None:
    if isinstance(value, list) and len(value) > _ROW_BATCH:
        gz.write(b"[")
        first = True
        while value:
            batch = value[:_ROW_BATCH]
            del value[:_ROW_BATCH]  # release rows as soon as they are written
            text = _dumps(batch)[1:-1]
            gz.write(text.encode("utf-8") if first else b"," + text.encode("utf-8"))
            first = False
        gz.write(b"]")
    else:
        gz.write(_dumps(value).encode("utf-8"))


def dump_gzip(payload: dict[str, Any]) -> bytes:
    """Serialise ``payload`` to gzip-compressed JSON, section by section.

    The dict is consumed: each section is removed once written so its memory
    can be reclaimed.  Key order is preserved.
    """

    buffer = io.BytesIO()
    with gzip.GzipFile(
        fileobj=buffer, mode="wb", compresslevel=_GZIP_LEVEL, mtime=0
    ) as gz:
        gz.write(b"{")
        first = True
        for key in list(payload):
            value = payload.pop(key)
            gz.write(
                (b"" if first else b",")
                + _dumps(key).encode("utf-8")
                + b":"
            )
            _write_value(gz, value)
            first = False
            del value
        gz.write(b"}")
    return buffer.getvalue()


# --------------------------------------------------------------------------- #
# Shared (memory + disk) dataset cache
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CachedDataset:
    token: str
    gzip_body: bytes
    meta: dict[str, Any]

    @property
    def etag(self) -> str:
        # Weak validator: the same dataset is served gzip-encoded or identity.
        return f'W/"{self.token}"'

    def json_bytes(self) -> bytes:
        return gzip.decompress(self.gzip_body)


_MEMORY_LOCK = threading.Lock()
_MEMORY: "OrderedDict[tuple[str, str], CachedDataset]" = OrderedDict()
_THREAD_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.Lock] = {}


def reset_caches() -> None:
    """Forget all in-process caches (used by tests)."""

    with _MEMORY_LOCK:
        _MEMORY.clear()
    with _MANIFEST_LOCK:
        _MANIFEST_CACHE.clear()


def _thread_lock(name: str) -> threading.Lock:
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(name, threading.Lock())


class DatasetCache:
    def __init__(self, settings: DashboardSettings):
        self._dir: Path | None = (
            settings.data_root / "cache" / "s3" if settings.s3_disk_cache else None
        )

    # -- lookup ------------------------------------------------------------ #

    def get(self, name: str, token: str) -> CachedDataset | None:
        key = (name, token)
        with _MEMORY_LOCK:
            hit = _MEMORY.get(key)
            if hit is not None:
                _MEMORY.move_to_end(key)
                return hit
        entry = self._read_disk(name, token)
        if entry is not None:
            self._remember(name, entry)
        return entry

    def get_meta(self, name: str, token: str) -> dict[str, Any] | None:
        with _MEMORY_LOCK:
            hit = _MEMORY.get((name, token))
        if hit is not None:
            return hit.meta
        if self._dir is None:
            return None
        try:
            return json.loads(self._meta_path(name, token).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def get_or_build(
        self,
        name: str,
        token: str,
        build: Callable[[], tuple[bytes, dict[str, Any]]],
    ) -> CachedDataset:
        entry = self.get(name, token)
        if entry is not None:
            return entry
        with self._build_lock(name):
            # Another thread/worker may have finished the build while we waited.
            entry = self.get(name, token)
            if entry is not None:
                return entry
            started = time.monotonic()
            gzip_body, meta = build()
            meta = {**meta, "gzipSha256": hashlib.sha256(gzip_body).hexdigest()}
            entry = CachedDataset(token=token, gzip_body=gzip_body, meta=meta)
            self._write_disk(name, entry)
            self._remember(name, entry)
            logger.info(
                "Built dashboard dataset %s (%d KiB gzip) in %.1fs",
                name,
                len(gzip_body) // 1024,
                time.monotonic() - started,
            )
            return entry

    # -- memory ------------------------------------------------------------ #

    def _remember(self, name: str, entry: CachedDataset) -> None:
        with _MEMORY_LOCK:
            for key in [k for k in _MEMORY if k[0] == name and k[1] != entry.token]:
                del _MEMORY[key]
            _MEMORY[(name, entry.token)] = entry
            _MEMORY.move_to_end((name, entry.token))
            while len(_MEMORY) > _MEMORY_ENTRIES:
                _MEMORY.popitem(last=False)

    # -- disk -------------------------------------------------------------- #

    def _body_path(self, name: str, token: str) -> Path:
        assert self._dir is not None
        return self._dir / f"{name}-{token}.json.gz"

    def _meta_path(self, name: str, token: str) -> Path:
        assert self._dir is not None
        return self._dir / f"{name}-{token}.meta.json"

    def _read_disk(self, name: str, token: str) -> CachedDataset | None:
        if self._dir is None:
            return None
        try:
            meta = json.loads(self._meta_path(name, token).read_text(encoding="utf-8"))
            body = self._body_path(name, token).read_bytes()
        except (OSError, ValueError):
            return None
        expected = meta.get("gzipSha256")
        if (
            not isinstance(expected, str)
            or not body.startswith(_GZIP_MAGIC)
            or not hmac.compare_digest(hashlib.sha256(body).hexdigest(), expected)
        ):
            logger.warning("Ignoring corrupt dashboard cache file for %s", name)
            return None
        return CachedDataset(token=token, gzip_body=body, meta=meta)

    def _write_disk(self, name: str, entry: CachedDataset) -> None:
        if self._dir is None:
            return
        try:
            self._dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            # Body first, meta last: the meta file marks a complete entry.
            _atomic_write(self._body_path(name, entry.token), entry.gzip_body)
            _atomic_write(
                self._meta_path(name, entry.token),
                json.dumps(entry.meta).encode("utf-8"),
            )
            for stale in self._dir.glob(f"{name}-*"):
                if f"-{entry.token}." not in stale.name:
                    stale.unlink(missing_ok=True)
        except OSError as error:
            # Cache is an optimisation only; keep serving from memory.
            logger.warning("Could not write dashboard disk cache for %s: %s", name, error)

    # -- build coordination ------------------------------------------------ #

    @contextlib.contextmanager
    def _build_lock(self, name: str):
        with _thread_lock(name):
            fd = None
            if self._dir is not None and fcntl is not None:
                try:
                    self._dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                    fd = os.open(self._dir / f"{name}.lock", os.O_CREAT | os.O_RDWR, 0o600)
                    deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
                    while True:
                        try:
                            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            break
                        except BlockingIOError:
                            if time.monotonic() > deadline:
                                logger.warning(
                                    "Timed out waiting for the %s build lock; building anyway",
                                    name,
                                )
                                break
                            time.sleep(0.2)
                except OSError as error:
                    logger.warning("Dashboard cache lock unavailable for %s: %s", name, error)
                    if fd is not None:
                        os.close(fd)
                        fd = None
            try:
                yield
            finally:
                if fd is not None:
                    os.close(fd)  # closing releases the flock


def _atomic_write(target: Path, content: bytes) -> None:
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=target.parent, prefix=f".{target.name}.", suffix=".tmp", delete=False
        ) as handle:
            temporary = handle.name
            handle.write(content)
        os.replace(temporary, target)
        temporary = None
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# Common loader flow
# --------------------------------------------------------------------------- #


class S3DatasetStore:
    """Manifest -> verified streaming download -> normalise -> shared cache.

    Subclasses set the class attributes and implement ``_validate_key`` and
    ``_build_payload``.
    """

    audience: str = ""  # "worker" | "finance"
    manifest_prefix: str = ""  # manifest["objects"][f"{prefix}/{company}"]
    error_cls: type[Exception] = RuntimeError
    gzip_mode: str = "require"

    def __init__(self, settings: DashboardSettings, s3_client=None):
        self.settings = settings
        self.s3 = s3_client or get_s3_client(settings)
        self.cache = DatasetCache(settings)

    # -- subclass hooks ---------------------------------------------------- #

    def _validate_key(self, key: str, company: str) -> None:  # pragma: no cover
        raise NotImplementedError

    def _build_payload(
        self,
        source: dict[str, Any],
        *,
        company: str,
        entry: dict[str, Any],
        manifest: dict[str, Any],
    ) -> dict[str, Any]:  # pragma: no cover
        """Turn the parsed object into the dashboard contract. May consume ``source``."""
        raise NotImplementedError

    # -- public API -------------------------------------------------------- #

    def read_cached(self, company: str) -> CachedDataset:
        manifest, entry, key, digest, token = self._resolve(company)
        name = self._name(company)

        def build() -> tuple[bytes, dict[str, Any]]:
            return self._build(company, manifest, entry, key, digest)

        return self.cache.get_or_build(name, token, build)

    def read(self, company: str) -> bytes:
        return self.read_cached(company).json_bytes()

    def status(self, company: str) -> dict[str, Any]:
        manifest, _entry, _key, _digest, token = self._resolve(company)
        meta = self.cache.get_meta(self._name(company), token)
        if meta is None:
            meta = self.read_cached(company).meta
        status: dict[str, Any] = {
            "audience": self.audience,
            "company": company,
            "status": meta["status"],
            "source_as_of": meta["source_as_of"],
            "source_exported_at": meta["source_exported_at"],
        }
        published_at = _text(manifest.get("publishedAt"))
        if published_at:
            status["published_at"] = _utc_timestamp(published_at, self.error_cls)
        return status

    # -- internals --------------------------------------------------------- #

    def _name(self, company: str) -> str:
        return f"{self.audience}-{company}"

    def _resolve(self, company: str):
        if company not in COMPANIES:
            raise self.error_cls("Unsupported dashboard company")
        manifest = load_manifest(self.s3, self.settings, self.error_cls)
        try:
            entry = manifest["objects"][f"{self.manifest_prefix}/{company}"]
        except (KeyError, TypeError) as error:
            raise self.error_cls(
                f"S3 manifest has no valid {self.manifest_prefix}/{company} entry"
            ) from error
        if not isinstance(entry, dict):
            raise self.error_cls(f"S3 manifest {self.manifest_prefix} entry is invalid")
        key = _required_text(entry, "key", self.error_cls)
        digest = _required_sha256(entry.get("sha256"), self.error_cls)
        fetched_at = _required_text(entry, "sourceFetchedAt", self.error_cls)
        self._validate_key(key, company)
        # Everything the processed output depends on goes into the token.
        fingerprint = hashlib.sha256(
            "|".join((self.settings.s3_bucket, key, digest, fetched_at)).encode("utf-8")
        ).hexdigest()[:40]
        return manifest, entry, key, digest, f"{CACHE_VERSION}-{fingerprint}"

    def _build(
        self,
        company: str,
        manifest: dict[str, Any],
        entry: dict[str, Any],
        key: str,
        digest: str,
    ) -> tuple[bytes, dict[str, Any]]:
        label = f"S3 {self.audience} object"
        try:
            response = self.s3.get_object(Bucket=self.settings.s3_bucket, Key=key)
        except (BotoCoreError, ClientError) as error:
            raise self.error_cls(f"Could not read S3 dashboard {self.audience} object") from error
        reader = VerifiedStreamReader(
            response.get("Body"),
            expected_sha256=digest,
            max_compressed=self.settings.s3_max_compressed_bytes,
            max_decompressed=self.settings.s3_max_decompressed_bytes,
            gzip_mode=self.gzip_mode,
            label=label,
            error_cls=self.error_cls,
        )
        try:
            source = parse_json_object(
                reader,
                incremental=self.settings.s3_incremental_parse,
                label=label,
                error_cls=self.error_cls,
            )
        finally:
            reader.close()
        payload = self._build_payload(source, company=company, entry=entry, manifest=manifest)
        del source
        header = payload["source"]
        meta = {
            "status": header["status"],
            "source_as_of": header["asOf"],
            "source_exported_at": header["exportedAt"],
        }
        return dump_gzip(payload), meta


# --------------------------------------------------------------------------- #
# Small helpers shared by the loaders
# --------------------------------------------------------------------------- #


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _required_text(mapping: dict[str, Any], key: str, error_cls: type[Exception]) -> str:
    value = _text(mapping.get(key))
    if not value:
        raise error_cls(f"S3 manifest entry is missing {key}")
    return value


def _required_sha256(value: Any, error_cls: type[Exception]) -> str:
    text = _text(value).lower()
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise error_cls("S3 manifest contains an invalid SHA-256 value")
    return text


def _utc_timestamp(value: str, error_cls: type[Exception]) -> str:
    from datetime import datetime, timezone

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise error_cls("S3 manifest timestamp is invalid") from error
    if parsed.tzinfo is None:
        raise error_cls("S3 manifest timestamp has no timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
