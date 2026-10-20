"""Matrix OAuth device authorization, independent of the transport SDK.

Implements Matrix's OAuth scopes and RFC 8628 device approval. This module
contains no SDK imports or persistent state; callers own credential storage.
"""

from __future__ import annotations

import asyncio
import math
import secrets
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Protocol
from urllib.parse import urlsplit

DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
API_SCOPE = "urn:matrix:client:api:*"
DEVICE_SCOPE = "urn:matrix:client:device:"
MAX_RESPONSE_BYTES = 65536


class MatrixOAuthError(RuntimeError):
    """An actionable OAuth failure with no server-controlled secret details."""


class OAuthNetworkError(MatrixOAuthError):
    """A bounded request failed before obtaining an OAuth response."""


def https_url(value: str) -> str:
    """Validate an HTTPS endpoint without credentials, query or fragment."""
    try:
        parsed = urlsplit(value)
        valid = (
            value == value.strip()
            and not any(ord(char) <= 32 or ord(char) == 127 for char in value)
            and parsed.scheme == "https"
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
            and "\\" not in value
        )
        _ = parsed.port  # Validate malformed ports before creating a request.
    except ValueError:
        valid = False
    if not valid:
        raise MatrixOAuthError(
            "OAuth requires an HTTPS URL without credentials or query parameters."
        )
    return value.rstrip("/")


def same_origin(url: str, issuer: str) -> bool:
    left, right = urlsplit(url), urlsplit(issuer)
    return (left.scheme, left.hostname, left.port or 443) == (
        right.scheme,
        right.hostname,
        right.port or 443,
    )


def _text(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    if (
        not isinstance(value, str)
        or not value
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        raise MatrixOAuthError(f"OAuth response has an invalid {key} field.")
    return value


def _positive(data: Mapping[str, Any], key: str) -> float:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MatrixOAuthError(f"OAuth response has an invalid {key} field.")
    try:
        number = float(value)
    except OverflowError:
        raise MatrixOAuthError(f"OAuth response has an invalid {key} field.") from None
    if not math.isfinite(number) or number <= 0:
        raise MatrixOAuthError(f"OAuth response has an invalid {key} field.")
    return number


def _endpoint(data: Mapping[str, Any], key: str, issuer: str) -> str:
    result = https_url(_text(data, key))
    if not same_origin(result, issuer):
        raise MatrixOAuthError("OAuth endpoints must belong to the advertised issuer.")
    return result


@dataclass(frozen=True)
class OAuthResponse:
    status: int
    body: Mapping[str, Any] = field(repr=False)

    def success(self) -> Mapping[str, Any]:
        if self.status not in (200, 201):
            raise MatrixOAuthError(f"OAuth request was rejected (HTTP {self.status}).")
        return self.body


class OAuthTransport(Protocol):
    async def request(
        self,
        method: str,
        url: str,
        *,
        form: Mapping[str, str] | None = None,
        document: Mapping[str, Any] | None = None,
        token: str | None = None,
    ) -> OAuthResponse: ...


class OAuthHTTP:
    async def request(
        self,
        method: str,
        url: str,
        *,
        form: Mapping[str, str] | None = None,
        document: Mapping[str, Any] | None = None,
        token: str | None = None,
    ) -> OAuthResponse:
        import json
        import aiohttp

        https_url(url)
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30)
            ) as session:
                async with session.request(
                    method,
                    url,
                    data=form,
                    json=document,
                    allow_redirects=False,
                    headers={"Authorization": f"Bearer {token}"} if token else None,
                ) as response:
                    raw = bytearray()
                    async for chunk in response.content.iter_chunked(8192):
                        raw.extend(chunk)
                        if len(raw) > MAX_RESPONSE_BYTES:
                            raise MatrixOAuthError(
                                "OAuth response exceeds the size limit."
                            )
                    if response.status in (301, 302, 303, 307, 308):
                        raise MatrixOAuthError("OAuth endpoint redirects are refused.")
                    if not raw and response.status in (200, 404):
                        body: object = {}
                    else:
                        try:
                            body = json.loads(raw)
                        except (ValueError, UnicodeDecodeError):
                            if response.status >= 400:
                                body = {}
                            else:
                                raise MatrixOAuthError(
                                    "OAuth endpoint returned invalid JSON."
                                ) from None
                    if not isinstance(body, dict):
                        raise MatrixOAuthError(
                            "OAuth endpoint must return a JSON object."
                        )
                    return OAuthResponse(response.status, body)
        except (aiohttp.ClientError, TimeoutError):
            raise OAuthNetworkError(
                "OAuth request failed; check connectivity and retry."
            ) from None


