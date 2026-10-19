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
