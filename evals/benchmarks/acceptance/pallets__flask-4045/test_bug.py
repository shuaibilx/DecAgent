import pytest
from flask import Blueprint

def test_bug():
    with pytest.raises(ValueError, match="dot"):
        Blueprint("app.ui", __name__)
