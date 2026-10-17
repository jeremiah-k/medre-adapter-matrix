# medre-adapter-matrix

Independent Matrix adapter for MEDRE, extracted from MEDRE commit
`e59c29dd31a95860d1cdbc19e531896616627a68`.

This repository retains the original adapter source history, with original
Git author and committer identity metadata. The import path stays
`medre.adapters.matrix`; the distribution registers an entry point in
`medre.adapters`, so MEDRE can discover it without a built-in adapter list.

MEDRE's default adapter source catalog names this repository, so a persistent
uv tool installation can add it by alias:

```bash
uv tool install 'git+https://github.com/jeremiah-k/medre.git@main'
medre adapters install matrix
medre adapters
```

Installing `matrix` requests the distribution without extras; install
`'matrix[e2e]'` instead to enable Matrix encryption.

The alias resolves to this repository's main branch. To pin a reviewed commit
or switch to an index or local source, print the catalog with
`medre adapters sources`, copy it to the override file it names, and edit the
requirement; `medre adapters remove matrix` drops it; `medre update` updates
the tool within its recorded sources. Only these explicit commands run uv;
MEDRE installs nothing while loading a configuration or starting the runtime.

Deployment projects and plain pip remain supported (MEDRE and this adapter
must share one Python environment):

```bash
# Package index, if/when this distribution is published:
python -m pip install 'medre-adapter-matrix[e2e]'

# Git source; replace FULL_COMMIT_SHA with a reviewed 40-character commit ID:
python -m pip install 'medre-adapter-matrix[e2e] @ git+https://github.com/jeremiah-k/medre-adapter-matrix.git@FULL_COMMIT_SHA'

# Local development checkout:
python -m pip install -e ../medre-adapter-matrix
```

Git installation uses standard Python direct URL requirements, not a custom
MEDRE downloader. For uv, use `tool.uv.sources` with an exact Git `rev` or
local `path`; commit the resulting `uv.lock` for reproducibility. Installing
an adapter does not require publishing it on PyPI. Runtime startup never
installs or updates dependencies.

