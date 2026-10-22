# Encryption readiness

A connected, authenticated and syncing session MUST NOT report healthy when
configured encryption is inactive. e2ee_optional MAY retain unencrypted-room
operation, but its health MUST be degraded while crypto is unavailable.

Missing dependencies MUST produce an actionable startup warning and a crypto
error diagnostic naming the matrix[e2e] installation. Unsuccessful initialization
MUST also be visible when setup returns without an active Olm/store pair.

Encrypted-room sends MUST remain refused whenever crypto is inactive. Optional
mode MUST NOT authorize downgrading encrypted-room content to plaintext.
e2ee_required retains its fail-closed startup behavior. Credential identity and
cross-signing reset policy remain independent of package installation.

When encryption initialization is inactive, later undecryptable event observations
MUST NOT replace the startup dependency or repair diagnosis. Undecryptable-event
counters remain available, and event-specific diagnostics MAY replace the last
error only while crypto is operational or no startup diagnosis exists.
