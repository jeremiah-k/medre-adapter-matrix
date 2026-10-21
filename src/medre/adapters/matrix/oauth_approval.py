"""Browser authorization for an explicitly permitted signing-key upload.

The identity policy decides whether an identity may be created or replaced.
This transport wrapper handles the server's UIA challenge only after that
policy permits the upload. It never authorizes runtime identity rotation.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from medre.adapters.matrix.oauth import (
    MAX_RESPONSE_BYTES,
    MatrixOAuthError,
    https_url,
    same_origin,
)

_UPLOAD_PATH = "/_matrix/client/v3/keys/device_signing/upload"


def _challenge(data: object, issuer: str) -> tuple[str, str]:
    if not isinstance(data, dict) or data.get("errcode") not in (
        None,
        "M_UNAUTHORIZED",
    ):
        raise MatrixOAuthError("The server rejected browser authorization for signing.")
    session, flows, params = data.get("session"), data.get("flows"), data.get("params")
    if (
        not isinstance(session, str)
        or not session
        or not session.isprintable()
        or not isinstance(flows, list)
        or not isinstance(params, dict)
    ):
        raise MatrixOAuthError("The signing authorization challenge is malformed.")
    for stage in ("m.oauth", "org.matrix.cross_signing_reset"):
        if not any(
            isinstance(flow, dict) and flow.get("stages") == [stage] for flow in flows
        ):
            continue
        settings = params.get(stage)
        url = settings.get("url") if isinstance(settings, dict) else None
        if (
            not isinstance(url, str)
            or not url.isprintable()
            or any(c.isspace() for c in url)
        ):
            break
        try:
            parsed = urlsplit(url)
            https_url(urlunsplit(parsed._replace(query="", fragment="")))
            if parsed.fragment or not same_origin(url, issuer):
                break
        except ValueError:
            break
        return session, url
    raise MatrixOAuthError(
        "The server did not offer a trusted OAuth signing approval URL."
    )


async def _read_challenge(response: Any, issuer: str) -> tuple[str, str]:
    content = bytearray()
    async for block in response.content.iter_chunked(8192):
        content.extend(block)
        if len(content) > MAX_RESPONSE_BYTES:
            raise MatrixOAuthError(
                "The signing authorization response exceeds the size limit."
            )
    try:
        data = json.loads(content)
    except (ValueError, UnicodeDecodeError):
        raise MatrixOAuthError(
            "The signing authorization response is not JSON."
        ) from None
    return _challenge(data, issuer)


def signing_approval(
    original: Callable[..., Awaitable[Any]],
    issuer: str,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    display: Callable[[str], None] = print,
) -> Callable[..., Awaitable[Any]]:
    async def send(
        method: str,
        path: str,
        data: Any = None,
        headers: Mapping[str, str] | None = None,
        trace_context: Any = None,
        timeout: float | None = None,
    ) -> Any:
        response = await original(method, path, data, headers, trace_context, timeout)
        if (
            method != "POST"
            or urlsplit(path).path != _UPLOAD_PATH
            or response.status == 200
        ):
            return response
        if response.status != 401:
            raise MatrixOAuthError("Signing-key upload was rejected.")
        session, url = await _read_challenge(response, issuer)
        display("Approve this MEDRE signing-key operation in your account browser:")
        display(url)
        display("Keep this command running until approval completes.")
        try:
            document = json.loads(data)
            if not isinstance(document, dict):
                raise ValueError
            document["auth"] = {"session": session}
        except (TypeError, ValueError):
            raise MatrixOAuthError("The signing-key request is malformed.") from None
        deadline = clock() + 120
        while clock() < deadline:
            await sleep(min(5.0, max(0.0, deadline - clock())))
            if clock() >= deadline:
                break
            try:
                async with asyncio.timeout(deadline - clock()):
                    response = await original(
                        method,
                        path,
                        json.dumps(document),
                        headers,
                        trace_context,
                        timeout,
                    )
                    if response.status == 200:
                        return response
                    if response.status != 401 or await _read_challenge(
                        response, issuer
                    ) != (
                        session,
                        url,
                    ):
                        raise MatrixOAuthError(
                            "Signing authorization changed or was rejected; retry."
                        )
            except TimeoutError:
                raise MatrixOAuthError(
                    "Signing approval timed out; repeat login to resume the saved session."
                ) from None
        raise MatrixOAuthError(
            "Signing approval expired; repeat login to resume the saved session."
        )

    return send
