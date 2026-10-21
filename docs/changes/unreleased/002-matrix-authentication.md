# Matrix authentication

Fresh devices publish their keys before repairing the self-signature under a
persisted account identity. Login and runtime repair retain matching account
keys, and rejected uploads cannot trigger identity rotation or claim success.

Matrix authentication supports browser-approved OAuth device sessions, explicit
public client IDs, saved-session resumption, automatic token renewal at the SDK
HTTP attempt boundary, and passwordless revocation of saved password or OAuth
sessions without deleting E2EE state. Own-device signing can complete browser authorization while identity replacement remains
explicit. Failed signing retains credentials for retry. Cross-process renewal
ownership and atomic private credential publication protect rotating tokens
against concurrent writes and cancellation.

Session renewal now tolerates bounded cross-process lock contention, detects
credential replacement, and preserves an explicit sidecar-authentication source
across restarts. Browser approval has a full timeout budget, already-revoked
password sessions can be logged out, and post-publication directory-sync
failures cannot trigger revocation of published OAuth tokens.
Explicit token overrides discard sidecar renewal authority, and the initial
cache read observes token rotations completed before manager startup.

Credential-free discovery follows bounded HTTPS redirects while authenticated
endpoints refuse redirects. Windows ownership releases its byte-range lock on
exit, approval deadlines cover response-body reads, and failed renewal saves
exclude persisted token values from explicit revocation. Credential directory
sync failures emit a secret-free debug diagnostic after successful publication.
