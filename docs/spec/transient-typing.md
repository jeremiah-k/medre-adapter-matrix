# Matrix transient typing

The adapter MUST declare native typing support separately from presence and
message capabilities. Only an explicitly supplied activity sink enables native
ingress. Configured, live room membership MUST bound forwarding; relay-user
entries, startup state and malformed membership lists MUST NOT create leases.

Each `m.typing` user list MUST replace the source conversation's full actor set,
including empty lists. The adapter MUST use the core snapshot contract rather
than maintain a parallel actor cache. Actor claims MUST remain unverified native
identifiers; outgoing activity MUST use the configured relay account.

SDK ephemeral callbacks MUST carry only the live response's transient sections.
The durable SDK owner MUST NOT replay typing from committed timeline batches. Recovered timeline
content MUST NOT be reinterpreted as fresh composing state. Typing MUST NOT enter
canonical admission, checkpoint state, durable outbox work or receipts.

Outbound typing MUST validate this adapter identity, configured room scope,
joined membership and encryption safety before calling the owned SDK client.
Positive timeouts MUST remain finite and bounded by the host freshness lease;
zero activity requests a native clear. Native failures MUST NOT claim successful
message handoff. Shutdown clearing remains best effort.

Activity is unencrypted metadata even in rooms with encrypted content. Route
opt-in MUST be explicit and independent of permission to relay messages. A
30-second freshness limit without another live observation can end the projected
indicator during a long composition. This does not establish full native typing
fidelity, source impersonation or recipient delivery confirmation.
