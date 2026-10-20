# Matrix authentication and own-device signing

The Matrix adapter owns its authentication and E2EE identity policy. Basic
password login MUST remain usable without importing the Matrix SDK.

A persisted account signing identity MUST be reused when its master and
self-signing keys match the homeserver. A fresh device MUST publish its own
keys before uploading its self-signature. A rejected device-key upload MUST
NOT authorize replacing the account identity or reporting verification success.

Verification success requires the server-visible master → self-signing →
current-device chain. Ordinary runtime startup MUST NOT create or rotate an
account signing identity. A lost or conflicting identity requires explicit,
authenticated recovery. Local-only trust flags are not server verification.

## Native OAuth device sessions

`auth login --oauth` MUST use the Matrix OAuth scopes and RFC 8628 device
authorization flow. Discovery MUST follow Matrix homeserver delegation and
validate HTTPS metadata before client registration. Device and token endpoints,
revocation endpoints, and displayed approval addresses MUST belong to the
advertised issuer. Endpoint redirects MUST NOT carry credentials elsewhere.
An explicitly registered public client ID MAY replace dynamic registration.

The accepted token MUST have the requested API/device scopes and a renewable
lifetime. Before saving it, `whoami` MUST confirm the approved user and allocated
device and MUST reject guest sessions. Invalid or unpersisted issued tokens
MUST be revoked where possible. Server error bodies MUST NOT enter diagnostics.
OAuth failures MUST NOT silently fall back to password authentication.

Managed sessions use the [version-1 OAuth credential envelope](../schemas/matrix-oauth-credentials.schema.json). Tokens MUST NOT
appear in object representations or auth-status output. File publication MUST
be atomic and private; invalid or oversized records MUST NOT replace a usable
record. Token renewal MUST serialize across tasks and processes, reload the
latest saved token before rotation, and finish durable publication before
propagating cancellation. Failed persistence MUST revoke the unpersisted result
where possible. A revoked or changed session MUST NOT be replaced implicitly.

Runtime MUST attach renewal before the SDK's HTTP attempt boundary, including
SDK-managed retries. Requests MUST use the configured HTTPS homeserver and
relative paths, replace stale Authorization headers, remove query tokens, and
reject custom Authorization overrides. Only the sidecar that supplied the
configured credentials may become their renewal authority.

Signing setup MAY handle a browser UIA challenge for a signing-key upload that
the identity policy has permitted. First bootstrap MAY request browser approval
without requiring an identity-reset flag. Replacing an existing identity MUST
require explicit reset authorization and password or OAuth authentication.
Runtime MUST NOT display or approve UIA challenges. Setup failure MUST preserve
a saved OAuth session and its signing state for recovery without another login.

Repeated OAuth login MUST reuse the saved device. Logout MUST revoke the
session before removing credentials, retain credentials when revocation fails,
and preserve the E2EE store. Credential-method changes MUST NOT overwrite a
managed OAuth record without logout.

References: [Matrix OAuth API](https://spec.matrix.org/latest/client-server-api/#oauth-20-api),
[RFC 8628](https://www.rfc-editor.org/rfc/rfc8628),
[RFC 7591](https://www.rfc-editor.org/rfc/rfc7591), and
[RFC 7009](https://www.rfc-editor.org/rfc/rfc7009).
