import pytest
from requests import Response
from requests.exceptions import ChunkedEncodingError
from urllib3.exceptions import ProtocolError

class BrokenStream:
    failed = False

    def stream(self, *args, **kwargs):
        if self.failed:
            return
        self.failed = True
        if False:
            yield b""
        raise ProtocolError("incomplete response body")

def test_bug():
    response = Response()
    response.status_code = 200
    response.raw = BrokenStream()
    for _ in range(2):
        with pytest.raises(ChunkedEncodingError):
            _ = response.content
