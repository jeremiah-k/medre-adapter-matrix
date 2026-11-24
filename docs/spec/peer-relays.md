# Matrix peer relays

The adapter trusts no other relays or bridges by default: the configured
`trusted_relays` list defaults to empty. An allowlisted room message that
carries the reserved MEDRE namespace (`content.medre`) MUST be admitted only
when its wire sender is exactly one of the configured entries. Sender
identity is the event's wire sender, matched by exact string equality with
no case folding and no wildcard expansion; an entry MUST be a full Matrix
user ID (leading `@`, a `:` separator, no whitespace, no `*`). Configuration
validation MUST reject any other shape.

The gate decision MUST precede decoding and MUST be derived from raw event
content alone, so a missing, corrupt, or unsupported-version envelope cannot
pass through the gate by failing canonicalization. A message without the
namespace MUST never be gated, in any configuration.

An admitted message whose envelope is missing, malformed, or not the
supported schema version MUST be dropped and MUST NOT be downgraded to an
ordinary message. The untrusted and invalid drop reasons MUST be counted
separately. Each drop MUST emit at most one warning per sender and reason
per interval, naming the sender, room, and event; the untrusted warning
MUST state the remedy (adding the exact user ID to `trusted_relays`) and
the invalid warning MUST direct the operator to check the peer relay's
version. Neither warning MUST include envelope or claim contents.

Listing a relay MUST NOT grant authority. An embedded envelope is an
untrusted relay hint: it MUST NOT bind relations, rewrite attribution, or
authorize any mutation, and canonical body, sender, and relations continue
to come only from the wire event. The loop-origin hint (an envelope whose
`source_adapter` equals this adapter's ID) MUST suppress publication only
when the wire sender is this adapter's own account or a listed peer; any
other sender's envelope is already dropped by the gate, so no
unauthenticated claim can cause a silent drop.

Diagnostics MUST report the drop counters and a count of configured
trusted relays. Diagnostics MUST NOT report relay user IDs.
