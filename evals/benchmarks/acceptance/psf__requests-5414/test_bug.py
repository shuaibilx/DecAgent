import pytest
from requests import PreparedRequest
from requests.exceptions import InvalidURL

def test_bug():
    request = PreparedRequest()
    with pytest.raises(InvalidURL):
        request.prepare_url("http://.example.com", None)
