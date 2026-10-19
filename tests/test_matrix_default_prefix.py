"""Default visible attribution follows the MMRelay presentation contract."""

from datetime import datetime, timezone
from dataclasses import replace
import pytest
from medre.core.events import CanonicalEvent, EventMetadata, NativeMetadata
from medre.core.rendering.renderer import RenderingContext
from types import SimpleNamespace
from medre.adapters.matrix.renderer import MatrixRenderer
from medre.config.adapters.matrix import MatrixConfig


def message(native, sender="!9f098838", text="Testing from medre"):
    return CanonicalEvent(
        event_id="prefix-proof",
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter="source",
        source_transport_id=sender,
        source_channel_id="3",
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"body": text},
        metadata=EventMetadata(native=NativeMetadata(data=native)),
    )


@pytest.fixture
def renderer(monkeypatch):
    # Source projection belongs to a separately installed provider. Model its
    # generic boundary here; the cross-adapter campaign exercises both codecs.
    monkeypatch.setattr(
        "medre.adapters.matrix.renderer.project_source_fields",
        lambda *a, **kw: {
            "source_sender_label": "❄️TadChilly🏂",
            "source_sender_short_label": "🥶",
            "source_sender_id": "!9f098838",
            "source_platform": "meshtastic",
        },
    )
    # Native source readers are provider-owned too. The isolated Matrix CI
    # environment deliberately has no Meshtastic provider installed.
    monkeypatch.setattr(
        "medre.adapters.matrix.renderer.current_native_namespace",
        lambda data, transport: data.get(transport, {}),
    )
    return MatrixRenderer(
        configs={
            "target": MatrixConfig(
                "target", "https://example.org", "@relay:example.org"
            )
        },
        source_configs={"source": SimpleNamespace(mmrelay_compatibility=True)},
    )


@pytest.mark.parametrize(
    "origin,prefix", [("Forx", "[❄️TadChilly🏂/Forx]: "), ("", "[❄️TadChilly🏂]: ")]
)
async def test_default_preserves_full_sender_origin_and_html(renderer, origin, prefix):
    event = message(
        {
            "meshtastic": {
                "schema_version": 1,
                "longname": "❄️TadChilly🏂",
                "shortname": "🥶",
                "packet_id": "496196763",
            }
        }
    )
    result = await renderer.render(
        event,
        RenderingContext(
            target_adapter="target",
            target_platform="matrix",
            delivery_strategy="direct",
            source_origin_label=origin,
        ),
    )
    content = result.payload["_matrix_operation"]["content"]
    assert content["body"] == prefix + "Testing from medre"
    assert prefix in content["formatted_body"]
    assert content["meshtastic_text"] == "Testing from medre"
    assert content["meshtastic_longname"] == "❄️TadChilly🏂"


@pytest.mark.parametrize(
    "template,expected",
    [("", "Testing from medre"), ("[{sender_short}]: ", "[🥶]: Testing from medre")],
)
async def test_explicit_prefix_and_disable_override_automatic(
    renderer, template, expected
):
    renderer._configs["target"] = replace(
        renderer._configs["target"], relay_prefix=template
    )
    result = await renderer.render(
        message({}),
        RenderingContext(
            target_adapter="target",
            target_platform="matrix",
            delivery_strategy="direct",
            source_origin_label="Forx",
        ),
    )
    assert result.payload["_matrix_operation"]["content"]["body"] == expected
