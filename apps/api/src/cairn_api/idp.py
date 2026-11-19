"""Sign-in: the identity provider behind the API.

Amazon Cognito proves who a person is (password, email verification, reset). Cairn keeps the
organisation, role and teams in PostgreSQL. The API talks to a provider through this small
protocol, so tests and local runs use ``FakeIdentityProvider`` and need no AWS account.

Error codes are fixed strings; nothing from the provider's message reaches a client, and a
sign-in failure never says whether the address exists.
"""

from __future__ import annotations

import asyncio
import base64
import json
import secrets
from dataclasses import dataclass
from typing import Any, Protocol


class AuthError(Exception):
    """A sign-in step failed. ``code`` is one of the fixed strings the API turns into a response."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Identity:
    sub: str
    email: str
    name: str


class IdentityProvider(Protocol):
    async def sign_up(self, email: str, password: str, name: str) -> bool:
        """Create the user. Returns True when the address is already confirmed (auto-confirm)."""

    async def confirm(self, email: str, code: str) -> None: ...

    async def login(self, email: str, password: str) -> Identity: ...

    async def forgot(self, email: str) -> None: ...

    async def reset(self, email: str, code: str, new_password: str) -> None: ...

    async def disable(self, email: str) -> None: ...


_CODES = {
    "NotAuthorizedException": "invalid_credentials",
    "UserNotFoundException": "invalid_credentials",
    "UserNotConfirmedException": "not_confirmed",
    "PasswordResetRequiredException": "reset_required",
    "InvalidPasswordException": "weak_password",
    "CodeMismatchException": "bad_code",
    "ExpiredCodeException": "bad_code",
    "LimitExceededException": "rate_limited",
    "TooManyRequestsException": "rate_limited",
    "TooManyFailedAttemptsException": "rate_limited",
    "InvalidParameterException": "invalid_request",
}


def _classify(exc: Exception) -> AuthError:
    name = type(exc).__name__
    return AuthError(_CODES.get(name, "unavailable"))


class CognitoIdentityProvider:
    """Amazon Cognito user pool, through boto3 in worker threads (the SDK is synchronous)."""

    def __init__(
        self, pool_id: str, client_id: str, region: str, *, auto_confirm: bool = False
    ) -> None:
        self._pool = pool_id
        self._client_id = client_id
        self._region = region
        self._auto_confirm = auto_confirm
        self._cli: Any = None

    def _client(self) -> Any:
        if self._cli is None:
            import boto3
            from botocore.config import Config

            self._cli = boto3.client(
                "cognito-idp",
                region_name=self._region,
                config=Config(connect_timeout=3, read_timeout=8, retries={"max_attempts": 2}),
            )
        return self._cli

    async def _call(self, fn: str, **kw: Any) -> Any:
        try:
            return await asyncio.to_thread(getattr(self._client(), fn), **kw)
        except Exception as exc:  # boto3 raises many classes; all are mapped to fixed codes
            raise _classify(exc) from None

    async def sign_up(self, email: str, password: str, name: str) -> bool:
        try:
            res = await self._call(
                "sign_up",
                ClientId=self._client_id,
                Username=email,
                Password=password,
                UserAttributes=[{"Name": "email", "Value": email}, {"Name": "name", "Value": name}],
            )
        except AuthError as err:
            # An address that already exists must look like any other sign-up.
            if err.code != "invalid_request":
                raise
            return False
        confirmed = bool(res.get("UserConfirmed"))
        if self._auto_confirm and not confirmed:
            await self._call("admin_confirm_sign_up", UserPoolId=self._pool, Username=email)
            await self._call(
                "admin_update_user_attributes",
                UserPoolId=self._pool,
                Username=email,
                UserAttributes=[{"Name": "email_verified", "Value": "true"}],
            )
            confirmed = True
        return confirmed

    async def confirm(self, email: str, code: str) -> None:
        await self._call(
            "confirm_sign_up", ClientId=self._client_id, Username=email, ConfirmationCode=code
        )

    async def login(self, email: str, password: str) -> Identity:
        res = await self._call(
            "initiate_auth",
            ClientId=self._client_id,
            AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": email, "PASSWORD": password},
        )
        result = res.get("AuthenticationResult")
        if not result or "IdToken" not in result:
            raise AuthError("unavailable")  # a challenge (for example MFA) is not wired yet
        claims = _claims(result["IdToken"])
        sub, mail = claims.get("sub"), claims.get("email")
        if not isinstance(sub, str) or not isinstance(mail, str):
            raise AuthError("unavailable")
        name = claims.get("name")
        return Identity(sub, mail.lower(), name if isinstance(name, str) and name else mail)

    async def forgot(self, email: str) -> None:
        try:
            await self._call("forgot_password", ClientId=self._client_id, Username=email)
        except AuthError as err:
            if err.code not in ("invalid_credentials", "invalid_request"):
                raise  # an unknown address is not revealed

    async def reset(self, email: str, code: str, new_password: str) -> None:
        await self._call(
            "confirm_forgot_password",
            ClientId=self._client_id,
            Username=email,
            ConfirmationCode=code,
            Password=new_password,
        )

    async def disable(self, email: str) -> None:
        await self._call("admin_disable_user", UserPoolId=self._pool, Username=email)


def _claims(jwt: str) -> dict[str, Any]:
    """The claims of a token Cognito returned directly over TLS (not a token from a client)."""
    try:
        body = jwt.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except (IndexError, ValueError):
        raise AuthError("unavailable") from None
    return data if isinstance(data, dict) else {}


class FakeIdentityProvider:
    """In memory, for tests and for local runs without AWS. The verification code is always 123456."""  # noqa: E501

    CODE = "123456"

    def __init__(self, *, auto_confirm: bool = False) -> None:
        self.auto_confirm = auto_confirm
        self.users: dict[str, dict[str, Any]] = {}
        self.disabled: set[str] = set()

    async def sign_up(self, email: str, password: str, name: str) -> bool:
        if len(password) < 10:
            raise AuthError("weak_password")
        if email not in self.users:
            self.users[email] = {
                "password": password,
                "name": name,
                "sub": secrets.token_hex(8),
                "confirmed": self.auto_confirm,
            }
        return bool(self.users[email]["confirmed"])

    async def confirm(self, email: str, code: str) -> None:
        user = self.users.get(email)
        if user is None or code != self.CODE:
            raise AuthError("bad_code")
        user["confirmed"] = True

    async def login(self, email: str, password: str) -> Identity:
        user = self.users.get(email)
        if user is None or user["password"] != password or email in self.disabled:
            raise AuthError("invalid_credentials")
        if not user["confirmed"]:
            raise AuthError("not_confirmed")
        return Identity(user["sub"], email, user["name"])

    async def forgot(self, email: str) -> None:
        return None

    async def reset(self, email: str, code: str, new_password: str) -> None:
        user = self.users.get(email)
        if user is None or code != self.CODE:
            raise AuthError("bad_code")
        if len(new_password) < 10:
            raise AuthError("weak_password")
        user["password"] = new_password

    async def disable(self, email: str) -> None:
        self.disabled.add(email)
