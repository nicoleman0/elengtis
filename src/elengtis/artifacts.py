"""Private S3-compatible artifact storage."""
from __future__ import annotations

import hashlib
from io import BytesIO

import boto3
from botocore.config import Config


class ArtifactStore:
    def __init__(self, endpoint: str, bucket: str, access_key: str, secret_key: str):
        self.bucket = bucket
        self.client = boto3.client("s3", endpoint_url=endpoint, aws_access_key_id=access_key,
                                  aws_secret_access_key=secret_key, config=Config(s3={"addressing_style": "path"}))

    def put(self, key: str, data: bytes, content_type: str = "application/json") -> dict:
        digest = hashlib.sha256(data).hexdigest()
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type,
                               Metadata={"sha256": digest})
        return {"object_key": key, "sha256": digest, "size_bytes": len(data), "content_type": content_type}

    def get(self, key: str) -> bytes:
        return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
