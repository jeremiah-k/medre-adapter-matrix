# Matrix relay attribution

An omitted or null `relay_prefix` selects automatic attribution. Outbound text
uses `"[{sender}/{origin_label}]: "` when a source origin label is available and
`"[{sender}]: "` otherwise. The full sender name is preserved. Unavailable names
fall back to native sender identity. Nonprintable names use the identity fallback.

A configured string MUST take precedence; `relay_prefix: ""` MUST disable the
prefix. The same visible attribution MUST appear in plain and HTML bodies, with
HTML escaped as content. Automatic prefixes MUST NOT rewrite canonical payloads
or MMRelay's `meshtastic_text`, long-name, short-name or packet metadata.
Reaction emote fallbacks share this policy; native reactions retain native wire
semantics. Configured output budgets still apply after complete composition.

## Unbound reply context

A text reply whose target cannot be represented as a destination-scoped Matrix
event ID MUST retain its answer and readable parent context. It MUST NOT invent
a Matrix relation from a canonical ID, a different adapter, or another room.
The renderer MUST record the `relation_reply` fallback and preserve reply
precedence when another relation appears first. This fallback MUST NOT change
the stored event, its relation tuple, or its source attribution.

When MMRelay interoperability is enabled for the source, an unbound reply MUST
retain its supplied `meshtastic_replyId` independently of the Matrix-native
relation. Relation metadata takes precedence over the native interop field.
Absent compatibility metadata MUST NOT be replaced with a canonical ID or a
foreign native reference. The original `meshtastic_text` MUST remain unchanged.

## Text presentation

Text messages and effective text edits MAY carry canonical semantic formatting.
The codec MUST admit only bounded, supported HTML whose decoded visible text
matches the authoritative plain body. Unknown markup, unsafe links, malformed
nesting, or inconsistent bodies MUST retain unchanged plain text. A valid reply
fallback MAY be removed from HTML only when a reply or thread relation exists.

The renderer MUST escape content and link attributes independently. Prefixes
MUST remain outside source styles. Formatted content uses the authoritative body; unformatted legacy
payloads retain their text-alias precedence. Changed relation fallbacks and truncated content MUST NOT reuse source
span offsets. Block styles with an inline attribution prefix MUST fall back to
plain presentation when their complete-line bounds cannot be preserved.
Only supported semantic tags are emitted; source HTML attributes and arbitrary
markup MUST NOT be forwarded. Formatting does not establish identity or change
relation ownership, native metadata, or delivery confirmation.
