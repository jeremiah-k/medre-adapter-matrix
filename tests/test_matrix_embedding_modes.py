"""Embedding policy applies to wire content independently of mmrelay opt-in."""

import json
from pathlib import Path
from datetime import datetime, timezone
from jsonschema import Draft202012Validator
from types import SimpleNamespace

import pytest
from medre.adapters.matrix.renderer import MatrixRenderer
from medre.core.events import (
    CanonicalEvent,
    EventMetadata,
    NativeMetadata,
    NativeRef,
    TransportMetadata,
)
from medre.core.rendering.renderer import RenderingContext
from medre_matrix_tests.helpers.matrix import matrix_payload_content


def _event() -> CanonicalEvent:
    return CanonicalEvent(
        event_id="embedding-event",
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter="independent-source",
        source_transport_id="native-actor",
        source_channel_id="native-channel",
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"body": "Hello"},
        source_native_ref=NativeRef(
            "independent-source", "native-channel", "native-message"
        ),
        metadata=EventMetadata(
            transport=TransportMetadata(protocol="private-chat"),
            native=NativeMetadata(data={"provider_private": "native-detail"}),
            custom={"operator_note": "custom-detail"},
        ),
    )


@pytest.mark.parametrize("mode", ["off", "minimal", "safe", "full"])
@pytest.mark.parametrize("compatibility", [False, True])
@pytest.mark.parametrize("strategy", ["direct", "fallback_text"])
async def test_metadata_mode_and_mmrelay_switch_are_independent(
    mode, compatibility, strategy
):
    renderer = MatrixRenderer(
        configs={
            "matrix": SimpleNamespace(
                metadata_embedding_mode=mode, access_token="CONFIG_TOKEN_NEVER_EMBED"
            )
        },
        source_configs={
            "independent-source": SimpleNamespace(mmrelay_compatibility=compatibility)
        },
    )
    ctx = RenderingContext(
        target_adapter="matrix", target_platform="matrix", delivery_strategy=strategy
    )
    result = await renderer.render(_event(), ctx)
    content = matrix_payload_content(result)
    assert ("meshtastic_id" in content) is compatibility
    assert "CONFIG_TOKEN_NEVER_EMBED" not in str(content)
    if mode == "off":
        assert "medre" not in content
        return
    medre = content["medre"]
    envelope = medre["envelope"]
    schema = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "docs/schemas/matrix-embedded-metadata.schema.json"
        ).read_text()
    )
    Draft202012Validator(schema).validate(medre)
    assert envelope["metadata_mode"] == mode
    assert envelope["canonical_event_id"] == "embedding-event"
    assert envelope["source_transport_id"] == "native-actor"
    if mode == "minimal":
        assert set(envelope) == {
            "schema_version",
            "canonical_event_id",
            "source_transport_id",
            "metadata_mode",
        }
        assert "metadata" not in medre
    else:
        assert envelope["event_kind"] == "message.created"
        assert envelope["source_native_ref"]["native_message_id"] == "native-message"
        assert medre["metadata"]["transport"]["protocol"] == "private-chat"
        assert ("native" in medre["metadata"]) is (mode == "full")
        assert ("custom" in medre["metadata"]) is (mode == "full")
        assert ("native-detail" in str(medre)) is (mode == "full")
        assert ("custom-detail" in str(medre)) is (mode == "full")


@pytest.mark.parametrize("relation_type", ["reply", "reaction"])
@pytest.mark.parametrize("compatibility", [False, True])
async def test_legacy_relation_fields_require_mmrelay_opt_in(
    relation_type, compatibility
):
    from msgspec.structs import replace
    from medre.core.events import EventRelation

    event = replace(
        _event(),
        relations=(
            EventRelation(
                relation_type=relation_type,
                target_event_id=None,
                target_native_ref=(
                    NativeRef("matrix", "room", "$target")
                    if relation_type == "reply"
                    else None
                ),
                key="👍" if relation_type == "reaction" else None,
                fallback_text="Original text",
                metadata={"meshtastic_reply_id": "77"},
            ),
        ),
    )
    renderer = MatrixRenderer(
        source_configs={
            event.source_adapter: SimpleNamespace(mmrelay_compatibility=compatibility)
        },
    )
    result = await renderer.render(
        event,
        RenderingContext(
            target_adapter="matrix",
            target_platform="matrix",
            delivery_strategy="direct",
        ),
    )
    content = matrix_payload_content(result)
    legacy_keys = {key for key in content if key.startswith("meshtastic_")}
    assert bool(legacy_keys) is compatibility
    if compatibility:
        assert content["meshtastic_replyId"] == "77"
    elif relation_type == "reply":
        assert content["m.relates_to"]["m.in_reply_to"]["event_id"] == "$target"
