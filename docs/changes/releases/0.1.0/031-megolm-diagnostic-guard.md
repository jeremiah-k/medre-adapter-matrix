# Guard malformed Matrix crypto event diagnostic identifiers

Treat missing, empty, or non-text Megolm session IDs as unknown when producing
bounded, privacy-preserving undecryptable-event diagnostics. Preserve the
normal hashed string-ID behavior. Correct the active durable-sync SDK contract
to match the pinned `mindroom-nio[e2e] 1.2.1`. This does not alter crypto
identity, native store ownership, routing or admission semantics.

Malformed Unicode identifiers also retain the unknown marker. Live malformed-ID
regressions check the deduplication key and refuse key-request scheduling, rather
than passing through the pre-live backlog exit.
