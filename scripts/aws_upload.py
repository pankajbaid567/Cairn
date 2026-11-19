"""Upload the deployment tarballs to the artifact bucket with the local AWS profile.

AWS_PROFILE=pankaj-aws uv run --with awscrt python scripts/aws_upload.py var/cairn-src.tar.gz var/cairn-web.tar.gz
"""

from __future__ import annotations

import sys
from pathlib import Path

import boto3

BUCKET = "cairn-artifacts-892797891248-ap-south-1"


def main(paths: list[str]) -> int:
    s3 = boto3.client("s3", region_name="ap-south-1")
    for p in paths:
        path = Path(p)
        s3.upload_file(
            str(path), BUCKET, path.name.replace("cairn-src", "cairn-src").split("/")[-1]
        )
        print("uploaded", path.name)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
