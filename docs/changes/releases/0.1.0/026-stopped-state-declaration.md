# Declare Matrix stopped state for maintenance capture

Ship a provider-owned stopped-state declaration module and reference it from
the adapter spec through the optional `stopped_state_declaration` symbol. The
declaration resolves the SDK-derived durable store database and the
access-token sidecar from configured values without importing the transport
SDK. Standard account IDs produce store database names the inventory path
grammar cannot express, so generic capture of the durable store is declared
as requiring a provider export rather than guessed; a store name without
those characters is selected as a required file. The token sidecar under the
XDG configuration home is reported export-only rather than selected
automatically. Maintenance does not acquire the SDK store ownership lease;
local exclusion is reported unsupported instead of claiming a retained lock. Consumers and the
trust-boundary contract live in `medre.maintenance.stopped_declare` on the
core side; a distribution without a declaration is reported as unsupported
there rather than guessed.
