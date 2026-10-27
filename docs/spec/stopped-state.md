# Stopped-state declaration

The adapter ships a provider-owned stopped-state declaration referenced from
its `AdapterSpec` through the optional `stopped_state_declaration` symbol. The
declaration MUST NOT import the transport SDK, MUST NOT open a profile, start
or stop a process, or touch the network, and MUST resolve every path from
configured values alone — the same prepared configuration the runtime derives
through the adapter's preparation hook.

The durable store database name is derived by the SDK from the configured user
ID and device ID. When that name contains characters the inventory path grammar
rejects, generic capture MUST be declared as externally managed with a
provider-export requirement rather than a guessed filename; a representable
name is declared as a required SQLite file under the configured store
directory. A configured instance without a device ID MUST report unsupported,
because the SDK names its store databases only at runtime.

The access-token sidecar under the XDG configuration home MUST be declared
export-only by this declaration. Its XDG location can be inside or outside
the selected root; the declaration does not select it automatically.

The SDK retains a store ownership lease. The maintenance declaration does not
acquire that lease, so it MUST report local exclusion as unsupported. This is
not evidence that no SDK lease exists, that a local profile is unused, or that
another host with the same account and device is inactive.

Consumers, selection enforcement, and the trust boundary live in
`medre.maintenance.stopped_declare` on the core side; planning refuses a
distribution whose declaration is missing or fails to load.
