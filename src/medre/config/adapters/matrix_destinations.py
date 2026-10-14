"""Pure adapter-owned destination-address validation."""

from medre.config.routes import RouteDestinationConfig


def validate_destination(destination: RouteDestinationConfig) -> None:
    """Reject addresses outside this transport's addressing contract."""
    if destination.destination_name is None:
        raise ValueError("destination_name is required for kind 'matrix_room'")
    if destination.destination_hash is not None:
        raise ValueError(
            "destination_hash must not be set for kind 'matrix_room'; name-based addressing resolves the native address via the adapter"
        )
