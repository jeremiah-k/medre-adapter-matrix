# Capture Matrix account state under its native lease

Resolve the configured Matrix database, present trust sidecars and credential
sidecar while retaining the pinned SDK's existing store lifetime lock. Refuse
live owners and filesystem aliases, preserve committed SQLite WAL, and retain
native filenames rather than renaming stores. Configuration and separately
supplied secrets remain explicit installation dependencies.

Real SDK checks capture and restore ordinary and durable stores with the same
crypto identity; durable stream identity survives restoration. Colon-bearing
SDK names require the core native-path contract and a POSIX restore host.
