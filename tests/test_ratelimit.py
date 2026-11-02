import pytest

from app.core.jobs import RetryLater
from app.core.ratelimit import RateLimiter, Window

T0 = 1_000_000_800.0  # start of a 900s window


def test_blocks_at_limit_and_resets(conn):
    rl = RateLimiter("p", [Window(900, 2), Window(86400, 100)])
    rl.acquire(conn, now=T0)
    rl.acquire(conn, now=T0)
    with pytest.raises(RetryLater) as e:
        rl.acquire(conn, now=T0)
    assert 0 < e.value.delay_s <= 902
    rl.acquire(conn, now=T0 + 900)  # next window
    daily = conn.execute("SELECT used FROM rate_limits WHERE window_s = 86400").fetchone()[0]
    assert daily == 3


def test_observe_overrides_usage(conn):
    rl = RateLimiter("p", [Window(900, 100)])
    rl.observe(conn, {900: (50, 50)}, now=T0)
    with pytest.raises(RetryLater):
        rl.acquire(conn, now=T0)


def test_reserve_keeps_headroom(conn):
    rl = RateLimiter("p", [Window(900, 3)], reserve=2)
    rl.acquire(conn, now=T0)
    with pytest.raises(RetryLater):
        rl.acquire(conn, now=T0)