After installation/restart, use `medre adapters check --config bridge.yaml` to
verify that the transport is discoverable. See
[MEDRE adapter installation](https://github.com/jeremiah-k/medre/blob/main/docs/ops/adapter-installation.md)
for mixed PyPI/Git/local installations and community adapters, and
[package management](https://github.com/jeremiah-k/medre/blob/main/docs/ops/package-management.md)
for the source catalog, extras, and updates.

## Getting started with MEDRE

Follow the [MEDRE Getting Started guide](https://github.com/jeremiah-k/medre/blob/main/docs/ops/getting-started.md)
for a Git-based uv deployment, a local fake smoke, concepts, and a first two-way
route. The guide follows the external-adapter distribution model. Keep
core and every selected provider in one environment, either a persistent
uv tool managed with `medre adapters install` or a locked deployment project;
the provider's Git repository, import namespace, and package name are separate
parts of ordinary Python packaging.

Core owns routing, persistence, lifecycle, and delivery evidence. Each provider
owns its platform connection and native semantics. Other independently maintained
providers use the same discovery and event contracts; no core registration patch
is needed. First-party siblings:
[Matrix](https://github.com/jeremiah-k/medre-adapter-matrix),
[Meshtastic](https://github.com/jeremiah-k/medre-adapter-meshtastic),
[MeshCore](https://github.com/jeremiah-k/medre-adapter-meshcore), and
[LXMF](https://github.com/jeremiah-k/medre-adapter-lxmf).
Choose providers for the platforms in your deployment and review their own
dependency and license requirements.

## Restart and offline messages

Classic Sync resumes from MEDRE's durable checkpoint. Events received during
startup are durably admitted before they are acknowledged, including messages
sent while MEDRE was offline. Initial history remains suppressed from routing.
Callbacks arriving after shutdown defer admission rather than advancing a
checkpoint without retaining the message.

## Ownership and imports

This distribution owns Matrix's transport implementation, configuration models,
runtime wrapper, fake adapter, native addressing behavior, SDK requirements,
and protocol/SDK tests. MEDRE core owns generic events, routing, lifecycle,
storage, evidence, configuration orchestration, and installed-package discovery.

| Surface         | Adapter-owned import                                       |
| --------------- | ---------------------------------------------------------- |
| Config models   | `medre.config.adapters.matrix`                             |
| Runtime wrapper | `medre.config.adapters.matrix_runtime.MatrixRuntimeConfig` |
| SDK-free fake   | `medre.adapters.fakes.matrix.FakeMatrixAdapter`            |

Import the runtime wrapper from this distribution directly. Core has no named
wrapper compatibility bridge or transport SDK extras. The supported MEDRE
interface range and native SDK pins are declared in this project's
`pyproject.toml`; use a matching core revision during pre-release development.
Community adapter authors can use their own distribution names, import packages,
and hosting sources with the same `medre.adapters` entry-point contract.

MEDRE-authored adapter source is [MIT-licensed](LICENSE). This grant does
not relicense installed dependencies. Their license terms still apply to any
combined deployment or redistribution. See [dependency license notes](DEPENDENCY_LICENSES.md).

## Offline samples and diagnostics

This distribution contributes native fake values and an opaque conversation
address to `medre config sample`. Core includes only installed contributors;
a single contributor gets two fake instances for offline routing. Native
packet simulation methods remain adapter-specific. Run-session canonical
callback injection uses `AdapterContract.publish_inbound(event)`.

`medre adapters check --config bridge.yaml` distinguishes missing distributions
from broken/conflicting entry points. Basic CLI help and unrelated healthy
providers remain available when another provider fails discovery.

Matrix configures the core-owned `medre adapter matrix` parser. Optional CLI
registration failures are reported as `broken CLI contribution` without
disabling generic help, preflight, healthy commands, or runtime discovery.

A fresh Matrix session requests full room state alongside its saved event cursor.
This restores membership names and encryption state after a process restart;
subsequent syncs retain the incremental cursor without replaying old messages.

Matrix event `content.medre` carries versioned canonical provenance and selected
metadata. `metadata_embedding_mode` is destination-specific: `off` omits the
MEDRE namespace, `minimal` carries event/actor identity, `safe` adds native
message references and normalized metadata, and `full` also includes provider
native and custom metadata. Full mode exports those details deliberately;
configuration and SDK objects are never embedded.

Meshtastic's `mmrelay_compatibility` independently adds the legacy
`meshtastic_*` Matrix content fields. Disable that flag and select `off` for
content without relay metadata. Embedded provenance is an untrusted relay hint,
not delivery evidence or authorization to operate on another message. The wire
contract is [matrix-embedded-metadata.schema.json](docs/schemas/matrix-embedded-metadata.schema.json).

## Tests

Protocol and SDK contract tests live in this repository with adapter-owned
helpers. Cross-adapter routing and lifecycle tests remain in MEDRE. Run the
following from this repository in a fresh virtual environment, with a matching
MEDRE checkout at `../medre`.

```bash
# SDK-free fake tier: core dev supplies test tooling; skip this adapter's SDK deps.
python -m pip install -e '../medre[dev]'
python -m pip install --no-deps -e .
python -m pytest -q

# Native SDK tier: this distribution supplies its own SDK pins.
python -m pip install -e '.[test,e2e]'
python -m pytest -v -m "matrix_sdk and not local_integration and not soak and not live and not hardware and not docker"
```

The default pytest selection excludes SDK, service, hardware, local integration,
and soak tiers. Installing SDKs afterward leaves the same environment SDK-enabled;
use a fresh environment for a new SDK-free proof.

## Fake smoke sources

The fake provides a synchronous `make_event(text=..., event_kind=...)` returning
a canonical event with its own adapter instance ID. After installing all
providers referenced by a fake bridge config, exercise a source with:

```bash
medre smoke --config bridge.yaml --source-adapter ADAPTER_ID --json
```

Use the configured instance ID, which may differ from the transport name. Each
normal smoke tests one source; inspect `source_candidates` and
`coverage_scope: single_source` in its report. Eligible sources must have started
and provide the helper. A pass requires delivery for every matched configured
route target, including targets unavailable after construction or startup.
Factory errors fail the smoke and trigger runtime cleanup. The result proves
fake direct-pipeline behavior; native callbacks and recipient delivery have
separate qualification tiers.

## Coordinated CI

The repository CI installs MEDRE core and this adapter. Its fake job omits
native SDK dependencies; its SDK job installs this distribution's declared pins.
CI tests against MEDRE `main` by default. Set the repository Actions variable
`MEDRE_CORE_REF` to a branch or commit during coordinated core development;
the manual workflow input `core_ref` overrides it. Clear the variable after
the compatible core revision reaches `main`.

Core's compatibility matrix tests each selected adapter at the exact commit
recorded in its manifest, along with cross-adapter behavior. In a coordinated
checkout containing the selected sibling repos, run from MEDRE:

```bash
python scripts/ci/adapter_matrix.py --check-local
# Once all adapter changes are committed:
python scripts/ci/adapter_matrix.py --sync-local
```

The helper validates clean sibling checkouts and their entry points, and compares
Python distribution names using normalized case/separator spelling. Synchronizing
updates the manifest atomically without fetching or installing packages. Review
the manifest diff, publish adapter commits first, then publish the core pin
update. A failed write preserves the original manifest and cleans up the temporary
file. Deployment projects should separately lock their chosen complete
package graph.

Standalone CI records the exact core and adapter SHAs tested against its
configured core baseline. MEDRE core CI qualifies the pinned compatibility
pair; a standalone green result does not qualify another core revision.
SDK qualification extras come from the selected core compatibility manifest,
while SDK versions remain authoritative in this adapter's package metadata.

## Provider contract and quality

The [registration reference](https://github.com/jeremiah-k/medre/blob/main/docs/dev/adapter-registration.md)
documents every hook, its loading phase, and the minimal fake constructor/event
calls. Shared native-metadata helpers are public under `medre.adapters.metadata`.
Conformance tests use `medre.testing`; SDK pin checks read this distribution's
installed metadata while this repository chooses its SDK qualification extras.
The fake constructor and explicit-keyword event factory are checked in the
SDK-free tier. Native schemas and validated examples live in `docs/schemas` here.

This repository declares its own Ruff/Black policy. CI checks all Python source
and tests, plus commit whitespace, alongside the fake/native qualification tiers.
The native adapter package includes its own `py.typed` marker.
