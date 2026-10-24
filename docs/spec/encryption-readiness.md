# Encryption readiness

A connected, authenticated and syncing session MUST NOT report healthy when
configured encryption is inactive. e2ee_optional MAY retain unencrypted-room
operation, but its health MUST be degraded while crypto is unavailable.

Production durable sync MUST require the SQLite crypto backend, including for
plaintext rooms. Missing backend dependencies MUST fail startup with an
installation diagnosis. The matrix[e2e] extra remains an installation alias;
the base provider includes the backend. A connected standalone optional session
MUST report inactive encryption through its health and startup diagnostics.

Encrypted-room sends MUST remain refused whenever crypto is inactive. Plaintext policy MUST filter encrypted-room or decrypted-message ingress before
canonical admission, even when the SDK backend can decrypt it. Optional
mode MUST NOT authorize downgrading encrypted-room content to plaintext.
e2ee_required retains its fail-closed startup behavior. Credential identity and
cross-signing reset policy remain independent of package installation.

When encryption initialization is inactive, later undecryptable event observations
MUST NOT replace the startup dependency or repair diagnosis. Undecryptable-event
counters remain available, and event-specific diagnostics MAY replace the last
error only while crypto is operational or no startup diagnosis exists.
