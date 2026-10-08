"""
Document storage service.

Supports local filesystem, S3, and Cloudflare R2.
"""

import os
import uuid
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

import boto3
from botocore.exceptions import ClientError


class StorageBackend(ABC):
    """Abstract base class for storage backends."""

    @abstractmethod
    def upload(
        self,
        file: BinaryIO,
        key: str,
        content_type: str,
    ) -> str:
        """Upload file and return URL/path."""

    @abstractmethod
    def download(self, key: str) -> bytes:
        """Download file content."""

    @abstractmethod
    def delete(self, key: str) -> bool:
        """Delete file."""

    @abstractmethod
    def get_url(
        self,
        key: str,
        expires_in: int = 3600,
    ) -> str:
        """Get URL for file access."""


class LocalStorage(StorageBackend):
    """Local filesystem storage for development."""

    def __init__(self, base_path: str = "./uploads"):
        self.base_path = Path(base_path)
        self.base_path.mkdir(
            parents=True,
            exist_ok=True,
        )

    def upload(
        self,
        file: BinaryIO,
        key: str,
        content_type: str,
    ) -> str:
        file_path = self.base_path / key

        file_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        with open(file_path, "wb") as f:
            f.write(file.read())

        return str(file_path)

    def download(self, key: str) -> bytes:
        file_path = self.base_path / key

        with open(file_path, "rb") as f:
            return f.read()

    def delete(self, key: str) -> bool:
        file_path = self.base_path / key

        if file_path.exists():
            file_path.unlink()
            return True

        return False

    def get_url(
        self,
        key: str,
        expires_in: int = 3600,
    ) -> str:
        return f"/uploads/{key}"


class S3Storage(StorageBackend):
    """
    AWS S3 or S3-compatible storage
    (including Cloudflare R2).
    """

    def __init__(
        self,
        bucket: str,
        region: str = "us-east-1",
        access_key: str = None,
        secret_key: str = None,
        endpoint_url: str = None,
    ):
        self.bucket = bucket
        self.region = region

        self.client = boto3.client(
            "s3",
            region_name=region,
            aws_access_key_id=(
                access_key
                or os.getenv("S3_ACCESS_KEY")
            ),
            aws_secret_access_key=(
                secret_key
                or os.getenv("S3_SECRET_KEY")
            ),
            endpoint_url=endpoint_url,
        )

    def upload(
        self,
        file: BinaryIO,
        key: str,
        content_type: str,
    ) -> str:
        self.client.upload_fileobj(
            file,
            self.bucket,
            key,
            ExtraArgs={
                "ContentType": content_type
            },
        )

        return f"s3://{self.bucket}/{key}"

    def download(self, key: str) -> bytes:
        response = self.client.get_object(
            Bucket=self.bucket,
            Key=key,
        )

        return response["Body"].read()

    def delete(self, key: str) -> bool:
        try:
            self.client.delete_object(
                Bucket=self.bucket,
                Key=key,
            )
            return True

        except ClientError:
            return False

    def get_url(
        self,
        key: str,
        expires_in: int = 3600,
    ) -> str:
        return self.client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": self.bucket,
                "Key": key,
            },
            ExpiresIn=expires_in,
        )


class R2Storage(S3Storage):
    """Cloudflare R2 storage (S3-compatible)."""

    def __init__(
        self,
        bucket: str,
        account_id: str = None,
        access_key: str = None,
        secret_key: str = None,
    ):
        account_id = (
            account_id
            or os.getenv("R2_ACCOUNT_ID")
        )

        endpoint = (
            f"https://{account_id}."
            "r2.cloudflarestorage.com"
        )

        super().__init__(
            bucket=bucket,
            region="auto",
            access_key=(
                access_key
                or os.getenv("R2_ACCESS_KEY")
            ),
            secret_key=(
                secret_key
                or os.getenv("R2_SECRET_KEY")
            ),
            endpoint_url=endpoint,
        )


class DocumentStorage:
    """
    High-level document storage service.

    Handles:
    - File uploads with automatic key generation
    - Organization by company/document type
    - Metadata tracking
    """

    def __init__(
        self,
        backend: StorageBackend = None,
    ):
        self.backend = (
            backend
            or self._get_default_backend()
        )

    def _get_default_backend(
        self,
    ) -> StorageBackend:
        storage_type = os.getenv(
            "STORAGE_TYPE",
            "local",
        )

        if storage_type == "local":
            return LocalStorage(
                os.getenv(
                    "LOCAL_STORAGE_PATH",
                    "./uploads",
                )
            )

        elif storage_type == "s3":
            return S3Storage(
                bucket=os.getenv("S3_BUCKET"),
                region=os.getenv(
                    "S3_REGION",
                    "us-east-1",
                ),
            )

        elif storage_type == "r2":
            return R2Storage(
                bucket=os.getenv("R2_BUCKET"),
                account_id=os.getenv(
                    "R2_ACCOUNT_ID"
                ),
            )

        else:
            raise ValueError(
                f"Unknown storage type: "
                f"{storage_type}"
            )

    def upload_document(
        self,
        file: BinaryIO,
        filename: str,
        content_type: str,
        company_id: str = "default",
        document_type: str = "document",
    ) -> dict:
        timestamp = datetime.now(UTC).strftime(
            "%Y/%m/%d"
        )

        unique_id = str(uuid.uuid4())[:8]
        extension = Path(filename).suffix

        key = (
            f"{company_id}/"
            f"{document_type}/"
            f"{timestamp}/"
            f"{unique_id}{extension}"
        )

        path = self.backend.upload(
            file,
            key,
            content_type,
        )

        return {
            "key": key,
            "path": path,
            "filename": filename,
            "content_type": content_type,
            "company_id": company_id,
            "document_type": document_type,
            "uploaded_at": (
                datetime.now(UTC).isoformat()
            ),
        }

    def upload_check_image(
        self,
        image_data: bytes,
        check_number: str,
        company_id: str = "default",
    ) -> dict:
        import io

        timestamp = datetime.now(UTC).strftime(
            "%Y/%m/%d"
        )

        unique_id = str(uuid.uuid4())[:8]

        key = (
            f"{company_id}/checks/"
            f"{timestamp}/"
            f"check_{check_number}_"
            f"{unique_id}.png"
        )

        file = io.BytesIO(image_data)

        path = self.backend.upload(
            file,
            key,
            "image/png",
        )

        return {
            "key": key,
            "path": path,
            "check_number": check_number,
            "content_type": "image/png",
        }

    def get_document(
        self,
        key: str,
    ) -> bytes:
        return self.backend.download(key)

    def get_document_url(
        self,
        key: str,
        expires_in: int = 3600,
    ) -> str:
        return self.backend.get_url(
            key,
            expires_in,
        )

    def delete_document(
        self,
        key: str,
    ) -> bool:
        return self.backend.delete(key)