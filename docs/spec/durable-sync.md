# Durable Matrix sync

## Ownership

The Matrix provider requires Python 3.12+ and mindroom-nio[e2e] 1.1.5. The SDK
MUST own captured responses, crypto mutations, membership projections, gap
recovery and its upstream cursor. MEDRE MUST own semantic ingress admission and
its downstream event, routing and delivery records. Native readiness MUST NOT
establish message delivery success.

One SDK owner and one ordered application consumer MUST share a persistent
adapter-scoped store. The consumer identity MUST bind the adapter instance,
homeserver and account. The SDK store additionally binds the device. Reopening
MUST refuse a conflicting consumer or an active writer.

## Acceptance and replay

MEDRE MUST admit timeline records using the SDK record's event-time provenance.
It MUST acknowledge a batch only after every record and application evidence
checkpoint are accepted. Expected ingress deferrals MUST retry in order without
acknowledgement. Repeated capacity and evidence deferrals MUST use bounded
exponential backoff, reset for each record or batch. Media-fetch deferrals MUST
use an independent 15-second base, doubling to a 120-second cap, so fast failures
do not exhaust acquisition attempts in a tight loop. A deferred evidence commit
MUST NOT redispatch accepted records. Unexpected exceptions MUST retain the
committed batch and surface through supervision. Cancellation MUST retire the source and consumer
before another generation acquires the store.

Crash replay MAY repeat records admitted before acknowledgement. Core's stable
native-identity deduplication MUST prevent duplicate admission effects; this
contract does not establish exactly-once native delivery. SDK recovery loss
records MUST persist as application evidence before acknowledgement. Ephemeral
typing MUST remain outside this durable record path.

## Store adoption

Before first adoption, the provider MUST seed the ordinary SDK store's sync token
from MEDRE's classic_sync checkpoint using the SDK store API. Adoption MUST
preserve device crypto and trust state. Reopening an adopted store MUST use its
SDK position and pending batches; the legacy checkpoint MUST NOT rewind it.
Outstanding legacy recovery MUST fail adoption instead of being cleared.

The durable consumer binding includes the Matrix client-server origin, MXID and
adapter ID. URL spelling (host case, default ports, trailing slash) is normalized.
Where the active device's saved credentials match the account, device and access
token, the authenticated homeserver URL takes precedence over a separately configured
base URL (e.g. after well-known delegation). An already-owned OAuth session MAY
supply its authenticated homeserver when its account/device match, independently of rotating access tokens. Explicit tokens
MUST retain the full sidecar triple-match; an unrelated credential sidecar
MUST NOT change the binding. Already adopted stores retain a matching legacy
raw-URL consumer ID; conflicting IDs MUST fail closed without rewriting the
SDK store. This is an identity binding, not arbitrary homeserver migration.

The nio_durable application checkpoint records accepted stream and sequence
identity plus recovery-loss metadata; it MUST NOT be used as an upstream cursor.
Attachment acquisition retry checkpoints retain their independent authority.

An adopted database cannot be opened by the ordinary SDK store. Operators MUST
stop bridges and back up adapter state and core storage before upgrading. A
rollback requires a compatible durable reader or coordinated restoration of the
pre-upgrade state; deleting crypto state is not a migration strategy.

## Backend and platform

The durable API requires an encryption-enabled SQLite backend even for plaintext
rooms. This storage requirement MUST NOT authorize encrypted-room delivery or ingress
when plaintext policy is configured. Known encrypted-room and decrypted-event
observations MUST be filtered before canonical admission, while their SDK batch
continues to acknowledgement. The e2e extra remains a compatibility alias.
The released SDK's POSIX file-lease requirement prevents production durable
sessions on Windows; importable crypto alone does not qualify Windows storage.
