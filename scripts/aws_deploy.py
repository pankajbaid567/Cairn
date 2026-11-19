"""Upload the packaged source and web build, then rebuild and restart on the EC2 host.

Run `make aws-package` and build the web app first (`make web-dist`), then:

    AWS_PROFILE=pankaj-aws uv run --with awscrt python scripts/aws_deploy.py

Steps on the host (over SSM, no SSH): download both tarballs from the artifact bucket, stop the
waker and the container, replace /opt/cairn-src and /opt/cairn/web, rebuild the image, start the
waker. The database is not touched here: run `make remote-init` first when a migration is new.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import boto3

BUCKET = "cairn-artifacts-892797891248-ap-south-1"
INSTANCE = "i-0b6ba9aa600049792"
REGION = "ap-south-1"

REMOTE = rf"""set -e
B={BUCKET}
aws s3 cp s3://$B/cairn-src.tar.gz /tmp/cairn-src.tar.gz --region {REGION} --only-show-errors
aws s3 cp s3://$B/cairn-web.tar.gz /tmp/cairn-web.tar.gz --region {REGION} --only-show-errors
systemctl stop cairn-waker.service; docker rm -f cairn >/dev/null 2>&1 || true
rm -rf /opt/cairn-src/* /opt/cairn/web/*
tar -xzf /tmp/cairn-src.tar.gz -C /opt/cairn-src 2>/dev/null
tar -xzf /tmp/cairn-web.tar.gz -C /opt/cairn/web 2>/dev/null
chmod +x /opt/cairn-src/deploy/aws/*.sh
cd /opt/cairn-src && docker build -f infra/docker/python-app.Dockerfile -t cairn-combined:aws . > /var/log/cairn-build.log 2>&1; echo build_exit=$?
systemctl start cairn-waker.service; sleep 2; systemctl is-active cairn-waker.service
"""


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    s3 = boto3.client("s3", region_name=REGION)
    for name in ("cairn-src.tar.gz", "cairn-web.tar.gz"):
        s3.upload_file(str(root / "var" / name), BUCKET, name)
        print("uploaded", name, flush=True)
    ssm = boto3.client("ssm", region_name=REGION)
    cmd = ssm.send_command(
        InstanceIds=[INSTANCE],
        DocumentName="AWS-RunShellScript",
        Parameters={"commands": [REMOTE], "executionTimeout": ["900"]},
    )["Command"]["CommandId"]
    for _ in range(120):
        time.sleep(8)
        try:
            inv = ssm.get_command_invocation(CommandId=cmd, InstanceId=INSTANCE)
        except ssm.exceptions.InvocationDoesNotExist:
            continue
        if inv["Status"] not in ("InProgress", "Pending"):
            print(
                inv["Status"],
                inv["StandardOutputContent"].strip()[-120:],
                inv["StandardErrorContent"].strip()[-200:],
            )
            return (
                0
                if inv["Status"] == "Success" and "build_exit=0" in inv["StandardOutputContent"]
                else 1
            )
    print("timed out waiting for the host")
    return 1


if __name__ == "__main__":
    sys.exit(main())
