import boto3, os, uuid

_s3 = boto3.client(
    "s3",
    region_name=os.getenv("S3_REGION"),
    aws_access_key_id=os.getenv("S3_ACCESS_KEY_ID"),
    aws_secret_access_key=os.getenv("S3_SECRET_ACCESS_KEY"),
)

def put_bytes(bucket: str, key_prefix: str, content: bytes, mime: str) -> str:
    key = f"{key_prefix}/{uuid.uuid4().hex}"
    _s3.put_object(Bucket=bucket, Key=key, Body=content, ContentType=mime)
    base = os.getenv("S3_PUBLIC_BASEURL")
    return f"{base}/{key}" if base else key



