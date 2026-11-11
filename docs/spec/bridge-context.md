# Cross-platform context in Matrix

Safe and full metadata modes MUST include bounded, versioned source and relation
context when its fields are valid. Off and minimal modes MUST omit this context.
The source records its platform, configured adapter, conversation, native
reference, sender claim and verification description independently of prefix
presentation. Relation hints retain canonical and scoped native target IDs when
available. Raw SDK blobs, credentials and relation binding authority MUST NOT be
copied into this context.

Inbound context MUST be validated for its supported version, field types and
size bounds. Invalid context MUST be ignored without discarding the Matrix
message. Unknown fields MUST NOT be preserved. Valid context MUST remain a
remote relay claim under Matrix-native metadata; it MUST NOT replace the actual
Matrix sender, source adapter, native reference, or canonical relations. Local
storage evidence remains the authority for native relation binding and mutation
permissions. A sender mention without a target ID MUST NOT become an exact reply.

The builder and normalizer are provided by `medre.core.rendering.bridge_context`.
The Matrix module retains its existing imports for callers. The v1 wire format
matches core's bridge-context schema; this move MUST NOT change metadata modes
or the receiver's native identity and relation authority.

## Public native fact projection

The optional `source.native_facts` object carries one source-owned namespace
and up to twelve bounded string/integer/boolean facts selected by its adapter.
The Matrix renderer uses the exact shared core builder; it must not enumerate
`meshtastic_*` fields or copy opaque native packet maps into Matrix content.
When rendering supplied source attribution, origin-label precedence MUST be the
explicit route/context label, then the attribution snapshot label, then the
configured source label. Only `None` falls through; an explicit empty label
MUST be preserved at each level.

The source attribution (`sender_name`/`sender_short_name`) is generic and
separate from native facts such as portnum. All embedded facts remain untrusted
until an application authorizes the **observed** Matrix sender.

## Explicit reaction keys

Canonical reaction rendering MUST require an explicit nonempty relation key
even when a reply relation is also present, and preserve that exact key. Message bodies, payload emoji, and presentation
fallbacks MUST NOT supply a missing key. The explicitly enabled legacy
presentation mode remains outside this canonical contract.

## Thread and reply intent

A thread relation alone MUST NOT remove authored quote lines or HTML as a reply
fallback. Fallback removal requires a native reply marker. A thread with
`m.in_reply_to` and `is_falling_back` false or absent MUST retain explicit reply
intent alongside its thread relation. A true compatibility-fallback flag MUST
NOT create an explicit reply relation.
