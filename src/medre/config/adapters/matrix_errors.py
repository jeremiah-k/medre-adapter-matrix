"""Configuration validation error for the matrix adapter."""

from medre.config.adapters.errors import AdapterConfigError


class MatrixConfigError(AdapterConfigError):
    """Invalid matrix adapter configuration."""
