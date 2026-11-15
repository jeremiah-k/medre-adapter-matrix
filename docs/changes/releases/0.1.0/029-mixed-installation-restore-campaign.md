# Qualify native Matrix identity with core and radio state

Extend the real pinned-nio ordinary/durable stopped-capture campaign to restore
an actual MEDRE core SQLite outbox, Matrix reply-reference mapping, adapter
checkpoint and an interrupted radio part alongside the Matrix crypto account.
The campaign asserts the original cryptographic identity and durable SDK stream
on native reopen, and that the reserved radio part cannot acquire a fresh
submission grant. This is local native-storage qualification, not a live
homeserver session, device-firmware export or successful radio delivery.

Seed a nonzero committed durable sequence and a distinct unacknowledged message
batch before capture. Require the restored cursor, acknowledgement sequence,
progress generation and complete pending batch to match before acknowledging
the restored work.
