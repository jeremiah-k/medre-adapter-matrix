"""Safe cross-platform context survives Matrix without becoming local authority."""

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import msgspec
from jsonschema import Draft202012Validator
from medre.adapters.matrix.bridge_context import normalize_bridge_context
from medre.adapters.matrix.codec import MatrixCodec
from medre.adapters.matrix.metadata import build_matrix_metadata_content
from medre.config.adapters.matrix import MatrixConfig
from medre.core.events import (
    CanonicalEvent,
    EventMetadata,
    EventRelation,
    NativeMetadata,
    NativeRef,
)


def event(platform):
    """Every provider supplies only canonical fields and an opaque native reference."""
    return CanonicalEvent(
        event_id="local-original",
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter=f"source-{platform}",
        source_transport_id="native-sender",
        source_channel_id="3",
        parent_event_id=None,
        lineage=(),
        relations=(
            EventRelation(
                relation_type="reply",
                key=None,
                fallback_text=None,
                target_event_id="local-target",
                target_native_ref=NativeRef(
                    adapter=f"source-{platform}",
                    native_channel_id="3",
                    native_message_id="native-target",
                ),
            ),
        ),
        payload={"body": "hello"},
        metadata=EventMetadata(
            native=NativeMetadata(data={"SECRET_SDK_BLOB": "PRIVATE_SENTINEL"})
        ),
        source_native_ref=NativeRef(
            adapter=f"source-{platform}",
            native_channel_id="3",
            native_message_id="native-original",
        ),
    )


@pytest.mark.parametrize(
    "platform",
    ["matrix", "meshtastic", "meshcore", "xmpp", "lxmf", "chatmail", "simplexchat"],
)
def test_safe_context_preserves_source_and_exact_relation_hints(platform):
    """All platforms use the same bounded context without copying SDK/private metadata."""
    attribution = SimpleNamespace(
        source_platform=platform,
        source_origin_label="Forx",
        source_sender_label="Peer (unverified)",
        source_sender_label_raw="Peer",
        source_sender_short_label="P (unverified)",
        source_sender_short_label_raw="P",
        source_sender_verification="unverified",
    )
    wire = build_matrix_metadata_content(
        event(platform), "safe", source_attribution=attribution
    )
    context = wire["medre"]["metadata"]["bridge_context"]
    assert context["source"]["platform"] == platform
    assert context["source"]["sender_name"] == "Peer"
    assert context["source"]["sender_verification"] == "unverified"
    assert context["source"]["native_ref"]["native_message_id"] == "native-original"
    assert (
        context["relations"][0]["target_native_ref"]["native_message_id"]
        == "native-target"
    )
    assert "PRIVATE_SENTINEL" not in json.dumps(wire)
    schema = json.loads(
        (
            Path(__file__).parents[1]
            / "docs/schemas/matrix-embedded-metadata.schema.json"
        ).read_text()
    )
    Draft202012Validator(schema).validate(wire["medre"])
    incoming = SimpleNamespace(
        body="hello",
        sender="@peer:example.com",
        event_id="$matrix-peer",
        source={
            "type": "m.room.message",
            "content": {"msgtype": "m.text", "body": "hello", **wire},
        },
    )
    decoded = MatrixCodec(
        "matrix",
        MatrixConfig(
            adapter_id="matrix",
            homeserver="https://example.com",
            user_id="@bot:example.com",
            access_token="test-token",
        ),
    ).decode(incoming, room_id="!room:example.com")
    assert decoded.source_adapter == "matrix"
    assert decoded.source_native_ref.native_message_id == "$matrix-peer"
    assert not decoded.relations
    assert (
        decoded.metadata.native.data["matrix"]["relay"]["bridge_context"]["source"][
            "adapter"
        ]
        == f"source-{platform}"
    )
    native_schema = json.loads(
        (
            Path(__file__).parents[1]
            / "docs/schemas/matrix-native-metadata.schema.json"
        ).read_text()
    )
    Draft202012Validator(native_schema).validate(
        json.loads(msgspec.json.encode(decoded.metadata.native.data))
    )


@pytest.mark.parametrize("mode", ["off", "minimal"])
def test_context_respects_explicit_metadata_mode(mode):
    """Operators can suppress cross-platform context with existing mode controls."""
    wire = build_matrix_metadata_content(event("meshcore"), mode)
    assert "bridge_context" not in json.dumps(wire)


def test_malformed_and_oversized_peer_context_is_ignored():
    """Unknown schema, invalid types and oversized arrays cannot become local context."""
    context = build_matrix_metadata_content(event("meshcore"))["medre"]["metadata"][
        "bridge_context"
    ]
    for field, value in [
        ("schema_version", True),
        ("schema_version", 2),
        ("relations", [{}] * 33),
        ("source", {"adapter": {}, "canonical_event_id": "id"}),
    ]:
        invalid = copy.deepcopy(context)
        invalid[field] = value
        assert normalize_bridge_context(invalid) is None
    invalid = copy.deepcopy(context)
    invalid["source"]["sender_name"] = "x" * 1025
    assert normalize_bridge_context(invalid) is None
    context["source"]["password"] = "PRIVATE_SENTINEL"
    context["relations"][0]["target_fact"] = {"authorization": "owned"}
    normalized = normalize_bridge_context(context)
    assert normalized is not None
    assert "PRIVATE_SENTINEL" not in json.dumps(normalized)
    assert "target_fact" not in json.dumps(normalized)
