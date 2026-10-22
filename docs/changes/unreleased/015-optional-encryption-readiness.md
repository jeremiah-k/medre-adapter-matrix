# Surface optional encryption limitations at startup

Report degraded health when requested Matrix encryption is inactive, including
otherwise connected and syncing sessions. Record missing dependencies in crypto
diagnostics with the matrix[e2e] install command and explain unsuccessful crypto
initialization before delivery fails. Keep encrypted-room sends fail-closed and
preserve unencrypted-room operation in optional mode.
