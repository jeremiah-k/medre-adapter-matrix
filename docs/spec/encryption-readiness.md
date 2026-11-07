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
a recipient with no known device list, incomplete room membership (neither
`members_synced` nor a complete durable outbound membership cache), and an
unavailable device view are all trust gaps. The adapter MUST NOT auto-verify peer devices, MUST NOT fall back
to a weaker transport, and MUST NOT deliver an authorized encrypted send to a
recipient with trust gaps under `strict` policy: the send MUST be refused
before any SDK send call with a retryable, actionable error naming the room,
the offending devices, and the verification remedy. Under `strict`, trust state MUST be
re-read on every delivery attempt so devices verified or reset between
attempts are honored on the next one. The adapter MUST also
pass `ignore_unverified_devices=False` to nio so a device discovered while
preparing group-session sharing cannot bypass the earlier snapshot. A nio
`OlmUnverifiedDeviceError` before room handoff is a retryable trust refusal,
not a permanent delivery failure.

Under `permissive` policy (the default), encrypted sends MUST preserve
normal delivery behavior without evaluating peer-device trust, querying the
session peer-device store, or emitting routine trust-gap warnings or counters.
This default is not an affirmation that a peer is verified; it reflects the
absence of a supported end-to-end peer-verification workflow. The optional
`strict` gate MUST NOT become the default without a separate review, a
practical local peer-verification/repair workflow and qualification. A nio
`verified` flag describes **local device-store trust**, not trust inherited
from another Matrix client. Own-device cross-signing is independent of peer
verification and MUST remain available in normal operation.

The real-encrypted-peer-qualification precondition has been performed for
this gate: one ordered Docker scenario (`tests/test_matrix_device_trust_docker.py`)
drives `MatrixAdapter.deliver` on a real Synapse homeserver with genuine
Megolm crypto and proves that the first strict delivery on a fresh session
prepares room membership and is refused with the unverified peer device
named, that affirmative local-store verification lets strict deliver while
the peer decrypts the payload, that a trust reset refuses the next send,
that local trust survives an adapter restart on the same store, and that
the default delivers without performing an additional peer-device trust scan.
This satisfies that precondition only: the separate review and a practical
peer-verification/repair workflow remain open, `permissive` stays the
default, and the scenario runs on a Docker loopback — it proves no
federation, external network, cross-signing ceremony, or recipient-side
read.

Neither policy state proves delivery: an encrypted send accepted by the
homeserver — even to fully verified devices — is ciphertext acceptance, not a
delivery receipt. Peer-device trust MUST NOT be reported as proof that any
peer device received or decrypted a message.

Under the opt-in strict gate, membership preparation MUST precede trust
evaluation. Classic sessions use
complete room membership after the SDK's `joined_members` request; durable
sessions reuse the SDK's locked membership preparation, invalidated outbound
membership cache, and active
device store, without rewriting the durable room projection. Pending device-key
queries MUST complete before strict authorization. Durable key refreshes MUST
use the pinned SDK's `AsyncClient.keys_query`, which delegates to its durable
outbound crypto owner under the crypto lock and transactional capture; callers
MUST NOT bypass the owner or invent a second mutable query path. Lookup/query failures and
membership invalidation MUST remain fail-closed; cancellation MUST propagate.
The adapter's current device is excluded from peer checks, but sibling devices
of its own account remain recipients and MUST be affirmatively verified.
Client or durable-owner replacement during preparation MUST invalidate the
report rather than combining trust state from different sessions. Diagnostic
identifiers MUST escape terminal controls without changing policy identity.
SDK refusals after preflight MUST identify the room and available user/device
IDs without key material or raw terminal controls.

## Health diagnosis

Adapter health MUST expose reason codes for pending initial sync acknowledgement,
reconnection, stale sync, inactive crypto, authentication and transport failures.
Authentication alone MUST NOT establish inbound readiness. A pending resumed
long poll MAY leave startup degraded until its durable batch is acknowledged;
that wait MUST be distinguishable from a crypto failure. The first acknowledged
durable batch MUST be observable in runtime logging. Startup summaries remain
snapshots of startup, rather than claims about subsequent health.
