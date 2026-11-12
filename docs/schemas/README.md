# Matrix adapter schema contracts

Git attributes disable line-ending translation for this schema tree so frozen SHA-256
digests and installed wheel resources remain identical on Windows and Unix.

Source distributions include this directory. Wheels include the same schema
bundle, freeze manifest, and examples under `medre/adapters/matrix/_schemas`.
Installed consumers can read it with
`importlib.resources.files("medre.adapters.matrix").joinpath("_schemas")`.

Declared `$id` values use this repository's `raw.githubusercontent.com` schema
namespace, matching core's identifier ownership policy. The earlier `medre.dev`
identifiers are replaced before the first tag; editor mappings should use the
included schemas' `$id` values. Identifiers do not require network retrieval.

## 0.1.0 release schema freeze

`v0.1.0.freeze.json` records the SHA-256 of every exported top-level JSON,
YAML and SQL schema artifact in this distribution at the first-release
baseline. `tests/test_schema_freeze_010.py` rejects additions, deletions and
content changes without a deliberate update. This is a release-review gate,
**not** a schema-version negotiation mechanism and not a promise to migrate
unpublished prerelease databases. The freeze covers normative schemas, not
examples or runtime data. After the first tag, changes must follow the
versioning policy (including independent adapter-native and SQLite versions),
with contract tests and a documented compatibility decision; never silently
replace a released freeze manifest.

## Frozen relay provenance projection

The Matrix wire `medre.envelope` is an **untrusted routing hint**, not proof
of authorship, delivery, or authorization to mutate another message. The
versioned `matrix.relay.medre_envelope` projection preserves validated v1
identity, event kind, optional native source reference, and trace/conversation
pointers when present. In `minimal` mode it keeps only fields actually sent;
missing provenance is never filled in with invented empty strings. Unknown
fields are omitted, and malformed known fields cause the relay hint to be
discarded while the Matrix message is still decoded.

## No relay-specific native namespace in 0.1.0

The normative `matrix-native-metadata.schema.json` freezes only `native.matrix`.
A producer's source identity, native message/channel references, and reply or
reaction relationship belong to MEDRE's generic `CanonicalEvent`, `NativeRef`,
`EventRelation` and bounded `bridge_context`, not any relay-specific sidecar
namespace. Matrix-specific native metadata continues to describe the Matrix
event that was observed. Flat `meshtastic_*` content keys are not part of this
frozen contract, do not replace the authored Matrix body, and are not projected
into canonical native metadata. An untrusted wire claim, even a structurally
valid MEDRE `bridge_context`, confers no authorship, reaction-target, or
mutation authority.

The Matrix renderer emits canonical metadata only; the retired opt-in
wire-compatibility surface has been removed and must not be reintroduced.

## Git checkout line endings

A main-to-candidate Git checkout may retain unchanged CRLF files. Packaging
MUST emit the exact frozen bytes. LF reconstruction is permitted only when
its digest matches the freeze manifest; every other byte change MUST fail
the build. Fresh checkouts retain the explicit `-text` attribute policy.
