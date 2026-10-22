# Semantic text presentation

- Convert supported Matrix HTML into bounded canonical text spans when visible
  text matches the plain body, including effective text edits and reply context.
- Render semantic styles with escaped content and link attributes; keep relay
  attribution outside source formatting and preserve authoritative body text.
- Retain plain presentation for unsupported markup, unsafe links, changed
  relation fallbacks, and spans incompatible with final block boundaries.
