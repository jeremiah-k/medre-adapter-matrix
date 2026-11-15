# Remove the MMRelay interop surface

The Matrix adapter no longer emits or reads the legacy MMRelay wire surface:
flat `meshtastic_*` content keys, the `native.interop.mmrelay` metadata
fallback, and the `mmrelay_compatibility` source-config switch are gone from
the renderer and event-shape helpers. Canonical `content.medre` provenance,
canonical attribution prefixes, and ordinary Matrix-room ingestion are
unchanged.

Reaction fallback shaping is repaired in the same change: the emote body no
longer starts with a stray newline, and the quoted target preview resolves
from canonical relation enrichment (core `original_text` / `fallback_text`)
with an honest "an earlier message" fallback instead of empty quotes.
