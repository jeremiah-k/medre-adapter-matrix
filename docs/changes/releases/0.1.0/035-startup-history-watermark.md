# Startup history watermark

Inbound Matrix events whose origin server timestamp predates the adapter's
start by more than `startup_backlog_suppress_seconds` (default 60 seconds)
are startup history and are suppressed, whichever path delivered them —
live sync, durable catch-up, or gap recovery. The check precedes decode,
so messages, reactions, and edits are equally covered, and it reuses the
transport-neutral `should_suppress_startup_backlog` policy the Meshtastic
adapter already applies. The window covers homeserver federation lag and
minor clock drift; `0` disables the suppression. Suppressed events count
under `inbound_suppressed_stale` in diagnostics.

Events inside the tolerance window may still forward even when created before
startup. Missing, non-finite, and out-of-range origin timestamps do not count
as history; they cannot prevent durable checkpoint progress. Stopping or
failing startup retires the baseline so a later session starts with new state.

Invalid suppression windows, including integers outside float range, produce a
typed configuration error. The exact cutoff remains inclusive for forwarding:
only timestamps strictly older than the tolerance are classified as history.
