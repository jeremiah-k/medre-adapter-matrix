# Trusted peer relays

The Matrix adapter now ships a peer-relay trust list. The default trusts no
other relay or bridge: an allowlisted room message carrying the MEDRE
envelope (`content.medre`) is dropped unless its sender's exact Matrix user
ID is listed under the adapter instance's new `trusted_relays` setting.
Ordinary Matrix messages without the envelope are never gated.

Operators sharing a room with MMRelay add the peer bot's exact user ID
(`trusted_relays: ["@mmrelay-bot:example.org"]`); the drop warning names the
observed sender and that remedy. A listed relay's malformed or
unsupported-version envelope is dropped under a separate counter with a
warning to check the peer relay's version. Warnings are rate-limited per
sender and reason and never contain claim contents. `medre diagnostics`
gains `trusted_relay_count` (a count, never IDs) and the two drop counters
`inbound_filtered_untrusted_relay` and
`inbound_filtered_invalid_relay_envelope`.

Listing a relay admits its envelopes through the gate only; an embedded
claim never gains authority. The loop-origin echo hint is scoped to this
adapter's own account and listed peers, so a forged envelope naming the
adapter's ID can no longer silently suppress an unlisted sender's message.
A shared room should keep the default `metadata_embedding_mode: safe`: the
`minimal` mode omits the relay context MMRelay requires from a peer claim.
The rules are specified in `docs/spec/peer-relays.md`.
