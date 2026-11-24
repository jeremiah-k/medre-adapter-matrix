# medre-adapter-matrix

The Matrix adapter for MEDRE.

## Install

Use [uv](https://docs.astral.sh/uv/getting-started/installation/) to install
MEDRE and this adapter in the same persistent tool environment:

```bash
uv tool install --python 3.12 medre
medre adapters install matrix
```

Matrix requires Python 3.12+ and includes its durable SQLite/crypto backend.
The `matrix[e2e]` spelling remains a compatible installation alias. The
released mindroom-nio store requires POSIX file leases; Windows production
sessions are therefore not supported today.

On Windows, the package-changing command prints a uv command to run **after**
`medre.exe` exits, rather than replacing an executable still in use.
Adapter installation defaults to package-index requirements; existing saved
Git requests and catalogs are preserved. For fake smoke, guided route
creation, locked uv deployment projects, private Git sources, local checkouts,
and the same-environment requirement, see the
[MEDRE Getting Started guide](https://github.com/jeremiah-k/medre/blob/main/docs/ops/getting-started.md)
and the
[adapter-source guide](https://github.com/jeremiah-k/medre/blob/main/docs/ops/adapter-installation.md).
Neither MEDRE nor the adapter installs packages during runtime startup.

## Configure

Save a private `connections.yaml` with an explicit instance and room
allowlist:

```yaml
adapters:
  matrix:
    bridge:
      adapter_kind: real
      encryption_mode: e2ee_required
      room_allowlist: ["!abc123:localhost"]
routes: {}
```

New private rooms and direct messages in Element are encrypted by default, so
the example uses `e2ee_required`; `plaintext` is the option for an unencrypted
room, and `e2ee_optional` falls back to plaintext when the crypto library does
not load. In an encrypted room, `plaintext` cannot decrypt any message.

Supply account settings through MEDRE's environment overrides or the
credential sidecar; the instance token matches `adapters.matrix.bridge`. Set
the allowlist to exactly the room(s) you intend to monitor: without it, the
adapter processes every message from every room the bot has joined. For
login and E2EE identity setup, room provisioning, all options, and
troubleshooting, see the
[Matrix setup guide](https://github.com/jeremiah-k/medre/blob/main/docs/ops/transport-setup/matrix.md).

## Verify

Confirm the installed adapter and the saved configuration resolve:

```bash
medre adapters check --config connections.yaml
```

The setup guide's
[Verify](https://github.com/jeremiah-k/medre/blob/main/docs/ops/transport-setup/matrix.md#verify)
section shows expected startup output, shutdown output, and health states.

## Startup readiness

After authentication and configured room joins, startup waits up to
`initial_sync_timeout_seconds` (45 seconds by default) for the first acknowledged
sync. A pending long poll is logged as startup progress. Login alone never
establishes healthy readiness. On expiry, startup returns with degraded health
and the existing sync owner keeps recovering; a later acknowledgement publishes
healthy current state automatically. Native sync failure or reconnect exits
the wait immediately with its actual health.

Set the wait to `0` to report pending health immediately. The runtime's
`adapter_start_timeout_seconds` bounds the complete adapter start, including
login, joins, and this wait. Allow additional runtime budget for slow services;
its cancellation still closes the partially started session. See
[durable sync](docs/spec/durable-sync.md#startup-readiness) for the contract.

Durable sync restores the SDK's persisted membership and encryption projection.
A fresh store obtains its initial state from the homeserver; existing stores keep
their device identity and pending batches. See the
[durable sync contract](docs/spec/durable-sync.md) for adoption and rollback limits.

## Message presentation

Supported Matrix HTML maps to bounded canonical text spans when its visible text
matches the plain body. Outbound content escapes text and links and keeps relay
attribution outside source styles. Unsupported or inconsistent markup retains
plain presentation. See the [presentation contract](docs/spec/relay-attribution.md#text-presentation)
for reply, edit, truncation, and block-boundary behavior.

## Embedded metadata

Matrix event `content.medre` carries versioned canonical provenance and selected
metadata. `metadata_embedding_mode` is destination-specific: `off` omits the
MEDRE namespace, `minimal` carries event/actor identity, `safe` adds native
message references and normalized metadata, and `full` also includes
transport-native and custom metadata. Full mode exports those details
deliberately; configuration and SDK objects are never embedded.

Outbound Matrix content carries no legacy `meshtastic_*` fields; the embedded
MEDRE namespace is the only relay metadata surface, and selecting
`metadata_embedding_mode: off` yields content without it. Embedded provenance
is an untrusted relay hint, not delivery evidence or authorization to operate
on another message. The wire contract is
[matrix-embedded-metadata.schema.json](docs/schemas/matrix-embedded-metadata.schema.json).

## Browser-approved Matrix login

For an OAuth-enabled homeserver, authorize MEDRE from your browser instead of
providing its account password. The adapter includes the crypto backend;
select the adapter whose persistent device store should be prepared:

```bash
medre adapter matrix auth login --oauth \
  --user @relay:example.org --adapter-id matrix
medre adapter matrix auth status
```

Use the configured adapter key for `--adapter-id`. The command displays the
verification address and a short code. Approve only the login you initiated.
Homeserver discovery follows Matrix delegation; `--homeserver` can select an
explicit server. `--client-id` supports an existing public client registration
when dynamic registration is unavailable. Without `--adapter-id`, the command
only establishes credentials and does not load the E2EE SDK.

Keep credentials out of the runtime YAML so its missing authentication fields
resolve from MEDRE's credential sidecar. Runtime requests automatically renew
OAuth tokens, including SDK retry attempts. Repeating the login command resumes
the saved device session and retries signing instead of allocating another
device. Password options cannot be combined with `--oauth`.

The browser may request a separate signing-key approval. A matching account
identity is reused across devices; a missing local identity for an account that
already has signing keys requires restoring the original store. Deliberately
replacing lost or conflicting account keys requires `--reset-cross-signing`:

```bash
medre adapter matrix auth login --oauth --adapter-id matrix --reset-cross-signing
```

A reset can require other clients to verify the account identity again. It is
not part of routine login. Signing failures retain OAuth credentials and E2EE
state so setup can be retried. Own-device signing does not verify other users
or restore historical encrypted room keys.

Stop bridges using this account before changing sessions. To revoke a saved
password or OAuth session and then authenticate again, use:

```bash
medre adapter matrix auth logout
```

Logout preserves the crypto store and account-signing sidecar. Back them up
securely together. Password login remains available on supporting homeservers
through the existing interactive or `--password-stdin` commands.

## Restart and offline messages

The SDK persists captured responses, crypto state, recovery and its upstream
cursor. MEDRE admits committed records before acknowledging their SDK batch.
Restart replays unacknowledged records through native-ID deduplication. History
provenance and recovery losses remain explicit; neither a saved cursor nor a
connected client establishes successful delivery.

Inbound events whose origin server timestamp predates the adapter's start by
more than `startup_backlog_suppress_seconds` (default 60 seconds, `0` disables)
are startup history and are suppressed on every delivery path — live sync,
durable catch-up, or gap recovery — using the same transport-neutral
`should_suppress_startup_backlog` policy the Meshtastic adapter applies. The
check precedes decode, so messages, reactions, and edits are equally covered,
and suppressed events count under `inbound_suppressed_stale` in diagnostics.
The window covers homeserver federation lag and minor clock drift so events
sent moments before startup still flow.

## Default relay attribution

Matrix output defaults to `[sender/origin]: message`, preserving the full sender
name. An absent origin renders `[sender]: message`. Set `relay_prefix: ""` to
disable attribution or supply a custom generic template. Omitted/null selects
the automatic policy; MMRelay wire metadata keeps the original unprefixed text.

## Device verification

E2EE setup uses the adapter's persistent store and checks its own device's
server-visible signing chain. A fresh device uploads its keys before reusing
the account signing identity; ordinary login does not require rotating that
identity. Keep the E2EE store when renewing credentials. A cross-signing reset
replaces account identity material and can require verification in other clients.

## Peer-device verification policy

`verification_policy` is an optional peer-device **send authorization** gate.
The `permissive` default sends encrypted messages without scanning the local
peer-device trust store or emitting trust-gap warnings. This is normal bridge
operation for trusted rooms, not a trust verdict or a delivery receipt.
Own-device cross-signing is handled independently during identity setup and
runtime verification and does not establish trust in other users' devices.

Operators may explicitly select `strict`: then encrypted sends require an
affirmative local nio trust view for every recipient device, complete room
membership, and SDK enforcement with `ignore_unverified_devices=False`.
Unknown/unverified peers cause a retryable, pre-handoff refusal. MEDRE does
not currently offer an operator peer-verification workflow, and verification
performed in a different Matrix client does not automatically populate
MEDRE's local device store. Do not enable `strict` unless local peer trust
can be established independently. No automatic verification or plaintext
downgrade occurs.

The policy applies only to encrypted sends — crypto active and the room
affirmatively encrypted. Plaintext delivery, ephemeral typing, and redactions
are outside its scope. For the opt-in gate, a recipient missing from the
local device store counts as a trust gap. The default does not inspect that
store. The adapter's own current device is handled by cross-signing, not
peer-device authorization.

Default policy note: `permissive` is the default for compatibility. Flipping
that default is a deliberate, separately reviewed change; do not rely on the
current default for assurance.

Neither mode proves delivery. A refused send is never delivered, but an
encrypted send that passes the policy — even to fully verified devices — is
only homeserver acceptance of the Megolm ciphertext. It is not a delivery
receipt: nobody can observe from the send whether a peer device received,
decrypted, or read the message.

## Transient typing

Routes can explicitly opt in with `forward_typing: true` when both endpoints
support typing and map concrete conversations. The adapter projects Matrix's
complete ephemeral user lists into core actor leases, excludes its own relay
user, and sends the aggregate indicator as the configured relay account.

Typing is separate from message encryption and discloses activity even in
encrypted rooms. The same configured-room, joined-room and encryption-safety
guards apply to outbound requests. Indicators use finite native timeouts and
best-effort clears; they create no canonical event, outbox row or receipt.
Unchanged composition can expire after core's 30-second freshness bound unless
another native observation arrives. Message forwarding alone does not opt in. The [typing contract](docs/spec/transient-typing.md)
defines native scope and live-sync behavior.

## Encryption readiness

Install the Matrix adapter with its declared dependencies. The durable SDK
requires its SQLite crypto backend even with encryption_mode: plaintext;
plaintext policy continues refusing encrypted-room delivery. Missing backend
dependencies fail production startup instead of silently selecting ordinary sync.

Encryption readiness and own-device cross-signing are separate diagnostics.
Installing dependencies or authenticating does not recover lost account signing
keys. Runtime never resets that identity. To inspect the actual selected store:

```bash
medre adapter matrix auth status --adapter-id matrix --config ~/medre/bridge.yaml
```

If a signing file exists but cannot be read, check ownership/read permissions
first. Preserve a private backup before manually moving a confirmed unusable
file aside; import refuses replacement. Rerun status after the destination is
absent. SDK/path inspection failures require diagnosis before assuming keys are
missing.

If the signing file is missing, stop the bridge and locate an existing SDK
account-signing JSON for the same Matrix account (for example in an MMRelay
store or a backup). Do not copy tokens or device databases between applications.
Validate and import that backup using the existing MEDRE credentials:

```bash
medre adapter matrix auth restore-signing --adapter-id matrix \
  --config ~/medre/bridge.yaml \
  --from ~/.mmrelay/matrix/store/@bot:example.com_cross_signing.json
```

The command verifies the token's account/device and matches the backup's keys
against the existing server identity before writing anything. Publication
requires an absent destination and filesystem hard-link support; an existing
file is never replaced. The source backup remains unchanged. A post-publication
filesystem error can leave the complete file present: inspect status before
retrying. Restart the bridge to sign its own device using the restored keys.
A readable local file alone does not prove server/device verification. Import
restores account-signing keys, not historical encrypted-room message keys.

Ordinary login preserves account signing keys and cannot reconstruct missing
private material. Only when no matching backup exists should an operator
consider authenticated login with --reset-cross-signing; it replaces the account
identity and requires verification again in other clients. Stop the running
bridge before using `medre diagnostics --refresh-health`, which opens adapters.

The adapter connects to your existing homeserver account and never operates
one. The opt-in durable-runtime live proof, including its local homeserver
account setup, is developer testing — see
[local service testing](docs/dev/local-service-testing.md).

## Development

See [contributing](CONTRIBUTING.md) for the shared Trunk quality gate and
uv-based editable development workflow. Maintainer material — packaging and
provenance, ownership and imports, offline samples and diagnostics, tests,
fake smoke sources, coordinated CI, and the adapter contract — lives in
[adapter maintenance](docs/dev/adapter-maintenance.md).
