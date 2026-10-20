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
