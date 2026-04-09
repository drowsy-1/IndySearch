import hashlib
import io
import logging
import os
from pathlib import Path

import zstandard as zstd

logger = logging.getLogger(__name__)

_compressor = zstd.ZstdCompressor(level=15)
_decompressor = zstd.ZstdDecompressor()

_s3_client = None


def _get_s3_client():
    """Lazily create an S3-compatible client for Railway Storage Bucket."""
    global _s3_client
    if _s3_client is None:
        import boto3

        _s3_client = boto3.client(
            "s3",
            endpoint_url=os.environ["ENDPOINT"],
            aws_access_key_id=os.environ["ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["SECRET_ACCESS_KEY"],
            region_name=os.environ.get("REGION", "auto"),
        )
    return _s3_client


def _use_bucket() -> bool:
    return bool(os.environ.get("BUCKET"))


def _get_storage_dir() -> Path:
    storage_dir = Path(os.environ.get("STORAGE_DIR", "./data/texts"))
    storage_dir.mkdir(parents=True, exist_ok=True)
    return storage_dir


def make_key(site_id: int, path: str) -> str:
    """Generate a storage key from site_id and page path."""
    path_hash = hashlib.md5(path.encode()).hexdigest()
    return f"{site_id}/{path_hash}.zst"


def store_text(key: str, text: str) -> str:
    """Compress text with zstd and store. Returns the key."""
    compressed = _compressor.compress(text.encode("utf-8"))

    if _use_bucket():
        bucket = os.environ["BUCKET"]
        _get_s3_client().put_object(Bucket=bucket, Key=key, Body=compressed)
        logger.debug(f"Stored {len(text)} chars → {len(compressed)} bytes at s3://{bucket}/{key}")
    else:
        storage_dir = _get_storage_dir()
        file_path = storage_dir / key
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(compressed)
        logger.debug(f"Stored {len(text)} chars → {len(compressed)} bytes at {key}")

    return key


def load_text(key: str) -> str:
    """Read and decompress text from storage."""
    if _use_bucket():
        bucket = os.environ["BUCKET"]
        response = _get_s3_client().get_object(Bucket=bucket, Key=key)
        compressed = response["Body"].read()
    else:
        storage_dir = _get_storage_dir()
        file_path = storage_dir / key
        compressed = file_path.read_bytes()

    return _decompressor.decompress(compressed).decode("utf-8")
