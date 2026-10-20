# Matrix authentication

Fresh devices publish their keys before repairing the self-signature under a
persisted account identity. Login and runtime repair retain matching account
keys, and rejected uploads cannot trigger identity rotation or claim success.

Matrix authentication supports browser-approved OAuth device sessions, explicit
public client IDs, saved-session resumption, automatic token renewal at the SDK
HTTP attempt boundary, and passwordless revocation of saved password or OAuth sessions without deleting E2EE state. Own-device
signing can complete browser authorization while identity replacement remains
explicit. Failed signing retains credentials for retry. Cross-process renewal
ownership and atomic private credential publication protect rotating tokens
against concurrent writes and cancellation.
