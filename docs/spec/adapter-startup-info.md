# Matrix startup info

Core discovers the adapter's optional `startup_info()` callable and
renders the supplied facts using the shared startup-info contract.

The provider MUST read already-held state and MUST NOT contact the
homeserver. It binds one session and one client before reading the user
and device IDs. Either ID can be omitted when unavailable.

The configured homeserver appears when it adds endpoint information,
such as a different host, non-default port, or base path. A root HTTPS
endpoint matching the server name in the Matrix user ID is redundant.
The fact set excludes access tokens, encryption material, and
credential-bearing endpoints.
