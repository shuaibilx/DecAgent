from requests.utils import prepend_scheme_if_needed

def test_bug():
    value = "http://user:pass@example.com/path?query"
    assert prepend_scheme_if_needed(value, "http") == value
