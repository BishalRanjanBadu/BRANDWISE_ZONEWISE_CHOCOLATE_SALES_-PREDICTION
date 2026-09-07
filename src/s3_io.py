"""S3 access.

Clients are constructed with region ONLY. Explicit aws_access_key_id /
aws_secret_access_key arguments override and disable the default credential
chain, so a pod would authenticate as whatever stale key it found instead of
its IRSA role. There is no key-passing code path in this module by design.
"""
import io
import json
import pickle
from functools import lru_cache

import boto3
import pandas as pd

from src.config import AWS_REGION, S3_BUCKET


@lru_cache(maxsize=1)
def client():
    return boto3.client("s3", region_name=AWS_REGION)


def get_bytes(key: str, bucket: str = None) -> bytes:
    return client().get_object(Bucket=bucket or S3_BUCKET, Key=key)["Body"].read()


def put_bytes(data: bytes, key: str, bucket: str = None) -> None:
    client().put_object(Bucket=bucket or S3_BUCKET, Key=key, Body=data)


def exists(key: str, bucket: str = None) -> bool:
    """True/False only for a genuine 404. Any other error is re-raised —
    an AccessDenied must never be reported as 'not found'."""
    try:
        client().head_object(Bucket=bucket or S3_BUCKET, Key=key)
        return True
    except client().exceptions.ClientError as exc:
        if exc.response["Error"]["Code"] in ("404", "NoSuchKey"):
            return False
        raise


def read_csv(key: str, **kw) -> pd.DataFrame:
    return pd.read_csv(io.BytesIO(get_bytes(key)), **kw)


def write_csv(df: pd.DataFrame, key: str) -> None:
    buf = io.StringIO()
    df.to_csv(buf, index=False)
    put_bytes(buf.getvalue().encode("utf-8"), key)


def read_json(key: str) -> dict:
    return json.loads(get_bytes(key))


def write_json(obj, key: str) -> None:
    put_bytes(json.dumps(obj, indent=2, default=str).encode("utf-8"), key)


def read_pickle(key: str):
    return pickle.loads(get_bytes(key))


def write_pickle(obj, key: str) -> None:
    buf = io.BytesIO()
    pickle.dump(obj, buf)
    put_bytes(buf.getvalue(), key)


def write_text(text: str, key: str) -> None:
    put_bytes(text.encode("utf-8"), key)


def copy(src_key: str, dst_key: str, bucket: str = None) -> None:
    b = bucket or S3_BUCKET
    client().copy_object(Bucket=b, Key=dst_key, CopySource={"Bucket": b, "Key": src_key})
