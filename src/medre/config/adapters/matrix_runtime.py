"""Adapter-owned runtime configuration wrapper for matrix."""

from dataclasses import dataclass
from typing import Self

from medre.config.adapters.matrix import MatrixConfig
from medre.config.model import GenericAdapterRuntimeConfig


@dataclass(frozen=True)
class MatrixRuntimeConfig(GenericAdapterRuntimeConfig):
    """Validated runtime configuration for one matrix instance."""

    config: MatrixConfig | None = None

    @classmethod
    def from_dict(cls, instance_name: str, data: dict) -> Self:
        return cls.from_transport_dict(
            instance_name, data, transport="matrix", config_cls=MatrixConfig
        )
