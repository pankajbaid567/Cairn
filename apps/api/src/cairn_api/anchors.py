"""External anchors for the audit chain.

The chain proves that no event was edited or removed inside the database. An administrator with
full access to that database could still rewrite every row and recompute every hash. An anchor
defeats that: the head of the chain is also written, with a signature, to a store the database
cannot reach (an S3 bucket with Object Lock, so a stored anchor cannot be overwritten or deleted
during its retention period). Verification then checks that each anchor's hash is still the hash
of that event in the database.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class Anchor:
    org_id: int
    seq: int
    head_hash: str
    at: str
    signature: str

    def body(self) -> bytes:
        return json.dumps(self.__dict__, sort_keys=True, separators=(",", ":")).encode()


def sign_anchor(key: bytes, org_id: int, seq: int, head_hash: str, at: str) -> str:
    return hmac.new(key, f"{org_id}|{seq}|{head_hash}|{at}".encode(), hashlib.sha256).hexdigest()


def signature_ok(key: bytes, a: Anchor) -> bool:
    return hmac.compare_digest(a.signature, sign_anchor(key, a.org_id, a.seq, a.head_hash, a.at))


class AnchorStore(Protocol):
    async def put(self, anchor: Anchor) -> None: ...

    async def list(self, org_id: int) -> list[Anchor]: ...


class MemoryAnchorStore:
    """For tests and local runs: a plain dict, which is exactly what the real store is not."""

    def __init__(self) -> None:
        self.items: dict[tuple[int, int], Anchor] = {}

    async def put(self, anchor: Anchor) -> None:
        key = (anchor.org_id, anchor.seq)
        if key in self.items:
            return  # an anchor is written once
        self.items[key] = anchor

    async def list(self, org_id: int) -> list[Anchor]:
        return sorted((a for (o, _), a in self.items.items() if o == org_id), key=lambda a: a.seq)


class S3AnchorStore:
    """Anchors as small JSON objects in a bucket with Object Lock and versioning."""

    def __init__(self, bucket: str, region: str) -> None:
        self._bucket = bucket
        self._region = region
        self._cli: Any = None

    def _client(self) -> Any:
        if self._cli is None:
            import boto3
            from botocore.config import Config

            self._cli = boto3.client(
                "s3",
                region_name=self._region,
                config=Config(connect_timeout=3, read_timeout=8, retries={"max_attempts": 2}),
            )
        return self._cli

    async def put(self, anchor: Anchor) -> None:
        key = f"anchors/{anchor.org_id}/{anchor.seq:010d}.json"

        def run() -> None:
            self._client().put_object(
                Bucket=self._bucket,
                Key=key,
                Body=anchor.body(),
                ContentType="application/json",
                IfNoneMatch="*",  # never replace an existing anchor
            )

        try:
            await asyncio.to_thread(run)
        except Exception as exc:  # an existing key is fine; anything else is a real failure
            if "PreconditionFailed" not in str(exc) and "412" not in str(exc):
                raise

    async def list(self, org_id: int) -> list[Anchor]:
        def run() -> list[Anchor]:
            cli = self._client()
            keys: list[str] = []
            token: str | None = None
            while True:
                kw: dict[str, Any] = {"Bucket": self._bucket, "Prefix": f"anchors/{org_id}/"}
                if token:
                    kw["ContinuationToken"] = token
                res = cli.list_objects_v2(**kw)
                keys += [o["Key"] for o in res.get("Contents", [])]
                token = res.get("NextContinuationToken")
                if not token:
                    break
            out = []
            for key in sorted(keys)[-200:]:
                data = json.loads(cli.get_object(Bucket=self._bucket, Key=key)["Body"].read())
                out.append(Anchor(**data))
            return out

        return await asyncio.to_thread(run)
