"""Native address rules are owned and enforced by the installed adapter."""

import pytest
from medre.adapter_registry import get_destination_spec
from medre.config.errors import ConfigValidationError
from medre.config.routes import RouteDestinationConfig


def test_registered_native_destination_validates() -> None:
    assert get_destination_spec("matrix_room") is not None
    destination = RouteDestinationConfig.from_dict(
        "native",
        {"kind": "matrix_room", **{"destination_name": "!room:test"}},
        field_name="dest_destination",
        section_path="routes.native",
    )
    assert destination.kind == "matrix_room"


@pytest.mark.parametrize(
    "address", [{}, {"destination_name": "room", "destination_hash": "opaque"}]
)
def test_native_destination_rejects_invalid_address(address: dict) -> None:
    with pytest.raises(ConfigValidationError):
        RouteDestinationConfig.from_dict(
            "native",
            {"kind": "matrix_room", **address},
            field_name="dest_destination",
            section_path="routes.native",
        )
