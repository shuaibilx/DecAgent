import pytest

def test_bug():
    explanation = pytest.approx({"foo": 0.0})._repr_compare({"foo": 42.0})
    assert "Max relative difference: inf" in "\n".join(explanation)
