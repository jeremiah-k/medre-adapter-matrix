# Add explicit peer-device verification policy for encrypted sends

Introduce `verification_policy` on the Matrix adapter configuration with two
states. `permissive` (the default) preserves encrypted delivery without
scanning local peer-device trust or warning about routine trust gaps.
`strict` refuses such sends at the encrypted-send authorization point before
any SDK send call, with a retryable, actionable error naming the recipient,
the offending devices, and the verification remedy. The adapter never
auto-verifies peer devices, never falls back to a weaker transport, and never
sends anyway. Trust state is re-read per delivery, so verification and device
resets between attempts are honored; the policy binds only to encrypted sends
and leaves plaintext delivery, ephemeral typing, and redactions untouched.
`strict` is NOT planned as the default before a separately qualified local
peer-verification workflow exists; flipping the default is a deliberate,
separately reviewed change. Neither policy state
proves delivery: an encrypted send accepted by the homeserver is ciphertext
acceptance, not a delivery receipt.

Trust preparation fetches missing membership and pending device keys before
strict authorization, using the durable SDK cache when the room projection is
partial. Own-account sibling devices remain recipients; only the current device
is excluded. Failures stay retryable with combined membership/device-key guidance; the
pinned `AsyncClient.keys_query` delegates to the durable owner's locked,
transactional key-query path when durable sync is active. SDK trust-race refusals identify safe room/device references without key material.
Client replacement during refresh invalidates the report, and diagnostic
identifiers escape terminal controls without altering recipient identities.
