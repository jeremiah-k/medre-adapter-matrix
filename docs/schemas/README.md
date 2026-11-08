# Matrix adapter schema contracts

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
