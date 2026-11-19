# Adapter startup info

Core's runtime duck-types an optional `startup_info()` callable on the
built adapter and renders the returned scalar facts at the adapter's
ready transition. The full provider contract, redaction policy, cache
behavior, and presentation rules are owned by the core specification;
this page fixes only what this adapter supplies.

## Facts

The provider MUST format facts the adapter already holds and MUST NOT
perform homeserver or network I/O, acquire resources, or mutate session
state. A synchronous callable is safe on any worker thread: the method
binds the session once and takes one coherent client-identity snapshot
through the session boundary, so a concurrent client replacement cannot
mix identity fields from different clients.

The first row carries the authenticated account identity when held: the
user ID and device ID the logged-in client holds, obtained from the
session's client-identity snapshot. Both are omitted until the client
actually holds them; they are never probed (no `whoami` or other
request fills a gap). The adapter's readiness is degraded until the
first sync acknowledgement; emission belongs at the true ready
transition with whatever identity is held by then, and no fact gates or
delays readiness. The second row carries the configured homeserver and
the installed transport SDK version, read from installed package
metadata at runtime and omitted when the distribution is absent.

The access token and all olm/identity key material are never offered.
No pairing, credential-bearing, or secret-named field is surfaced as a
fact or label. Device IDs are operator-visible platform facts and
render in full under core's redaction policy; redaction itself remains
core's render-boundary responsibility.

## Decoupling

The provider MUST return plain JSON-like data shaped by the contract.
Adapter source MUST NOT import `medre.core` symbols for startup info;
core validates, redacts, orders, and caps the facts at its render
boundary. An adapter instance without the callable — including the
fake adapter, which owns no Matrix identity — simply reports nothing.
