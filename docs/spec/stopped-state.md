# Stopped-state declaration

The adapter's provider-owned declaration MUST preview configured paths without
importing the transport SDK, opening a native profile, or touching the network.
Capture MUST resolve native state only after retaining local exclusion through
snapshot publication. Core consumes this contract through
`medre.maintenance.stopped_declare`.

## Selected account state

A configured user ID and device ID MUST identify the exact pinned SDK database
`<user_id>_<device_id>.db`. Missing identities MUST be refused rather than
inferred. Account and device values MUST NOT contain path separators. Present
ordinary-store trust sidecars (`trusted_devices`, `blacklisted_devices`,
`ignored_devices`) and the MEDRE credential sidecar MUST be selected alongside
the database. Native SQLite sidecars MUST be validated, while committed WAL
content MUST be captured by SQLite backup rather than raw sidecar copying.

The common capture root MUST include every selected file, including the XDG
credential sidecar when it exists. For a store under the XDG state home and
credentials under the XDG configuration home, the common home directory can
serve as the root; selection remains explicit and MUST NOT crawl that root.
Absent credentials do not manufacture a file: credentials supplied in runtime
configuration MUST be retained separately with that configuration. The token
sidecar is sensitive and may include other account credentials.

SDK filenames containing colons require a POSIX capture and restore host.
Windows alternate-stream spellings MUST NOT be accepted. Provider state MUST
NOT be renamed merely to fit a portable inventory grammar.

## Ownership and activation

Capture MUST retain the existing SDK `<database>.ingest.lock` using the same
exclusive flock protocol as the native store. It MUST NOT create, replace,
steal or restore that lock file. A live ordinary or durable SDK owner MUST block
capture. Selected identity, credential, trust and SQLite sidecar files and the
lease file MUST reject symlink and hard-link aliases.

A restored SDK owner creates its own lock at the restored location. Before
activation, the operator MUST stop the original native owner and configure the
restored store and XDG locations. Local exclusion MUST NOT be described as
excluding another host with the same Matrix account and device. Capture and
restore do not log in, rotate keys, reset native cursors, or authorize retries.
