"""Cloudflare R2 object storage helper (S3-compatible via boto3).

Mirrors fieldpulse_writer.py's conventions: load_dotenv at module load,
module-level logger to stdout, single module-level client, public functions
that wrap operations in try/except and raise StorageError on failure.
"""

import logging
import mimetypes
import os
import re
import sys
import uuid
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from dotenv import load_dotenv

_HERE = Path(__file__).resolve().parent

# --- Logging ----------------------------------------------------------------

logger = logging.getLogger("voice_intake.storage")
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)


class StorageError(Exception):
    """Raised when an R2 read/write operation fails (or is unconfigured)."""


# --- Configuration ----------------------------------------------------------

load_dotenv(_HERE / ".env")

R2_ENDPOINT_URL = (os.environ.get("R2_ENDPOINT_URL") or "").strip() or None
R2_ACCESS_KEY_ID = (os.environ.get("R2_ACCESS_KEY_ID") or "").strip() or None
R2_SECRET_ACCESS_KEY = (os.environ.get("R2_SECRET_ACCESS_KEY") or "").strip() or None
R2_BUCKET_NAME = (os.environ.get("R2_BUCKET_NAME") or "").strip() or None

# One client, created at module load. R2 uses region_name='auto'. boto3 stores
# the credentials in memory; the actual HTTPS call only happens on operations,
# so creating the client never fails by itself — we surface missing config via
# _ensure_configured() so the error message is clear.
_client = boto3.client(
    "s3",
    endpoint_url=R2_ENDPOINT_URL,
    aws_access_key_id=R2_ACCESS_KEY_ID,
    aws_secret_access_key=R2_SECRET_ACCESS_KEY,
    region_name="auto",
)


def _ensure_configured():
    missing = [
        name for name, value in (
            ("R2_ENDPOINT_URL", R2_ENDPOINT_URL),
            ("R2_ACCESS_KEY_ID", R2_ACCESS_KEY_ID),
            ("R2_SECRET_ACCESS_KEY", R2_SECRET_ACCESS_KEY),
            ("R2_BUCKET_NAME", R2_BUCKET_NAME),
        ) if not value
    ]
    if missing:
        raise StorageError(
            f"R2 is not configured — missing in .env: {', '.join(missing)}"
        )


# --- Public API -------------------------------------------------------------

def upload_audio(local_path: str, key: str = None) -> str:
    """Upload a local file to R2. If key is None, generate one as
    consultations/<uuid>.<ext> preserving the file's original extension.
    Returns the R2 key the file was stored under.
    """
    _ensure_configured()
    if not os.path.exists(local_path):
        raise StorageError(f"Local file not found: {local_path}")

    if key is None:
        ext = os.path.splitext(local_path)[1].lstrip(".") or "bin"
        key = f"consultations/{uuid.uuid4()}.{ext}"

    # Best-effort content-type so downloads come back with a sensible MIME.
    content_type, _ = mimetypes.guess_type(local_path)
    extra_args = {"ContentType": content_type} if content_type else {}

    logger.info("Uploading %s -> r2://%s/%s", local_path, R2_BUCKET_NAME, key)
    try:
        _client.upload_file(local_path, R2_BUCKET_NAME, key, ExtraArgs=extra_args)
    except (ClientError, BotoCoreError) as exc:
        logger.error("R2 upload failed: %s", exc)
        raise StorageError(
            f"Failed to upload {local_path} to r2://{R2_BUCKET_NAME}/{key}: {exc}"
        )
    logger.info("Upload OK: r2://%s/%s", R2_BUCKET_NAME, key)
    return key


def download_audio(key: str, local_path: str) -> str:
    """Download an R2 object to a local path. Returns the local path. Raises
    StorageError with a clear message if the key doesn't exist or the
    download otherwise fails."""
    _ensure_configured()
    logger.info("Downloading r2://%s/%s -> %s", R2_BUCKET_NAME, key, local_path)
    try:
        _client.download_file(R2_BUCKET_NAME, key, local_path)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("NoSuchKey", "404"):
            raise StorageError(f"R2 key not found: {key}")
        logger.error("R2 download failed: %s", exc)
        raise StorageError(
            f"Failed to download r2://{R2_BUCKET_NAME}/{key}: {exc}"
        )
    except BotoCoreError as exc:
        logger.error("R2 download failed: %s", exc)
        raise StorageError(
            f"Failed to download r2://{R2_BUCKET_NAME}/{key}: {exc}"
        )
    logger.info("Download OK: %s", local_path)
    return local_path


