import pytest
from _pytest.mark.structures import get_unpacked_marks

def test_bug():
    marker = pytest.mark.xfail
    @marker("a")
    class A:
        pass
    @marker("b")
    class B:
        pass
    @marker("c")
    class C(A, B):
        pass
    assert get_unpacked_marks(C) == [marker("c").mark, marker("a").mark, marker("b").mark]