@dataclass(frozen=True)
class OAuthSession:
    homeserver: str
    user_id: str
    device_id: str
    issuer: str
    token_endpoint: str
    revocation_endpoint: str
    client_id: str
    scope: str
    expires_at: float
    refresh_at: float
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)

    def __post_init__(self) -> None:
        https_url(self.homeserver)
        https_url(self.issuer)
        for name in ("token_endpoint", "revocation_endpoint"):
            _endpoint({name: getattr(self, name)}, name, self.issuer)
        for name in (
            "user_id",
            "device_id",
            "client_id",
            "access_token",
            "refresh_token",
        ):
            _text({name: getattr(self, name)}, name)
        if not self.user_id.startswith("@") or ":" not in self.user_id:
            raise MatrixOAuthError("OAuth identity must be a complete Matrix user ID.")
        if any(c.isspace() for c in self.device_id) or set(self.scope.split()) != {
            API_SCOPE,
            DEVICE_SCOPE + self.device_id,
        }:
            raise MatrixOAuthError(
                "OAuth scopes do not match the requested Matrix device."
            )
        expiry = _positive({"expires_at": self.expires_at}, "expires_at")
        renewal = _positive({"refresh_at": self.refresh_at}, "refresh_at")
        if renewal >= expiry:
            raise MatrixOAuthError("OAuth refresh deadline must precede expiry.")

    def credentials(self) -> dict[str, Any]:
        return {
            "auth_type": "oauth",
            "homeserver": self.homeserver,
            "user_id": self.user_id,
            "device_id": self.device_id,
            "access_token": self.access_token,
            "oauth": {
                "version": 1,
                "issuer": self.issuer,
                "client_id": self.client_id,
                "token_endpoint": self.token_endpoint,
                "revocation_endpoint": self.revocation_endpoint,
                "scope": self.scope,
                "expires_at": self.expires_at,
                "refresh_at": self.refresh_at,
                "refresh_token": self.refresh_token,
            },
        }

    @classmethod
    def parse(cls, data: Mapping[str, Any]) -> OAuthSession:
        oauth = data.get("oauth")
        if (
            data.get("auth_type") != "oauth"
            or not isinstance(oauth, dict)
            or type(oauth.get("version")) is not int
            or oauth["version"] != 1
        ):
            raise MatrixOAuthError("Unsupported or malformed OAuth credential record.")
        return cls(
            **{
                key: _text(data, key)
                for key in ("homeserver", "user_id", "device_id", "access_token")
            },
            **{
                key: _text(oauth, key)
                for key in (
                    "issuer",
                    "client_id",
                    "token_endpoint",
                    "revocation_endpoint",
                    "scope",
                    "refresh_token",
                )
            },
            expires_at=_positive(oauth, "expires_at"),
            refresh_at=_positive(oauth, "refresh_at"),
        )


@dataclass(frozen=True)
class DeviceAuthorization:
    url: str
    code: str = field(repr=False)


