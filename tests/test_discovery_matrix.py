"""Discovery contract for a separately installed MEDRE transport."""
from importlib.metadata import entry_points

from medre.adapter_registry import AdapterSpec, get_adapter_spec


def test_adapter_entry_point() -> None:
    matches = [ep for ep in entry_points(group="medre.adapters") if ep.name == "matrix"]
    assert len(matches) == 1
    spec = matches[0].load()()
    assert isinstance(spec, AdapterSpec)
    assert spec.transport == "matrix"
    assert spec.adapter.module == "medre.adapters.matrix.adapter"
    assert spec.config.module == "medre.config.adapters.matrix"
    assert get_adapter_spec("matrix") == spec
