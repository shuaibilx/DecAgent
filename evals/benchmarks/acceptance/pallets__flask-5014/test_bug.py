import pytest
from flask import Blueprint

def test_bug():
    with pytest.raises(ValueError, match="empty"):
        Blueprint("", __name__)
