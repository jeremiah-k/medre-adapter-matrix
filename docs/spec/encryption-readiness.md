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

## Peer-device verification policy

The adapter MUST expose an explicit `verification_policy` with exactly two
states: `permissive` and `strict`. The configured policy binds at the
encrypted-send authorization point, together with the encrypted-room safety
layers, and applies only to encrypted sends (crypto active and the room
affirmatively established as encrypted). Plaintext delivery, ephemeral typing,
and redactions MUST stay outside its scope.

A peer device counts as trusted only when the SDK affirmatively marks it
verified. Unset, blacklisted, or ignored trust states, unreadable trust state,
a recipient with no known device list, and an unavailable device view are all
trust gaps. The adapter MUST NOT auto-verify peer devices, MUST NOT fall back
to a weaker transport, and MUST NOT deliver an authorized encrypted send to a
recipient with trust gaps under `strict` policy: the send MUST be refused
before any SDK send call with a retryable, actionable error naming the room,
the offending devices, and the verification remedy. Trust state MUST be
re-read on every delivery attempt so devices verified or reset between
attempts are honored on the next one.

Under `permissive` policy the same trust gaps MUST produce loud diagnostics —
a warning naming the room, devices, and trust state, plus a diagnostics
counter — without changing delivery behavior. The default is `permissive` for
compatibility; `strict` is intended to become the default in a later release,
and flipping the default is a deliberate separately reviewed change.

Neither policy state proves delivery: an encrypted send accepted by the
homeserver — even to fully verified devices — is ciphertext acceptance, not a
delivery receipt. Peer-device trust MUST NOT be reported as proof that any
peer device received or decrypted a message.

## Health diagnosis

Adapter health MUST expose reason codes for pending initial sync acknowledgement,
reconnection, stale sync, inactive crypto, authentication and transport failures.
Authentication alone MUST NOT establish inbound readiness. A pending resumed
long poll MAY leave startup degraded until its durable batch is acknowledged;
that wait MUST be distinguishable from a crypto failure. The first acknowledged
durable batch MUST be observable in runtime logging. Startup summaries remain
snapshots of startup, rather than claims about subsequent health.
