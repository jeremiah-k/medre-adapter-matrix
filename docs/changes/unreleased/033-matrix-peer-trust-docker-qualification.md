# Qualify the peer-device trust gate on a real encrypted room

One ordered Docker Synapse scenario drives `MatrixAdapter.deliver` on a real
encrypted room with genuine Megolm crypto and closes the
real-encrypted-peer-qualification precondition for the `verification_policy`
gate. The first strict delivery on a fresh session prepares room membership
and is refused with the unverified peer device named; verifying the device
in MEDRE's own nio store lets strict deliver and the peer client decrypts
the payload; a trust reset refuses the next send; local trust survives an
adapter restart on the same store; the default delivers without a peer-device trust scan or trust-gap warning.

The scenario runs under the `docker` tier (deselected by default) and needs
`mindroom-nio[e2e]`. Each adapter instance owns its own Matrix account so
two nio stores never share one device identity, and the verdict JSON
records every phase with redacted device IDs. Loopback only: the scenario
proves no federation, external network, cross-signing ceremony, or
recipient-side read. `permissive` remains the default; flipping the default
still requires the separate review and an operator peer-verification
workflow.
