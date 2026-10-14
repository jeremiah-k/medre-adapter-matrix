"""Declarative registration for the separately distributed adapter."""

from medre.adapter_registry import AdapterSpec, DestinationSpec, SymbolRef


def adapter_spec() -> AdapterSpec:
    return AdapterSpec(
        sample_config=SymbolRef("medre.adapters.matrix.sample", "sample_config"),
        destinations=(
            DestinationSpec(
                "matrix_room",
                SymbolRef(
                    "medre.config.adapters.matrix_destinations", "validate_destination"
                ),
            ),
        ),
        logger_levels=(("nio.crypto.log", "ERROR"), ("peewee", "WARNING")),
        transport="matrix",
        config=SymbolRef("medre.config.adapters.matrix", "MatrixConfig"),
        runtime_config=SymbolRef(
            "medre.config.adapters.matrix_runtime", "MatrixRuntimeConfig"
        ),
        adapter=SymbolRef("medre.adapters.matrix.adapter", "MatrixAdapter"),
        fake_adapter=SymbolRef("medre.adapters.fakes.matrix", "FakeMatrixAdapter"),
        renderer_factory=SymbolRef(
            "medre.adapters.matrix.renderer", "build_matrix_renderer"
        ),
        dependency_probe=SymbolRef("medre.adapters.matrix.compat", "HAS_NIO"),
        distribution="mindroom-nio",
        import_names=("mindroom_nio", "nio"),
        sdk_import_roots=("nio", "aiohttp"),
        native_namespace_reader=SymbolRef(
            "medre.adapters.matrix.event_shape", "matrix_namespace"
        ),
        versioned_namespace_reader=SymbolRef(
            "medre.adapters.matrix.event_shape", "matrix_versioned_namespace"
        ),
        attribution_projector=SymbolRef(
            "medre.adapters.matrix.attribution", "project_matrix_attribution"
        ),
        runtime_config_preparer=SymbolRef(
            "medre.adapters.matrix.runtime", "prepare_matrix_runtime_config"
        ),
        runtime_directories=SymbolRef(
            "medre.adapters.matrix.runtime", "matrix_runtime_directories"
        ),
        cli_register=SymbolRef(
            "medre.adapters.matrix.cli_contrib", "register_matrix_cli"
        ),
        cli_dispatch=SymbolRef(
            "medre.adapters.matrix.cli_contrib", "dispatch_matrix_cli"
        ),
        support_endpoint_fields=("homeserver", "user_id", "room_allowlist"),
        support_secret_fields=("access_token",),
        native_detection_priority=20,
    )
