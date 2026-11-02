import pytest

from app.core.charts import downsample


def test_short_series_unchanged():
    assert downsample([1, 2, 3], [4, 5, 6], 10) == ([1, 2, 3], [4, 5, 6])


def test_downsample_keeps_ends_and_peak():
    xs = list(range(10_000))
    ys = [0.0] * 10_000
    ys[5_000] = 99.0
    ox, oy = downsample(xs, ys, 100)
    assert len(ox) == 100
    assert (ox[0], ox[-1]) == (0, 9_999)
    assert 99.0 in oy
    assert ox == sorted(ox)


def test_length_mismatch():
    with pytest.raises(ValueError):
        downsample([1, 2], [1])
