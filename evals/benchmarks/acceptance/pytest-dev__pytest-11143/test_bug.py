pytest_plugins = ["pytester"]

def test_bug(pytester):
    pytester.makepyfile(test_inner="0\n\ndef test_foo():\n    pass\n")
    result = pytester.runpytest()
    assert result.ret == 0
    result.assert_outcomes(passed=1)
