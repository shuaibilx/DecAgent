pytest_plugins = ["pytester"]

def test_bug(pytester):
    pytester.makepyfile(**{"tests/test_1.py": """import pytest
def test_pass(arg):
    pass
@pytest.fixture
def arg():
    pytest.skip("Fixture conditional skip")
"""})
    result = pytester.runpytest("-rs", "tests/test_1.py", "--rootdir=tests")
    result.stdout.fnmatch_lines(["SKIPPED [[]1[]] tests/test_1.py:2: Fixture conditional skip"])