class MatrixOAuthClient:
    def __init__(
        self,
        transport: OAuthTransport | None = None,
        *,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.transport = transport or OAuthHTTP()
        self.clock, self.monotonic, self.sleep = clock, monotonic, sleep

    async def discover(self, homeserver: str) -> tuple[str, Mapping[str, Any]]:
        from medre.adapters.matrix.auth import _normalize_homeserver

        server = https_url(_normalize_homeserver(homeserver))
        parts = urlsplit(server)
        discovery = await self.transport.request(
            "GET", f"https://{parts.netloc}/.well-known/matrix/client"
        )
        if discovery.status == 200:
            delegated = discovery.body.get("m.homeserver")
            if isinstance(delegated, dict):
                server = https_url(_text(delegated, "base_url"))
        elif discovery.status != 404:
            discovery.success()
        metadata = await self.transport.request(
            "GET", server + "/_matrix/client/v1/auth_metadata"
        )
        if metadata.status == 404:
            metadata = await self.transport.request(
                "GET",
                server + "/_matrix/client/unstable/org.matrix.msc2965/auth_metadata",
            )
        body = metadata.success()
        grants = body.get("grant_types_supported", [])
        if (
            not isinstance(grants, list)
            or not all(isinstance(grant, str) for grant in grants)
            or not {DEVICE_GRANT, "refresh_token"}.issubset(grants)
        ):
            raise MatrixOAuthError(
                "The homeserver does not advertise renewable OAuth device login."
            )
        methods = body.get("token_endpoint_auth_methods_supported", ["none"])
        if not isinstance(methods, list) or "none" not in methods:
            raise MatrixOAuthError("The OAuth server does not support public clients.")
        issuer = https_url(_text(body, "issuer"))
        for key in (
            "device_authorization_endpoint",
            "token_endpoint",
            "revocation_endpoint",
        ):
            _endpoint(body, key, issuer)
        return server, body

    async def authorize(
        self,
        homeserver: str,
        display: Callable[[DeviceAuthorization], None],
        *,
        expected_user: str | None = None,
        client_id: str | None = None,
    ) -> OAuthSession:
        server, metadata = await self.discover(homeserver)
        issuer = _text(metadata, "issuer")
        if client_id is None:
            registration = (
                await self.transport.request(
                    "POST",
                    _endpoint(metadata, "registration_endpoint", issuer),
                    document={
                        "client_name": "MEDRE",
                        "application_type": "native",
                        "client_uri": "https://github.com/jeremiah-k/medre",
                        "token_endpoint_auth_method": "none",
                        "grant_types": [DEVICE_GRANT, "refresh_token"],
                    },
                )
            ).success()
            if registration.get("token_endpoint_auth_method", "none") != "none":
                raise MatrixOAuthError(
                    "OAuth registration requires an unsupported client secret."
                )
            client_id = _text(registration, "client_id")
        device_id = "MEDRE_" + secrets.token_hex(16)
        scope = API_SCOPE + " " + DEVICE_SCOPE + device_id
        approval = (
            await self.transport.request(
                "POST",
                _endpoint(metadata, "device_authorization_endpoint", issuer),
                form={"client_id": client_id, "scope": scope},
            )
        ).success()
        code = _text(approval, "device_code")
        url = _endpoint(approval, "verification_uri", issuer)
        deadline = self.monotonic() + min(900.0, _positive(approval, "expires_in"))
        interval = _positive({"interval": approval.get("interval", 5)}, "interval")
        display(DeviceAuthorization(url, _text(approval, "user_code")))
        token_url = _endpoint(metadata, "token_endpoint", issuer)
        revoke_url = _endpoint(metadata, "revocation_endpoint", issuer)
        while self.monotonic() < deadline:
            await self.sleep(min(interval, deadline - self.monotonic()))
            if self.monotonic() >= deadline:
                break
            issued_at = self.clock()
            try:
                async with asyncio.timeout(deadline - self.monotonic()):
                    response = await self.transport.request(
                        "POST",
                        token_url,
                        form={
                            "client_id": client_id,
                            "device_code": code,
                            "grant_type": DEVICE_GRANT,
                        },
                    )
            except (OAuthNetworkError, TimeoutError):
                interval = max(5.0, interval * 2)
                continue
            error = response.body.get("error")
            if response.status in (400, 403) and error == "authorization_pending":
                continue
            if response.status in (400, 403) and error == "slow_down":
                interval += 5
                continue
            if response.status == 429 or response.status >= 500:
                interval += 5
                continue
            if error == "access_denied" and response.status in (400, 403):
                raise MatrixOAuthError(
                    "Browser approval was declined; repeat login when ready."
                )
            if error == "expired_token" and response.status in (400, 403):
                raise MatrixOAuthError(
                    "Browser approval expired; repeat the login command."
                )
            tokens = response.success()
            try:
                access, refresh, expires, renew = self._tokens(tokens, scope, issued_at)
                whoami = (
                    await self.transport.request(
                        "GET",
                        server + "/_matrix/client/v3/account/whoami",
                        token=access,
                    )
                ).success()
                user = _text(whoami, "user_id")
                if (
                    whoami.get("device_id") != device_id
                    or whoami.get("is_guest") is True
                    or not matches_user(user, expected_user)
                ):
                    raise MatrixOAuthError(
                        "The approved account or device differs from the requested login."
                    )
                return OAuthSession(
                    server,
                    user,
                    device_id,
                    issuer,
                    token_url,
                    revoke_url,
                    client_id,
                    scope,
                    expires,
                    renew,
                    access,
                    refresh,
                )
            except BaseException:
                await self._revoke_tokens(tokens, revoke_url, client_id)
                raise
        raise MatrixOAuthError("Browser approval expired; retry the login command.")

    def _tokens(
        self,
        body: Mapping[str, Any],
        scope: str,
        issued: float,
        previous_refresh: str | None = None,
    ) -> tuple[str, str, float, float]:
        if str(body.get("token_type", "")).lower() != "bearer" or set(
            str(body.get("scope", scope)).split()
        ) != set(scope.split()):
            raise MatrixOAuthError("OAuth returned an unexpected token type or scope.")
        lifetime = _positive(body, "expires_in")
        refresh = (
            _text(body, "refresh_token")
            if "refresh_token" in body
            else previous_refresh
        )
        if not refresh:
            raise MatrixOAuthError("OAuth did not issue a refresh token.")
        return (
            _text(body, "access_token"),
            refresh,
            issued + lifetime,
            issued + lifetime * 0.8,
        )

    async def refresh(self, session: OAuthSession) -> OAuthSession:
        issued = self.clock()
        response = (
            await self.transport.request(
                "POST",
                session.token_endpoint,
                form={
                    "grant_type": "refresh_token",
                    "refresh_token": session.refresh_token,
                    "client_id": session.client_id,
                },
            )
        ).success()
        try:
            access, refresh, expires, renew = self._tokens(
                response, session.scope, issued, session.refresh_token
            )
            return replace(
                session,
                access_token=access,
                refresh_token=refresh,
                expires_at=expires,
                refresh_at=renew,
            )
        except BaseException:
            await self._revoke_tokens(
                response, session.revocation_endpoint, session.client_id
            )
            raise

    async def _revoke_tokens(
        self, tokens: Mapping[str, Any], endpoint: str, client_id: str
    ) -> None:
        # Cleanup is bounded and never replaces the original failure.
        for name in ("refresh_token", "access_token"):
            token = tokens.get(name)
            if isinstance(token, str) and token:
                try:
                    await self.transport.request(
                        "POST",
                        endpoint,
                        form={
                            "client_id": client_id,
                            "token": token,
                            "token_type_hint": name,
                        },
                    )
                except Exception:
                    pass

    async def revoke(self, session: OAuthSession) -> None:
        for name in ("refresh_token", "access_token"):
            (
                await self.transport.request(
                    "POST",
                    session.revocation_endpoint,
                    form={
                        "client_id": session.client_id,
                        "token": getattr(session, name),
                        "token_type_hint": name,
                    },
                )
            ).success()


def matches_user(actual: str, expected: str | None) -> bool:
    if expected is None:
        return True
    if expected.startswith("@") and ":" in expected:
        return actual == expected
    return actual.split(":", 1)[0] == "@" + expected.removeprefix("@")
