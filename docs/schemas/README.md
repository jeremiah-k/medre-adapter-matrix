# Matrix adapter schema contracts

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
