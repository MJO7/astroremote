import pytest
from pydantic import ValidationError

from app.models import SequenceRequest


@pytest.mark.parametrize(
    ("field", "value"),
    [("exposure", 0), ("exposure", 86401), ("interval", -1), ("shots", 0), ("shots", 100001)],
)
def test_numeric_parameter_validation(field, value):
    data = {"exposure": 1, "interval": 0, "shots": 1}
    data[field] = value
    with pytest.raises(ValidationError):
        SequenceRequest(**data)


@pytest.mark.parametrize("folder", ["../escape", "/absolute", "night/../../escape", "night\\..\\escape"])
def test_output_folder_cannot_escape_root(folder):
    with pytest.raises(ValidationError):
        SequenceRequest(exposure=1, interval=0, shots=1, output_subdirectory=folder)


def test_valid_nested_output_folder():
    request = SequenceRequest(exposure=1, interval=0, shots=1, output_subdirectory="2026-09-21/M31")
    assert request.output_subdirectory == "2026-09-21/M31"