def delete_audio(key: str) -> None:
    """Delete an R2 object. S3/R2 returns success even if the key didn't
    exist, so this function is effectively idempotent."""
    _ensure_configured()
    logger.info("Deleting r2://%s/%s", R2_BUCKET_NAME, key)
    try:
        _client.delete_object(Bucket=R2_BUCKET_NAME, Key=key)
    except (ClientError, BotoCoreError) as exc:
        logger.error("R2 delete failed: %s", exc)
        raise StorageError(
            f"Failed to delete r2://{R2_BUCKET_NAME}/{key}: {exc}"
        )


_EXT_RE = re.compile(r"^[a-z0-9]{1,10}$")


def generate_consultation_key(filename: str) -> str:
    """Return a unique R2 key of the form `consultations/<uuid>.<ext>`.

    Preserves the filename's extension (lowercased, validated) so downstream
    `mimetypes.guess_type()` in the transcriber picks the right MIME. Falls
    back to `.bin` for weird / missing extensions.
    """
    ext = os.path.splitext(filename or "")[1].lstrip(".").lower()
    if not _EXT_RE.match(ext):
        ext = "bin"
    return f"consultations/{uuid.uuid4()}.{ext}"


def generate_upload_url(key: str, content_type: str, expires_in: int = 3600) -> str:
    """Return a presigned PUT URL for direct-to-R2 uploads.

    The client MUST send the exact same `Content-Type` header at PUT time
    that was passed here — boto3 bakes it into the SigV4 signature, and R2
    returns 403 SignatureDoesNotMatch on any mismatch.
    """
    _ensure_configured()
    logger.info(
        "Generating presigned PUT url for r2://%s/%s (expires_in=%ds)",
        R2_BUCKET_NAME, key, expires_in,
    )
    try:
        url = _client.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": R2_BUCKET_NAME,
                "Key": key,
                "ContentType": content_type,
            },
            ExpiresIn=expires_in,
        )
    except (ClientError, BotoCoreError) as exc:
        logger.error("Failed to generate upload URL: %s", exc)
        raise StorageError(
            f"Failed to generate upload URL for r2://{R2_BUCKET_NAME}/{key}: {exc}"
        )
    return url


def audio_exists(key: str) -> bool:
    """Return True if the key exists in R2, False if not. Any other error
    (auth, network, wrong bucket) is wrapped in StorageError."""
    _ensure_configured()
    try:
        _client.head_object(Bucket=R2_BUCKET_NAME, Key=key)
        return True
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        # head_object reports a missing key as "404" or "NoSuchKey" depending
        # on the backend — treat both as "doesn't exist".
        if code in ("404", "NoSuchKey"):
            return False
        raise StorageError(
            f"Failed to check r2://{R2_BUCKET_NAME}/{key}: {exc}"
        )
    except BotoCoreError as exc:
        raise StorageError(
            f"Failed to check r2://{R2_BUCKET_NAME}/{key}: {exc}"
        )


def generate_download_url(key: str, expires_in: int = 900) -> str:
    """Return a short-lived presigned GET URL for one consultation recording
    (admin panel "Play / download audio"). Read-only, expires in 15 minutes by
    default (capped at 1 hour). Only `consultations/...` keys are signed.
    Signing is local (no network call)."""
    _ensure_configured()
    if not isinstance(key, str) or not key.startswith("consultations/") or ".." in key:
        raise StorageError("Not a consultation recording key")
    expires_in = max(60, min(int(expires_in), 3600))
    try:
        return _client.generate_presigned_url(
            "get_object",
            Params={"Bucket": R2_BUCKET_NAME, "Key": key},
            ExpiresIn=expires_in,
        )
    except (ClientError, BotoCoreError) as exc:
        logger.error("Failed to generate download URL: %s", exc)
        raise StorageError(f"Failed to generate download URL for {key}: {exc}")
