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
validate HTTPS metadata before client registration. Credential-free client
well-known discovery MAY follow up to five HTTPS redirects under one total
deadline. Redirect following MUST NOT accept credentials or request bodies.
Device and token endpoints, revocation endpoints, and displayed approval
addresses MUST belong to the advertised issuer. Endpoint redirects MUST NOT carry credentials elsewhere.
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
where possible. Cleanup MUST NOT explicitly revoke token values retained by
the saved session, including when the issuer omits a replacement refresh token.
Authorization servers can invalidate related tokens under RFC 7009, so cleanup
does not guarantee that a saved grant remains usable. A revoked or changed
session MUST NOT be replaced implicitly. Platform-specific credential locks
MUST be released before closing their descriptors, including cancellation
cleanup.

Runtime MUST attach renewal before the SDK's HTTP attempt boundary, including
SDK-managed retries. Requests MUST use the configured HTTPS homeserver and
relative paths, replace stale Authorization headers, remove query tokens, and
reject custom Authorization overrides. Only the sidecar that supplied the
configured credentials may become their renewal authority. Explicitly configured
tokens must still match the current sidecar token; configurations populated by
sidecar fallback retain their source provenance so a restarted session can
follow a refreshed token without taking ownership of an unrelated token.
Replacing a sidecar-supplied token with an explicit override MUST discard its
managed-session provenance. Managed credentials that disappear or change identity
before startup MUST NOT silently become unmanaged tokens. The initial cached
session MUST be loaded under the same lock used for credential rotation.
Cached access tokens MUST be invalidated if the sidecar is replaced or removed.
Browser signing approval MUST receive a larger reconciliation budget than its
approval window. Approval retries MUST bound HTTP requests and response-body
reads by the same deadline. Directory-sync failures after atomic credential
publication MUST NOT be misreported as unpublished tokens.

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


## Password login and runtime signing

Password login without --adapter-id MUST report that it did not prepare
cross-signing. Credentials alone MUST NOT be represented as verified runtime
device signing. Selecting an adapter MUST target its configured E2EE store.
Runtime diagnostics MUST identify the adapter-scoped recovery command. Missing
private signing material MUST NOT trigger implicit account-identity replacement.


## Explicit signing-backup restoration

`auth status --adapter-id ID --config PATH` MUST inspect the selected runtime
store and use the SDK-owned signing filename. It MUST distinguish readable
local keys from server/device verification and MUST NOT expose private keys.
Inspection MUST NOT open a device database or contact the homeserver. Existing
unreadable or invalid files MUST receive preservation/permissions guidance rather
than an import command that requires their absence. SDK/path inspection errors
MUST NOT be presented as proof of missing keys. Runtime guidance MUST distinguish
bootstrap, device-signature repair, account-key recovery and provider failures;
device repair MUST NOT recommend account-identity reset.

`auth restore-signing` MUST validate an operator-selected SDK signing backup
and verify the existing token's account/device before querying account keys.
The backup's master/self-signing keys and master signature MUST match the
existing server identity before local publication. Missing current-device
signatures MAY be repaired by runtime after import; import MUST NOT upload keys,
create a device, replace credentials, or reset the server identity.

Import MUST publish a complete private file into an absent destination without
a check-then-replace fallback. Existing destinations, including racing imports,
MUST remain untouched. Unsupported publication filesystems MUST fail closed.
Directory-sync failures after publication MUST report that the complete file
may already exist. Operators MUST stop the bridge before restoring signing
state. Restoration of account-signing keys does not restore room message keys.
