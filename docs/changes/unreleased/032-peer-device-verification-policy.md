# Add explicit peer-device verification policy for encrypted sends

Introduce `verification_policy` on the Matrix adapter configuration with two
states. `permissive` (the default) preserves current delivery behavior while
making previously silent trust gaps observable: every encrypted send to a
recipient with unverified or unknown devices, or without an available device
view, logs a warning naming the room, devices, and trust state and increments
a `peer_device_trust_warnings` counter exposed in adapter diagnostics.
`strict` refuses such sends at the encrypted-send authorization point before
any SDK send call, with a retryable, actionable error naming the recipient,
the offending devices, and the verification remedy. The adapter never
auto-verifies peer devices, never falls back to a weaker transport, and never
sends anyway. Trust state is re-read per delivery, so verification and device
resets between attempts are honored; the policy binds only to encrypted sends
and leaves plaintext delivery, ephemeral typing, and redactions untouched.
`strict` is intended to become the default in a later release; flipping the
default is a deliberate separately reviewed change. Neither policy state
proves delivery: an encrypted send accepted by the homeserver is ciphertext
acceptance, not a delivery receipt.
