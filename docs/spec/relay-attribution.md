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
