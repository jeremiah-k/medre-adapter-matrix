# Keep normal encrypted delivery quiet and independently cross-sign MEDRE

The default Matrix peer-device authorization policy preserves encrypted
homeserver delivery without running a peer-device trust scan on every send,
incrementing an unverified-delivery counter, or emitting a warning about
unactionable local peer-device trust gaps. Remove the dead warning formatter,
per-room warning cache and its memory/counter diagnostics. No policy-mode
name is emitted by the runtime refusal message. Preserve the explicitly
selected `strict` authorization gate, fresh per-attempt trust checks, and
nio's secondary `ignore_unverified_devices=False` enforcement. Own-device
cross-signing and its safety diagnostics are unchanged: signing the bridge's
current device is not verification of other users' devices. SDK-backed and
real-encrypted-peer test tiers remain separately gated.
