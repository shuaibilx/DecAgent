from range_logic import clamp


def test_exclusive_upper_bound():
    assert clamp(10, 10) == 9


def test_values_below_bound():
    assert clamp(3, 10) == 3
