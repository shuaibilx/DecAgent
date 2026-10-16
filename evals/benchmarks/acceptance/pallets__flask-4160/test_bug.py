from decimal import Decimal
from flask import json

def test_bug():
    assert json.dumps(Decimal("0.003")) == '"0.003"'
